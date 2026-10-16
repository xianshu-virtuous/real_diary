"""real_diary 的落盘层：素材碎片 / 完成留档 / 运行水位。

三个键，都在 ``data/json_storage/<namespace>/``：

- ``day-YYYY-MM-DD``：当天的**素材碎片**（采集期追加；日记写完后删）
- ``done``：已生成日记的日期留档（路径 / 素材数 / 模型 / 是否完整 / 碎片删没删）
- ``runtime``：运行水位（上次巡检、上次生成、失败计数与退避）

三条约定：

1. **所有落盘都过这里**，其它模块不直接碰 ``storage_api``；
2. 缓存与节流闸都挂在**模块级**——框架每次 ``get_service()`` 都新建实例，
   跨调用者能共享状态的只有模块全局或盘；
3. **日期一律用本地时区的自然日**（与 ``time_sense`` / ``daily_memory`` 的
   ``memory_date`` 口径一致），不做 UTC 换算。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("real_diary.state")

#: 素材文件的键前缀（加日期即成键名）。
DAY_PREFIX = "day-"
#: 完成留档键。
DONE_KEY = "done"
#: 运行水位键。
RUNTIME_KEY = "runtime"

_DEFAULT_NAMESPACE = "real_diary"

#: 素材条目的固定形状（字段名是**自用契约**，只做加法）：
#: ``{at, day, source, kind, text, meta}``
_ITEM_KEYS = ("at", "day", "source", "kind", "text", "meta")

_namespace = _DEFAULT_NAMESPACE

#: 进程内缓存（同一次运行里别反复读盘）。
_day_cache: dict[str, dict[str, Any]] = {}
_done_cache: dict[str, Any] | None = None
_runtime_cache: dict[str, Any] | None = None

#: 写盘节流：同一键这么短时间内不重复落盘。
_SAVE_THROTTLE = 2.0
_last_saved: dict[str, float] = {}

#: 星期中文（标题模板用）。
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# --------------------------------------------------------------------------- #
# 命名空间
# --------------------------------------------------------------------------- #


def configure(namespace: str) -> str:
    """设定存储命名空间（``data/json_storage/<namespace>/``）。

    只保留字母/数字/下划线/连字符，避免命名空间里出现路径分隔符把数据写到别处。

    Args:
        namespace: 配置里的命名空间。

    Returns:
        实际生效的命名空间。
    """
    global _namespace
    cleaned = str(namespace or "").strip() or _DEFAULT_NAMESPACE
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", cleaned) or _DEFAULT_NAMESPACE
    if safe != _namespace:
        _namespace = safe
        invalidate()
    return _namespace


def store_name() -> str:
    """当前命名空间。"""
    return _namespace


def invalidate() -> None:
    """清掉进程内缓存（命名空间变了、或排障时手动刷）。"""
    _day_cache.clear()
    _done_cache = None
    _runtime_cache = None
    _last_saved.clear()


# --------------------------------------------------------------------------- #
# 日期工具（一律本地时区自然日）
# --------------------------------------------------------------------------- #


def date_of(timestamp: float) -> str:
    """把时间戳转成 ``YYYY-MM-DD``（本地时区）。"""
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d")


def today_str() -> str:
    """今天（本地时区）。"""
    return date_of(time.time())


def shift_date(date_str: str, days: int) -> str:
    """日期加减天数。非法输入返回空串。"""
    parsed = parse_date(date_str)
    if parsed is None:
        return ""
    return (parsed + timedelta(days=int(days))).strftime("%Y-%m-%d")


def parse_date(date_str: str) -> datetime | None:
    """解析 ``YYYY-MM-DD``；非法返回 ``None``。"""
    try:
        return datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def is_valid_date(date_str: str) -> bool:
    """是不是合法日期。"""
    return parse_date(date_str) is not None


def day_bounds(date_str: str) -> tuple[float, float]:
    """某个自然日的 ``(起, 止)`` 时间戳（本地零点到次日零点）。"""
    parsed = parse_date(date_str)
    if parsed is None:
        return 0.0, 0.0
    start = parsed.timestamp()
    return start, start + 86400.0


def weekday_cn(date_str: str) -> str:
    """中文星期（``周三``）；非法日期返回空串。"""
    parsed = parse_date(date_str)
    if parsed is None:
        return ""
    return _WEEKDAY_CN[parsed.weekday()]


def days_between(earlier: str, later: str) -> int:
    """两个日期相差几天（``later - earlier``）；非法输入返回 0。"""
    a = parse_date(earlier)
    b = parse_date(later)
    if a is None or b is None:
        return 0
    return int((b - a).days)


def clock_of(timestamp: float) -> str:
    """``HH:MM``（本地时区）。"""
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%H:%M")


def day_key(date_str: str) -> str:
    """素材文件的存储键。"""
    return f"{DAY_PREFIX}{date_str}"


# --------------------------------------------------------------------------- #
# 素材条目
# --------------------------------------------------------------------------- #


def make_item(
    *,
    at: float,
    source: str,
    kind: str,
    text: str,
    meta: dict[str, Any] | None = None,
    max_chars: int = 1200,
) -> dict[str, Any] | None:
    """组装一条素材；正文为空时返回 ``None``（空素材不占位置）。

    Args:
        at: 采集时刻（epoch 秒）。
        source: 来源标识（``context_archiver`` / ``daily_memory`` / ``sisp`` / ...）。
        kind: 条目类型（``archive`` / ``memory`` / ``state`` / ``timeline`` / ``note``）。
        text: 正文。
        meta: 附加字段（``stream_id`` / ``memory_ids`` / ``risk`` ...）。
        max_chars: 正文上限。

    Returns:
        标准化后的条目；无正文返回 ``None``。
    """
    body = " ".join(str(text or "").split())
    if not body:
        return None
    limit = max(1, int(max_chars))
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    try:
        stamp = float(at)
    except (TypeError, ValueError):
        stamp = time.time()
    if stamp <= 0:
        stamp = time.time()
    return {
        "at": stamp,
        "day": date_of(stamp),
        "source": str(source or "").strip() or "unknown",
        "kind": str(kind or "").strip() or "note",
        "text": body,
        "meta": dict(meta or {}),
    }


def normalize_item(raw: Any, *, max_chars: int = 1200) -> dict[str, Any] | None:
    """把任意输入压成标准条目；不可用时返回 ``None``。"""
    if not isinstance(raw, dict):
        return None
    return make_item(
        at=raw.get("at") or 0.0,
        source=str(raw.get("source") or ""),
        kind=str(raw.get("kind") or ""),
        text=str(raw.get("text") or ""),
        meta=raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
        max_chars=max_chars,
    )


def item_fingerprint(item: dict[str, Any]) -> str:
    """条目的去重指纹（来源 + 类型 + 正文前 120 字）。

    用途：事件与巡检会重复上报同一件事（归档事件 + 兜底巡检各来一次），
    指纹让重复上报只留一条。
    """
    source = str(item.get("source") or "")
    kind = str(item.get("kind") or "")
    text = " ".join(str(item.get("text") or "").split())[:120]
    return f"{source}|{kind}|{text}"


def _empty_day(date_str: str) -> dict[str, Any]:
    now = time.time()
    return {"date": date_str, "items": [], "created_at": now, "updated_at": now}


def _normalize_day(raw: Any, date_str: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_day(date_str)
    items: list[dict[str, Any]] = []
    for entry in raw.get("items") or []:
        item = normalize_item(entry)
        if item is not None:
            items.append(item)
    now = time.time()
    return {
        "date": str(raw.get("date") or date_str),
        "items": items,
        "created_at": float(raw.get("created_at") or now),
        "updated_at": float(raw.get("updated_at") or now),
    }


# --------------------------------------------------------------------------- #
# 素材：读 / 追加 / 删
# --------------------------------------------------------------------------- #


async def load_day(date_str: str, *, fresh: bool = False) -> dict[str, Any]:
    """读某天的素材文件。

    Args:
        date_str: ``YYYY-MM-DD``。
        fresh: 跳过进程内缓存，强制读盘。

    Returns:
        当天素材结构；不存在时返回空结构。
    """
    if not date_str:
        return _empty_day("")
    if not fresh and date_str in _day_cache:
        return _day_cache[date_str]
    raw = await storage_api.load_json(store_name(), day_key(date_str))
    day = _normalize_day(raw, date_str)
    _day_cache[date_str] = day
    return day


async def save_day(date_str: str, day: dict[str, Any], *, force: bool = True) -> None:
    """写某天的素材文件（带节流）。"""
    if not date_str:
        return
    key = day_key(date_str)
    now = time.time()
    if not force and (now - _last_saved.get(key, -1.0e9)) < _SAVE_THROTTLE:
        return
    _last_saved[key] = now
    day["updated_at"] = now
    _day_cache[date_str] = day
    await storage_api.save_json(store_name(), key, day)


async def append_items(
    date_str: str,
    items: list[dict[str, Any]],
    *,
    limit: int = 400,
    max_chars: int = 1200,
) -> tuple[int, int]:
    """把若干素材追加进某天，按指纹去重。

    Args:
        date_str: 目标日期。
        items: 待追加素材（会被标准化）。
        limit: 单日条数上限；超了丢最旧的。
        max_chars: 单条正文上限。

    Returns:
        ``(新增条数, 当前总条数)``。
    """
    if not date_str or not items:
        return 0, 0

    day = await load_day(date_str, fresh=True)
    existing: list[dict[str, Any]] = list(day.get("items") or [])
    known = {item_fingerprint(entry) for entry in existing}

    added = 0
    for raw in items:
        item = normalize_item(raw, max_chars=max_chars)
        if item is None:
            continue
        fingerprint = item_fingerprint(item)
        if fingerprint in known:
            continue
        known.add(fingerprint)
        existing.append(item)
        added += 1

    if added == 0:
        return 0, len(existing)

    cap = max(20, int(limit))
    if len(existing) > cap:
        dropped = len(existing) - cap
        existing = existing[-cap:]
        note = make_item(
            at=existing[-1].get("at") or time.time(),
            source="real_diary",
            kind="note",
            text=f"素材超过单日上限（{cap} 条），最旧的 {dropped} 条已被丢弃。",
            meta={"dropped": dropped},
            max_chars=200,
        )
        if note is not None:
            existing.append(note)

    day["items"] = existing
    await save_day(date_str, day, force=True)
    return added, len(existing)


async def delete_day(date_str: str) -> bool:
    """删掉某天的素材文件（日记落盘成功后才调用）。"""
    if not date_str:
        return False
    _day_cache.pop(date_str, None)
    _last_saved.pop(day_key(date_str), None)
    try:
        return bool(await storage_api.delete_json(store_name(), day_key(date_str)))
    except Exception as error:  # noqa: BLE001 - 删除失败只记日志
        logger.warning(f"[real_diary] 删除素材失败（{date_str}）: {error}")
        return False


async def list_days() -> list[str]:
    """列出所有还有素材碎片的日期（升序）。"""
    try:
        keys = await storage_api.list_json(store_name())
    except Exception as error:  # noqa: BLE001 - 列不出来就当没有
        logger.debug(f"[real_diary] 列出素材文件失败: {error}")
        return []
    days = [
        str(key)[len(DAY_PREFIX) :]
        for key in keys or []
        if str(key).startswith(DAY_PREFIX) and is_valid_date(str(key)[len(DAY_PREFIX) :])
    ]
    return sorted(days)


# --------------------------------------------------------------------------- #
# 完成留档
# --------------------------------------------------------------------------- #


def _empty_done() -> dict[str, Any]:
    return {"days": {}, "updated_at": time.time()}


def _normalize_done(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_done()
    days = raw.get("days")
    return {
        "days": dict(days) if isinstance(days, dict) else {},
        "updated_at": float(raw.get("updated_at") or time.time()),
    }


async def load_done(*, fresh: bool = False) -> dict[str, Any]:
    """读完成留档。"""
    global _done_cache
    if not fresh and _done_cache is not None:
        return _done_cache
    raw = await storage_api.load_json(store_name(), DONE_KEY)
    _done_cache = _normalize_done(raw)
    return _done_cache


async def save_done(done: dict[str, Any], *, force: bool = True) -> None:
    """写完成留档（带节流）。"""
    global _done_cache
    now = time.time()
    if not force and (now - _last_saved.get(DONE_KEY, -1.0e9)) < _SAVE_THROTTLE:
        return
    _last_saved[DONE_KEY] = now
    done["updated_at"] = now
    _done_cache = done
    await storage_api.save_json(store_name(), DONE_KEY, done)


async def mark_done(date_str: str, record: dict[str, Any], *, keep: int = 400) -> dict[str, Any]:
    """登记「这天写过日记了」，并按 keep 裁剪留档。

    Args:
        date_str: 日期。
        record: 留档内容（路径 / 素材数 / 模型 / 是否完整 / 碎片删没删）。
        keep: 最多保留多少天（按日期倒序裁）。

    Returns:
        最新的留档结构。
    """
    done = await load_done(fresh=True)
    days: dict[str, Any] = dict(done.get("days") or {})
    payload = dict(record or {})
    payload.setdefault("at", time.time())
    days[date_str] = payload

    cap = max(1, int(keep))
    if len(days) > cap:
        for old in sorted(days.keys())[: len(days) - cap]:
            days.pop(old, None)

    done["days"] = days
    await save_done(done, force=True)
    return done


async def get_done_record(date_str: str, *, fresh: bool = False) -> dict[str, Any] | None:
    """取某天的留档；没有就返回 ``None``。

    「这天写过没」的判据就是它——不靠文件是否存在，避免输出目录被手动
    移动/改名后误判成「没写过」而重复生成。
    """
    done = await load_done(fresh=fresh)
    record = (done.get("days") or {}).get(date_str)
    return dict(record) if isinstance(record, dict) else None


async def forget_done(date_str: str) -> bool:
    """抹掉某天的留档（``/日记 重写`` 用）。"""
    done = await load_done(fresh=True)
    days: dict[str, Any] = dict(done.get("days") or {})
    if date_str not in days:
        return False
    days.pop(date_str, None)
    done["days"] = days
    await save_done(done, force=True)
    return True


# --------------------------------------------------------------------------- #
# 运行水位
# --------------------------------------------------------------------------- #


def _empty_runtime() -> dict[str, Any]:
    return {
        "last_poll_at": 0.0,
        "last_check_at": 0.0,
        "last_generate_at": 0.0,
        "last_generate_date": "",
        "fail_streak": 0,
        "last_fail_at": 0.0,
        "last_error": "",
    }


async def load_runtime(*, fresh: bool = False) -> dict[str, Any]:
    """读运行水位。"""
    global _runtime_cache
    if not fresh and _runtime_cache is not None:
        return _runtime_cache
    raw = await storage_api.load_json(store_name(), RUNTIME_KEY)
    base = _empty_runtime()
    if isinstance(raw, dict):
        base.update({key: raw.get(key, value) for key, value in base.items()})
    _runtime_cache = base
    return _runtime_cache


async def save_runtime(patch: dict[str, Any], *, force: bool = True) -> dict[str, Any]:
    """合并写运行水位。"""
    global _runtime_cache
    current = dict(await load_runtime(fresh=True))
    current.update(dict(patch or {}))
    now = time.time()
    if not force and (now - _last_saved.get(RUNTIME_KEY, -1.0e9)) < _SAVE_THROTTLE:
        _runtime_cache = current
        return current
    _last_saved[RUNTIME_KEY] = now
    _runtime_cache = current
    await storage_api.save_json(store_name(), RUNTIME_KEY, current)
    return current


def backoff_seconds(streak: int, *, base: int = 300, cap: int = 7200) -> float:
    """失败退避秒数（指数增长，封顶）。

    Args:
        streak: 连续失败次数（≥1）。
        base: 首次退避秒数。
        cap: 退避上限。

    Returns:
        本次应该等多久。
    """
    if base <= 0:
        return 0.0
    steps = max(0, min(int(streak) - 1, 20))
    return float(min(int(base) * (2**steps), int(cap)))


__all__ = [
    "DAY_PREFIX",
    "DONE_KEY",
    "RUNTIME_KEY",
    "append_items",
    "backoff_seconds",
    "clock_of",
    "configure",
    "date_of",
    "day_bounds",
    "day_key",
    "days_between",
    "delete_day",
    "forget_done",
    "get_done_record",
    "invalidate",
    "is_valid_date",
    "item_fingerprint",
    "list_days",
    "load_day",
    "load_done",
    "load_runtime",
    "make_item",
    "mark_done",
    "normalize_item",
    "parse_date",
    "save_day",
    "save_done",
    "save_runtime",
    "shift_date",
    "store_name",
    "today_str",
    "weekday_cn",
]
