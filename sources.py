"""real_diary 的素材源读取层。

每个源只做两件事：**只读** + **按自然日过滤**。全部不写别人的数据。

| 源 | 位置 | 拿什么 |
| --- | --- | --- |
| 归档审计 | ``data/json_storage/context_archiver/audit.json``（**list**） | 每次归档的摘要 + 时间区间 + memory_id |
| 记忆索引 | 同目录 ``memory_index.json``（``{"entries": [...]}``） | 记忆条目元信息（标题 / 片段 / 触发词 / 风险） |
| 每日短期记忆 | ``send_to`` / ``cross_stream_relay`` 的 ``daily_memory_<流>_<日期>.json`` | 现成的「按天一条」摘要 |
| 状态阶段 | ``sisp`` / ``deep_neo_sisp`` 的 ``<流>.json`` + ``body_<流>.json`` | 当前阶段快照（**转变由 collector 自己比对**） |
| booku 记忆库 | ``booku_memory`` 服务 | 当天写入的记忆条目（默认关，最贵） |

两条纪律：

1. **探测服务前先只读预检**（``get_all_services()``），否则会白刷
   ``Service 类未找到`` 的 WARNING —— 详见 RISKS R22（字母序加载）。
2. **读不回来就当没有**，绝不抛异常打断生成：素材缺一路只是日记少一段，
   让插件崩掉才是事故。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api import service_api, storage_api
from src.app.plugin_system.api.log_api import get_logger

from . import state
from .config import RealDiaryConfig

logger = get_logger("real_diary.sources")

#: context_archiver 的存储命名空间与两个键（与它的 state.py 常量对齐）。
ARCHIVER_STORE = "context_archiver"
ARCHIVER_AUDIT_KEY = "audit"
ARCHIVER_MEMORY_INDEX_KEY = "memory_index"

#: 写「每日短期记忆」的两个插件（字段完全一致，取较新的那份）。
DAILY_MEMORY_STORES = ("send_to", "cross_stream_relay")
_DAILY_MEMORY_PREFIX = "daily_memory_"

#: 状态快照里「身体阶段」那个键的前缀。
_BODY_PREFIX = "body_"

#: booku 记忆服务签名。
BOOKU_SIGNATURE = "booku_memory:service:booku_memory"


# --------------------------------------------------------------------------- #
# 只读小工具
# --------------------------------------------------------------------------- #


async def _load_json(store: str, name: str) -> Any:
    """安全读一个 JSON 键（读不到返回 ``None``）。"""
    try:
        return await storage_api.load_json(store, name)
    except Exception as error:  # noqa: BLE001 - 别人没装/没写过都算「没有」
        logger.debug(f"[real_diary] 读取 {store}/{name} 失败: {error}")
        return None


async def _list_json(store: str) -> list[str]:
    """安全列一个命名空间的键（读不到返回空）。"""
    try:
        return list(await storage_api.list_json(store) or [])
    except Exception as error:  # noqa: BLE001
        logger.debug(f"[real_diary] 列出 {store} 失败: {error}")
        return []


def service_if_registered(signature: str) -> Any:
    """取服务实例，**先只读预检**再调用。

    预检能避免框架打 ``Service 类未找到`` 的 WARNING（加载期探测后加载的插件
    必然扑空，见 RISKS R22）。注册表读不到时不武断，退回直接取。

    Args:
        signature: 服务组件签名。

    Returns:
        服务实例；不可用返回 ``None``。
    """
    try:
        registered = service_api.get_all_services()
        if isinstance(registered, dict) and registered and signature not in registered:
            return None
    except Exception as error:  # noqa: BLE001 - 注册表读不到就退回直接取
        logger.debug(f"[real_diary] 读取组件注册表失败: {error}")
    try:
        return service_api.get_service(signature)
    except Exception as error:  # noqa: BLE001 - 取不到按不可用处理
        logger.debug(f"[real_diary] 取服务失败（{signature}）: {error}")
        return None


def _as_float(value: Any, default: float = 0.0) -> float:
    """把任意值压成 float。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


def _short(value: Any, limit: int = 24) -> str:
    """压成一行短文本（日志/元信息用）。"""
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


