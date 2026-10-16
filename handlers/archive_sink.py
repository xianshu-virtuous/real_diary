"""订阅上下文归档事件：归档成功**立刻**把摘要收成日记素材。

这是主人要的「归档即联动」——不用等下一次巡检。

事件契约（由 ``context_archiver`` 定义、只做加法）：
``stream_id / at / start_ts / end_ts / message_count / summary / memory_ids /
sink / cleared / truncated / trigger / error``。

失败路径不会发这条事件（那边水位线没推进、下次会重新总结），所以这里收到
的一定是「已经写进记忆的那一段」。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from .. import collector
from ..config import RealDiaryConfig

logger = get_logger("real_diary.archive_sink")

#: 订阅的事件名（自定义字符串事件，框架支持任意字符串）。
ARCHIVED_EVENT = "context_archiver:archived"


class ArchiveSinkHandler(BaseEventHandler):
    """把归档结果收成当天的日记素材。"""

    name: str = "archive_sink"
    description: str = "订阅上下文归档事件，把归档摘要立刻收成日记素材"
    weight: int = 30
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [ARCHIVED_EVENT]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理归档事件。

        Args:
            event_name: 事件名。
            params: 事件参数（原样返回，key 集合不能变——框架硬规则）。

        Returns:
            ``(EventDecision.SUCCESS, params)``。
        """
        config = self.plugin.config
        if not isinstance(config, RealDiaryConfig):
            return EventDecision.SUCCESS, params
        if not config.plugin.enabled or not config.collect.archive_events:
            return EventDecision.SUCCESS, params

        try:
            await collector.ingest_archive_event(config, params)
        except Exception as error:  # noqa: BLE001 - 采集失败绝不影响归档方
            logger.warning(f"[real_diary] 收取归档事件失败: {error}")

        return EventDecision.SUCCESS, params


__all__ = ["ARCHIVED_EVENT", "ArchiveSinkHandler"]
