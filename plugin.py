"""real_diary 插件入口。

真实日记：把**当天真实发生过的事**写成日记。

- **采集**：事件（``context_archiver:archived``）+ 巡检兜底 + 状态阶段采样，
  素材落到 ``data/json_storage/<namespace>/day-YYYY-MM-DD``；
- **生成**：幂等检查——启动时、以及每天 ``generate.check_time``（默认 02:00）
  各查一次「昨天写了没」，没写就补；写不成（缺素材/超窗口）就不写，
  **绝不虚构**；
- **清理**：日记落盘成功后，只删**自己抓来的素材碎片**；
  booku 记忆库与任何外部插件的数据一个字不动；
- **默认 observer**：先只看几天，确认素材质量再切 ``active``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.concurrency import get_task_manager
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from . import collector, composer, state
from .commands import DiaryCommand
from .config import RealDiaryConfig
from .handlers import ArchiveSinkHandler, PhaseWatcherHandler

logger = get_logger("real_diary")

#: 巡检任务的调度器任务名。
_TICK_TASK_NAME = "real_diary_tick"

#: 巡检间隔下限（秒）。
_MIN_TICK_SECONDS = 30

#: 等调度器就绪的最大轮数（每轮 0.5s）。
_SCHEDULER_WAIT_ROUNDS = 600


def _tick_interval(config: Any) -> int:
    """读巡检间隔（秒），非法值回退 300，且不低于下限。"""
    raw: Any = 300
    collect = getattr(config, "collect", None)
    if collect is not None:
        raw = getattr(collect, "poll_seconds", raw)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = 300
    return max(_MIN_TICK_SECONDS, seconds)


def _daily_check_due(config: Any, runtime: dict[str, Any], now: float) -> bool:
    """今天是否已经过了 ``check_time``、且今天还没检查过。

    这是「每天 02:00 检查一次」的实现：不额外注册一个定时任务，而是让巡检
    每隔几分钟看一眼——**只要进程在跑，那一天一定会被检查到一次**，
    哪怕 02:00 那一刻正好在重启。

    Args:
        config: 插件配置。
        runtime: 运行水位。
        now: 当前时刻。

    Returns:
        该不该现在做每日检查。
    """
    raw = str(getattr(getattr(config, "generate", None), "check_time", "") or "").strip()
    if not raw:
        return False
    try:
        hour_text, minute_text = raw.split(":", 1)
        target_minute = int(hour_text) * 60 + int(minute_text)
    except (TypeError, ValueError):
        return False

    local = time.localtime(now)
    now_minute = local.tm_hour * 60 + local.tm_min
    if now_minute < target_minute:
        return False
    return str(runtime.get("last_daily_check_date") or "") != state.date_of(now)


@register_plugin
class RealDiaryPlugin(BasePlugin):
    """真实日记插件。"""

    plugin_name: str = "real_diary"
    plugin_description: str = (
        "把当天真实发生过的事写成日记：采集归档摘要/每日短期记忆/记忆索引/状态阶段，"
        "跨天用指定模型写成第一人称日记并输出到指定目录；幂等补写、残缺不虚构"
    )
    plugin_version: str = "1.0.0"
    configs: list[type] = [RealDiaryConfig]

    def __init__(self, config: Any = None) -> None:
        """初始化插件。

        Args:
            config: 插件配置实例。
        """
        super().__init__(config)
        self._schedule_ids: list[str] = []
        self._register_task_id: str | None = None
        self._startup_task_id: str | None = None

    def get_components(self) -> list[type]:
        """返回本插件提供的组件；插件关闭时整体下线。"""
        if isinstance(self.config, RealDiaryConfig) and not self.config.plugin.enabled:
            return []
        return [ArchiveSinkHandler, PhaseWatcherHandler, DiaryCommand]

    # ── 生命周期 ────────────────────────────────────────────────────────────

    async def on_plugin_loaded(self) -> None:
        """加载后：定命名空间 → 排队启动检查 → 排队注册巡检。"""
        config = self.config
        if isinstance(config, RealDiaryConfig) and not config.plugin.enabled:
            return

        if isinstance(config, RealDiaryConfig):
            namespace = state.configure(config.storage.namespace)
            logger.info(
                f"[real_diary] 素材命名空间 {namespace}；模式 {config.plugin.mode}；"
                f"输出目录 {config.plugin.diary_dir or 'data/diary'}"
            )

        # 启动检查放到后台任务：它会（在 active 模式下）调模型，不能拖慢加载。
        if not isinstance(config, RealDiaryConfig) or config.generate.check_on_start:
            try:
                task_info = get_task_manager().create_task(
                    self._startup_check(),
                    name="real_diary_startup_check",
                    daemon=True,
                )
                self._startup_task_id = task_info.task_id
            except Exception as error:  # noqa: BLE001 - 检查失败只影响补写
                logger.warning(f"[real_diary] 排队启动检查失败: {error}")
        else:
            logger.info("[real_diary] check_on_start 已关闭，跳过启动检查")

        try:
            task_info = get_task_manager().create_task(
                self._register_tick_when_ready(),
                name="real_diary_tick_register",
                daemon=True,
            )
            self._register_task_id = task_info.task_id
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[real_diary] 排队注册巡检失败: {error}")

    async def on_plugin_unloaded(self) -> None:
        """卸载前：取消后台任务并移除巡检调度。"""
        for attribute in ("_startup_task_id", "_register_task_id"):
            task_id = getattr(self, attribute, None)
            if not task_id:
                continue
            try:
                get_task_manager().cancel_task(task_id)
            except Exception:  # noqa: BLE001 - 任务可能已结束
                pass
            setattr(self, attribute, None)

        schedule_ids = list(self._schedule_ids)
        self._schedule_ids.clear()

        try:
            scheduler = get_unified_scheduler()
        except Exception:  # noqa: BLE001 - 调度器没起来就不用清理
            scheduler = None
        if scheduler is None:
            return

        try:
            found = await scheduler.find_schedule_by_name(_TICK_TASK_NAME)
        except Exception:  # noqa: BLE001
            found = None
        if found and found not in schedule_ids:
            schedule_ids.append(found)

        for schedule_id in schedule_ids:
            try:
                await scheduler.remove_schedule(schedule_id)
            except Exception as error:  # noqa: BLE001 - 卸载阶段尽力清理
                logger.debug(f"[real_diary] 移除巡检任务失败: {error}")

    # ── 启动检查 ────────────────────────────────────────────────────────────

    async def _startup_check(self) -> None:
        """启动时补写：昨天没写就立刻写（这是「缺则补」的主通路）。"""
        config = self.config
        if not isinstance(config, RealDiaryConfig):
            return
        try:
            results = await composer.run_check(config, reason="startup")
        except Exception as error:  # noqa: BLE001 - 补写失败不影响 bot 启动
            logger.error(f"[real_diary] 启动检查异常: {error}")
            return

        if not results:
            logger.info("[real_diary] 启动检查：该写的都已写好")
            return
        for item in results:
            status = str(item.get("status") or "")
            day = str(item.get("date") or "-")
            if status == "written":
                logger.info(f"[real_diary] 启动补写完成：{day} → {item.get('path')}")
            elif status == "observer":
                logger.info(
                    f"[real_diary] 启动检查：{day} 待写（素材 {item.get('items')} 条，"
                    f"当前 observer 模式，不写文件）"
                )
            elif status == "discarded":
                logger.info(f"[real_diary] {day} 素材已丢弃：{item.get('reason')}")
            else:
                logger.info(f"[real_diary] 启动检查 {day}：{status} {item.get('reason') or ''}")

    # ── 巡检 ────────────────────────────────────────────────────────────────

    async def _register_tick_when_ready(self) -> None:
        """等调度器就绪后注册周期巡检。"""
        interval = _tick_interval(self.config)
        try:
            scheduler = get_unified_scheduler()
        except Exception as error:  # noqa: BLE001 - 拿不到调度器就别注册
            logger.warning(f"[real_diary] 调度器不可用，巡检未注册: {error}")
            return

        for _ in range(_SCHEDULER_WAIT_ROUNDS):
            try:
                schedule_id = await scheduler.create_schedule(
                    callback=self._tick_job,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": interval},
                    is_recurring=True,
                    task_name=_TICK_TASK_NAME,
                    force_overwrite=True,
                )
            except RuntimeError:
                await asyncio.sleep(0.5)
                continue
            except Exception as error:  # noqa: BLE001 - 反复失败就放弃注册
                logger.warning(f"[real_diary] 注册巡检任务失败: {error}")
                await asyncio.sleep(2.0)
                continue

            self._schedule_ids = [schedule_id]
            mode = getattr(getattr(self.config, "plugin", None), "mode", "observer")
            logger.info(f"[real_diary] 巡检已注册（每 {interval}s，模式 {mode}）")
            return

        logger.warning("[real_diary] 等待调度器就绪超时，巡检未注册")

    async def _tick_job(self) -> None:
        """调度器回调：采集一轮 → 看看要不要做每日检查。"""
        config = self.config
        if not isinstance(config, RealDiaryConfig):
            return
        if not config.plugin.enabled:
            return

        try:
            await collector.poll_once(config)
        except Exception as error:  # noqa: BLE001 - 巡检失败不打断调度
            logger.error(f"[real_diary] 巡检异常: {error}")

        try:
            runtime = await state.load_runtime(fresh=True)
            now = time.time()
            if not _daily_check_due(config, runtime, now):
                return
            await state.save_runtime(
                {"last_daily_check_date": state.date_of(now)},
                force=True,
            )
            results = await composer.run_check(config, now=now, reason="daily")
            for item in results:
                status = str(item.get("status") or "")
                day = str(item.get("date") or "-")
                if status == "written":
                    logger.info(f"[real_diary] 每日检查补写完成：{day} → {item.get('path')}")
                elif status == "observer":
                    logger.info(
                        f"[real_diary][observer] {day} 待写（素材 {item.get('items')} 条）"
                    )
                elif status in ("discarded", "skipped", "failed", "backoff"):
                    logger.info(f"[real_diary] 每日检查 {day}：{status} {item.get('reason') or ''}")
        except Exception as error:  # noqa: BLE001 - 每日检查失败不影响下一次巡检
            logger.error(f"[real_diary] 每日检查异常: {error}")


__all__ = ["RealDiaryPlugin"]