# --------------------------------------------------------------------------- #
# 源 1：归档审计（"那天到底归档了什么"的时间线）
# --------------------------------------------------------------------------- #


async def from_audit(config: RealDiaryConfig, date_str: str, **_: Any) -> list[dict[str, Any]]:
    """从 ``context_archiver`` 的归档审计里取某天的记录。

    只采**成功**的归档：失败的那次水位线没推进，下次会重新总结，
    现在采进来只会得到半截素材（真要被看见，巡检那一路会在它成功后再采到）。

    Args:
        config: 插件配置。
        date_str: 目标自然日。

    Returns:
        素材条目列表。
    """
    raw = await _load_json(ARCHIVER_STORE, ARCHIVER_AUDIT_KEY)
    if not isinstance(raw, list):
        return []

    day_start, day_end = state.day_bounds(date_str)
    max_chars = int(config.collect.max_item_chars)
    items: list[dict[str, Any]] = []

    for record in raw:
        if not isinstance(record, dict):
            continue
        if not bool(record.get("ok")):
            continue
        start_ts = _as_float(record.get("start_ts"))
        end_ts = _as_float(record.get("end_ts"), start_ts)
        at = _as_float(record.get("at"), end_ts)
        # 与目标日有交集就算（话题可能横跨午夜）
        if end_ts and end_ts < day_start:
            continue
        if at and at >= day_end:
            continue

        summary = " ".join(str(record.get("summary") or "").split())
        if not summary:
            continue

        item = state.make_item(
            at=at or end_ts or day_start,
            source="context_archiver",
            kind="archive",
            text=f"[归档] {summary}",
            meta={
                "stream_id": str(record.get("stream_id") or ""),
                "trigger": str(record.get("trigger") or ""),
                "message_count": int(_as_float(record.get("message_count"))),
                "memory_ids": list(record.get("memory_ids") or []),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "sink": str(record.get("sink") or ""),
            },
            max_chars=max_chars,
        )
        if item is not None:
            items.append(item)

    return items


# --------------------------------------------------------------------------- #
# 源 2：记忆索引（记忆条目的元信息）
# --------------------------------------------------------------------------- #


async def from_memory_index(
    config: RealDiaryConfig, date_str: str, **_: Any
) -> list[dict[str, Any]]:
    """从 ``context_archiver`` 的本地记忆索引里取某天写下的记忆条目。"""
    raw = await _load_json(ARCHIVER_STORE, ARCHIVER_MEMORY_INDEX_KEY)
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []

    day_start, day_end = state.day_bounds(date_str)
    max_chars = int(config.collect.max_item_chars)
    items: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        at = _as_float(entry.get("at"))
        if not (day_start <= at < day_end):
            continue
        title = " ".join(str(entry.get("title") or "").split())
        snippet = " ".join(str(entry.get("snippet") or "").split())
        if not (title or snippet):
            continue
        text = f"[记忆] {title}" + (f"：{snippet}" if snippet else "")
        item = state.make_item(
            at=at,
            source="context_archiver",
            kind="memory",
            text=text,
            meta={
                "memory_id": str(entry.get("id") or ""),
                "triggers": list(entry.get("triggers") or [])[:12],
                "risk": str(entry.get("risk") or "normal"),
                "person": str(entry.get("person") or ""),
                "stream": str(entry.get("stream") or ""),
            },
            max_chars=max_chars,
        )
        if item is not None:
            items.append(item)

    return items


# --------------------------------------------------------------------------- #
# 源 3：每日短期记忆（现成的「按天一条」）
# --------------------------------------------------------------------------- #


