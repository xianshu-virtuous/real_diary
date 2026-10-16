"""real_diary 的采集编排。

三条通路，互为兜底：

1. **事件（实时）**：订阅 ``context_archiver:archived``，归档一成功就把这次
   摘要收成素材——这是「归档即联动」，也是主人要的「立即」；
2. **巡检（兜底）**：定时把「今天/昨天」能抓的都抓一遍，补上事件丢掉的、
   以及进程当时没在跑的那一段；
3. **状态采样**：状态快照只有「当前值」没有历史，所以转变必须由本插件
   自己比对「上次看到什么」（存在 ``runtime.phases``）。

日期归属一律按**素材自己的时间戳**算自然日，而不是「现在」——
凌晨 2 点补昨天的日记时，事件带来的素材仍然应该落进昨天。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from . import sources, state
from .config import RealDiaryConfig

logger = get_logger("real_diary.collector")


def item_from_archive_payload(
    config: RealDiaryConfig,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """把 ``context_archiver:archived`` 的 payload 转成一条素材。

    Args:
        config: 插件配置。
        payload: 事件参数（契约见 context_archiver 的 ``_publish_archived_event``）。

    Returns:
        素材条目；payload 不可用时返回 ``None``。
    """
    if not isinstance(payload, dict):
        return None
    summary = " ".join(str(payload.get("summary") or "").split())
    if not summary:
        return None
    at = float(payload.get("end_ts") or payload.get("at") or 0.0) or time.time()
    return state.make_item(
        at=at,
        source="context_archiver",
        kind="archive",
        text=f"[归档] {summary}",
        meta={
            "stream_id": str(payload.get("stream_id") or ""),
            "trigger": str(payload.get("trigger") or ""),
            "message_count": int(float(payload.get("message_count") or 0)),
            "memory_ids": list(payload.get("memory_ids") or []),
            "start_ts": float(payload.get("start_ts") or 0.0),
            "end_ts": float(payload.get("end_ts") or 0.0),
            "sink": str(payload.get("sink") or ""),
            "via": "event",
        },
        max_chars=int(config.collect.max_item_chars),
    )


async def ingest_archive_event(
    config: RealDiaryConfig,
    payload: dict[str, Any],
) -> int:
    """把一条归档事件收成素材（落到它所属的那一天）。

    Args:
        config: 插件配置。
        payload: 事件参数。

    Returns:
        新增条数（重复上报会返回 0）。
    """
    item = item_from_archive_payload(config, payload)
    if item is None:
        return 0
    date_str = str(item.get("day") or "") or state.today_str()
    added, total = await state.append_items(
        date_str,
        [item],
        limit=int(config.collect.max_items_per_day),
        max_chars=int(config.collect.max_item_chars),
    )
    if added and config.plugin.debug_log:
        logger.info(
            f"[real_diary] 归档事件已收（{date_str}，+{added} 条，共 {total} 条）"
        )
    return added


async def ingest_for_day(
    config: RealDiaryConfig,
    date_str: str,
    *,
    reason: str = "poll",
) -> dict[str, Any]:
    """为某一天做一次兜底抓取并落盘。

    Args:
        config: 插件配置。
        date_str: 目标自然日。
        reason: 触发原因（写日志）。

    Returns:
        统计：``{date, fetched, added, total, reason}``。
    """
    if not state.is_valid_date(date_str):
        return {"date": date_str, "fetched": 0, "added": 0, "total": 0, "reason": reason}

    items = await sources.collect_for_day(config, date_str)
    added, total = await state.append_items(
        date_str,
        items,
        limit=int(config.collect.max_items_per_day),
        max_chars=int(config.collect.max_item_chars),
    )
    if added and config.plugin.debug_log:
        logger.info(
            f"[real_diary] {date_str} 兜底抓取（{reason}）：抓到 {len(items)} 条，新增 {added} 条，共 {total} 条"
        )
    return {"date": date_str, "fetched": len(items), "added": added, "total": total, "reason": reason}


async def sample_phases(config: RealDiaryConfig) -> int:
    """采样各流的状态阶段，只把**变化**记成素材。

    为什么这么做：``sisp`` / ``deep_neo_sisp`` 都不发布「阶段转变」事件、
    也不保留转变历史（只有当前值），所以唯一可行的路径就是「读快照 + 自己比对」。

    已知局限（如实说明、不假装完整）：**没有消息的时段采不到**——阶段可能
    在你没被调用的时间里自己翻页了。日记里只写「我观察到什么」，不推断中间过程。

    Args:
        config: 插件配置。

    Returns:
        记录了几条变化。
    """
    if not config.collect.state_phases:
        return 0

    runtime = await state.load_runtime(fresh=True)
    seen: dict[str, Any] = dict(runtime.get("phases") or {})
    stream_ids = await sources.list_state_streams(config)
    if not stream_ids:
        return 0

    changes = 0
    for stream_id in stream_ids:
        snapshot = await sources.read_state_snapshot(config, stream_id)
        if snapshot is None:
            continue
        phase, _until = sources.phase_of(snapshot)
        if not phase:
            continue

        previous = seen.get(stream_id) if isinstance(seen.get(stream_id), dict) else {}
        last_phase = str((previous or {}).get("phase") or "")
        if last_phase == phase:
            continue

        line = sources.phase_line(snapshot)
        if not line:
            continue

        first_time = not last_phase
        prefix = "首次采样" if first_time else f"{last_phase} → {phase}"
        item = state.make_item(
            at=time.time(),
            source="sisp",
            kind="state",
            text=f"[状态] {prefix}：{line}",
            meta={
                "stream_id": stream_id,
                "store": snapshot.get("store"),
                "from": last_phase,
                "to": phase,
                "first": first_time,
            },
            max_chars=int(config.collect.max_item_chars),
        )
        if item is not None:
            added, _total = await state.append_items(
                str(item.get("day") or "") or state.today_str(),
                [item],
                limit=int(config.collect.max_items_per_day),
                max_chars=int(config.collect.max_item_chars),
            )
            changes += int(added)
            if config.plugin.debug_log and added:
                logger.info(f"[real_diary] 状态变化已记：{prefix}（{stream_id[:8]}）")

        seen[stream_id] = {"phase": phase, "at": time.time()}

    if changes:
        await state.save_runtime({"phases": seen}, force=True)
    elif seen != (runtime.get("phases") or {}):
        # 首次采样即使没「变化」也要把基线落盘，否则每次启动都会重记一遍。
        await state.save_runtime({"phases": seen}, force=False)
    return changes


async def poll_once(config: RealDiaryConfig, *, now: float | None = None) -> dict[str, Any]:
    """一次巡检：抓今天与昨天 + 采样状态。

    只抓「今天」和「昨天」两天的兜底量——更早的日子由生成流程按需单独抓
    （见 composer），避免每次巡检都去扫一堆历史。

    Args:
        config: 插件配置。
        now: 参考时刻（测试注入用）。

    Returns:
        统计：``{today, yesterday, phases}``。
    """
    moment = float(now if now is not None else time.time())
    today = state.date_of(moment)
    yesterday = state.shift_date(today, -1)

    result: dict[str, Any] = {"today": today, "yesterday": yesterday, "phases": 0}
    if yesterday:
        await ingest_for_day(config, yesterday, reason="poll")
    if today:
        result["detail"] = await ingest_for_day(config, today, reason="poll")
    result["phases"] = await sample_phases(config)

    await state.save_runtime({"last_poll_at": moment}, force=False)
    return result


async def pending_days(config: RealDiaryConfig) -> list[str]:
    """列出「有素材但还没写过日记」的日期（升序）。

    判据只看 ``done`` 留档，不看文件在不在——输出目录被手动挪走也不会导致重写。

    Args:
        config: 插件配置。

    Returns:
        日期列表。
    """
    days = await state.list_days()
    pending: list[str] = []
    for day in days:
        if await state.get_done_record(day) is None:
            pending.append(day)
    return pending


__all__ = [
    "ingest_archive_event",
    "ingest_for_day",
    "item_from_archive_payload",
    "pending_days",
    "poll_once",
    "sample_phases",
]
