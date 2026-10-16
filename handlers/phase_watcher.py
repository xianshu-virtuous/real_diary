"""状态阶段采样：读快照 + 自己比对，把「阶段变了」记成日记素材。

为什么这么绕：``sisp`` / ``deep_neo_sisp`` **不发布阶段转变事件、也不保留转变
历史**（只有当前值两个 JSON），所以唯一可行的路径就是本插件自己采样比对。

**权重要小于 20**：sisp 的状态处理器挂在 ``on_message_received`` 上、weight=20，
事件按权重**降序**执行，所以 weight=10 保证本处理器跑在它之后，读到的是
「刚推进完」的新值。

**绝不回写 sisp 的存储**（那是别人的状态机，回写会吃掉它还没消费的
``current_event`` / ``spontaneous_event``，还会和它并发写打架）。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from .. import collector
from ..config import RealDiaryConfig

logger = get_logger("real_diary.phase_watcher")

#: 两次采样之间的最小间隔（秒）：每条消息都读盘没必要，阶段也不会秒变。
_MIN_SAMPLE_INTERVAL = 20.0


class PhaseWatcherHandler(BaseEventHandler):
    """按消息事件节流采样状态阶段。"""

    name: str = "phase_watcher"
    description: str = "采样 sisp 状态阶段，把阶段变化记成日记素材"
    weight: int = 10
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_RECEIVED]

    #: 上次采样时刻（单调钟）。挂**类属性**：框架每次取服务/组件都可能新建实例，
    #: 实例属性起不到节流作用。
    _last_sample_mono: float = -1.0e9

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理消息事件（只做一次便宜的读盘 + 比对）。

        Args:
            event_name: 事件名。
            params: 事件参数（原样返回）。

        Returns:
            ``(EventDecision.SUCCESS, params)``。
        """
        config = self.plugin.config
        if not isinstance(config, RealDiaryConfig):
            return EventDecision.SUCCESS, params
        if not config.plugin.enabled or not config.collect.state_phases:
            return EventDecision.SUCCESS, params

        now = time.monotonic()
        cls = type(self)
        if (now - cls._last_sample_mono) < _MIN_SAMPLE_INTERVAL:
            return EventDecision.SUCCESS, params
        cls._last_sample_mono = now

        try:
            await collector.sample_phases(config)
        except Exception as error:  # noqa: BLE001 - 采样失败绝不影响对话
            logger.debug(f"[real_diary] 状态采样失败: {error}")

        return EventDecision.SUCCESS, params


__all__ = ["PhaseWatcherHandler"]