async def from_daily_memory(
    config: RealDiaryConfig, date_str: str, **_: Any
) -> list[dict[str, Any]]:
    """取 ``send_to`` / ``cross_stream_relay`` 的当天日记忆。

    两个插件的字段**完全一致**、也常常同时装着（实测同群同日两份，
    ``message_count`` 与 ``last_summarized_ts`` 一模一样——等于各跑了一遍）。
    所以这里按 ``stream_id`` 归并，只留 ``last_summarized_ts`` 更新的那份。
    """
    max_chars = int(config.collect.max_item_chars)
    best: dict[str, dict[str, Any]] = {}

    for store in DAILY_MEMORY_STORES:
        keys = await _list_json(store)
        for key in keys:
            name = str(key)
            if not name.startswith(_DAILY_MEMORY_PREFIX) or not name.endswith(f"_{date_str}"):
                continue
            data = await _load_json(store, name)
            if not isinstance(data, dict):
                continue
            summary = " ".join(str(data.get("summary") or "").split())
            if not summary:
                continue
            stream_id = str(data.get("stream_id") or name[len(_DAILY_MEMORY_PREFIX) : -len(date_str) - 1])
            fresh_at = _as_float(data.get("last_summarized_ts"))
            previous = best.get(stream_id)
            if previous is not None and _as_float(previous.get("_fresh_at")) >= fresh_at:
                continue
            best[stream_id] = {
                "store": store,
                "stream_id": stream_id,
                "group_name": str(data.get("group_name") or ""),
                "chat_type": str(data.get("chat_type") or ""),
                "summary": summary,
                "message_count": int(_as_float(data.get("message_count"))),
                "updated_at": str(data.get("updated_at") or ""),
                "_fresh_at": fresh_at,
            }

    day_start, day_end = state.day_bounds(date_str)
    items: list[dict[str, Any]] = []
    for row in best.values():
        at = _as_float(row.get("_fresh_at")) or (day_end - 1.0)
        where = row.get("group_name") or _short(row.get("stream_id"), 8)
        item = state.make_item(
            at=min(max(at, day_start), day_end - 1.0),
            source="daily_memory",
            kind="daily",
            # 这个标签是有意的：daily_memory 是**她自己视角**的当天回忆
            # （由 actor 模型按第一人称总结），提示词里被当作「可以直接用的回忆」。
            text=f"[我的回忆·{where}] {row.get('summary')}",
            meta={
                "store": row.get("store"),
                "stream_id": row.get("stream_id"),
                "chat_type": row.get("chat_type"),
                "message_count": row.get("message_count"),
                "updated_at": row.get("updated_at"),
            },
            max_chars=max_chars,
        )
        if item is not None:
            items.append(item)

    return items


# --------------------------------------------------------------------------- #
# 源 4：状态快照（供 collector 比对阶段转变）
# --------------------------------------------------------------------------- #


async def list_state_streams(config: RealDiaryConfig) -> list[str]:
    """列出有状态快照的聊天流（按配置里的命名空间顺序，用第一个非空的）。"""
    for store in config.collect.sisp_stores:
        keys = await _list_json(str(store))
        stream_ids = [
            str(key)
            for key in keys
            if key and not str(key).startswith(_BODY_PREFIX)
        ]
        if stream_ids:
            return stream_ids
    return []


async def read_state_snapshot(
    config: RealDiaryConfig, stream_id: str
) -> dict[str, Any] | None:
    """读某个流的「局内状态 + 身体阶段」快照。

    Args:
        config: 插件配置。
        stream_id: 聊天流标识。

    Returns:
        ``{"store", "state", "body"}``；该流在任何一个命名空间里都没有快照时返回 ``None``。
    """
    for store in config.collect.sisp_stores:
        store_name = str(store)
        data = await _load_json(store_name, stream_id)
        if isinstance(data, dict):
            body = await _load_json(store_name, f"{_BODY_PREFIX}{stream_id}")
            return {
                "store": store_name,
                "state": data,
                "body": body if isinstance(body, dict) else {},
            }
    return None


def phase_of(snapshot: dict[str, Any]) -> tuple[str, float]:
    """从快照里抠出 ``(body_phase, phase_until)``。"""
    body = snapshot.get("body") if isinstance(snapshot, dict) else None
    if not isinstance(body, dict):
        return "", 0.0
    return str(body.get("body_phase") or ""), _as_float(body.get("phase_until"))


def phase_line(snapshot: dict[str, Any]) -> str:
    """把一份状态快照渲染成一行素材正文（人读得懂、模型也读得懂）。"""
    state_data = snapshot.get("state") if isinstance(snapshot, dict) else None
    if not isinstance(state_data, dict):
        state_data = {}
    phase, until = phase_of(snapshot)
    parts: list[str] = []
    if phase:
        left = ""
        if until > 0:
            remain = int(max(0.0, until - time.time()) // 60)
            left = f"（约 {remain} 分钟后转下一档）" if remain > 0 else "（已到期，下次采样会看到新档）"
        parts.append(f"阶段：{phase}{left}")
    if bool(state_data.get("in_sex_mode")):
        parts.append("处于私密模式")
    route = str(state_data.get("route") or "")
    if route:
        parts.append(f"路线：{route}")
    scene = str(state_data.get("scene") or "")
    if scene:
        parts.append(f"剧情：{scene}")
    event = str(state_data.get("current_event") or "")
    if event:
        parts.append(f"刚发生：{event}")
    spontaneous = str(state_data.get("spontaneous_event") or "")
    if spontaneous:
        parts.append(f"自发事件：{spontaneous}")
    return "；".join(parts) if parts else ""


# --------------------------------------------------------------------------- #
# 源 5：booku 记忆库（默认关）
# --------------------------------------------------------------------------- #


async def from_booku(config: RealDiaryConfig, date_str: str, **_: Any) -> list[dict[str, Any]]:
    """取 booku 记忆库里某天写入的记忆条目。

    **默认关**：booku 没有「按时间范围查」的接口，只能拉一批再本地按
    ``metadata.created_at`` 筛；而且它的检索是全局的，拉多少要看 luck。
    真要用，把 ``collect.booku_digest`` 打开。

    只读，不写不删——本插件永不修改记忆库。
    """
    service = service_if_registered(BOOKU_SIGNATURE)
    if service is None:
        return []

    try:
        result = await service.list_memory_entries(limit=200, include_archived=True)
    except Exception as error:  # noqa: BLE001 - 记忆库读不到就当没有
        logger.debug(f"[real_diary] 读取记忆库失败: {error}")
        return []

    rows = result.get("items") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []

    day_start, day_end = state.day_bounds(date_str)
    max_chars = int(config.collect.max_item_chars)
    items: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        at = _as_float(meta.get("event_start_at")) or _as_float(meta.get("created_at"))
        if not (day_start <= at < day_end):
            continue
        title = " ".join(str(row.get("title") or "").split())
        snippet = " ".join(str(row.get("content_snippet") or "").split())
        if not (title or snippet):
            continue
        item = state.make_item(
            at=at,
            source="booku",
            kind="memory",
            text=f"[记忆库] {title}" + (f"：{snippet}" if snippet else ""),
            meta={
                "memory_id": str(row.get("id") or ""),
                "status": str(meta.get("status") or ""),
                "memory_type": str(meta.get("memory_type") or ""),
            },
            max_chars=max_chars,
        )
        if item is not None:
            items.append(item)

    return items


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


async def collect_for_day(config: RealDiaryConfig, date_str: str) -> list[dict[str, Any]]:
    """把某一天能从各源抓到的素材全部抓回来（已按天过滤）。

    每个源各自兜异常，单个源失败不影响其它源。

    Args:
        config: 插件配置。
        date_str: 目标自然日。

    Returns:
        素材条目列表（未去重，交给 ``state.append_items`` 去重）。
    """
    items: list[dict[str, Any]] = []
    collect = config.collect

    if collect.audit_timeline:
        items += await from_audit(config, date_str)
    if collect.memory_index:
        items += await from_memory_index(config, date_str)
    if collect.daily_memory:
        items += await from_daily_memory(config, date_str)
    if collect.booku_digest:
        items += await from_booku(config, date_str)

    return items


__all__ = [
    "ARCHIVER_AUDIT_KEY",
    "ARCHIVER_MEMORY_INDEX_KEY",
    "ARCHIVER_STORE",
    "BOOKU_SIGNATURE",
    "DAILY_MEMORY_STORES",
    "collect_for_day",
    "from_audit",
    "from_booku",
    "from_daily_memory",
    "from_memory_index",
    "list_state_streams",
    "phase_line",
    "phase_of",
    "read_state_snapshot",
    "service_if_registered",
]
