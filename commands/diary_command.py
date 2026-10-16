"""``/日记`` 命令：看清素材、手动生成、丢弃残缺的一天。

仅主人可用（``PermissionLevel.OWNER``）——它会触发真实的模型调用与文件写入。

```
/日记                    状态总览
/日记 状态 /status       同上
/日记 素材 [日期]         某天的素材清单（核对质量用）
/日记 演练 [日期]         只调模型看正文，不落盘、不删素材
/日记 现在 [日期]         立刻生成（默认昨天）
/日记 重写 [日期]         抹掉留档后重新生成（会覆盖已有文件）
/日记 丢弃 [日期]         丢弃某天的素材，**不生成**
/日记 检查               跑一次幂等检查（缺就补）
/日记 observer|active    切换运行模式（只改运行时，重启回配置值）
/日记 帮助
```

日期参数留空 = 昨天，也认「今天 / 昨天」两个词。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from .. import collector, composer, state
from ..config import RealDiaryConfig

logger = get_logger("real_diary.command")

_HELP = """【真实日记】
/日记                状态总览
/日记 状态           同上
/日记 素材 [日期]     某天的素材清单（默认昨天）
/日记 演练 [日期]     只调模型看正文，不落盘
/日记 现在 [日期]     立刻生成（默认昨天）
/日记 重写 [日期]     抹掉留档后重新生成（会覆盖原文件）
/日记 丢弃 [日期]     丢弃某天素材，不生成
/日记 检查           跑一次幂等检查（缺就补）
/日记 observer       只采集不写日记
/日记 active         到点就写日记
/日记 帮助           本帮助

日期可以写 2026-10-14，也可以写「今天 / 昨天」。"""


def _resolve_date(raw: str) -> str:
    """把命令参数解析成日期；非法返回空串。"""
    text = " ".join(str(raw or "").split()).lower()
    if not text or text in ("昨天", "yesterday", "昨"):
        return state.shift_date(state.today_str(), -1)
    if text in ("今天", "today", "今"):
        return state.today_str()
    if state.is_valid_date(text):
        return text
    return ""


class DiaryCommand(BaseCommand):
    """真实日记命令。"""

    name: str = "日记"
    description: str = "真实日记：查看素材、立刻生成、丢弃残缺的一天"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名（``日记`` / ``diary``）。"""
        if not parts:
            return 0
        if parts[0] in ("日记", "diary"):
            return 1
        return 0

    # ── 内部工具 ────────────────────────────────────────────────────────────

    def _config(self) -> RealDiaryConfig | None:
        """取插件配置。"""
        config = getattr(self.plugin, "config", None)
        return config if isinstance(config, RealDiaryConfig) else None

    async def _reply(self, text: str) -> None:
        """向当前聊天流回话。"""
        await send_text(text, stream_id=self.stream_id)

    def _mode(self, config: RealDiaryConfig) -> str:
        """当前运行模式。"""
        return str(config.plugin.mode or "observer").lower()

    # ── 路由 ────────────────────────────────────────────────────────────────

    @cmd_route()
    async def handle_root(self) -> tuple[bool, str]:
        """状态总览（默认路由）。"""
        return await self.handle_status()

    @cmd_route("状态")
    async def handle_status(self) -> tuple[bool, str]:
        """状态总览。"""
        config = self._config()
        if config is None:
            await self._reply("插件配置不可用。")
            return False, "no config"

        today = state.today_str()
        yesterday = state.shift_date(today, -1)
        runtime = await state.load_runtime(fresh=True)
        done = await state.load_done(fresh=True)
        pending = await collector.pending_days(config)

        lines = [
            "【真实日记】",
            f"模式：{self._mode(config)}"
            f"{'（只采集，不写文件）' if self._mode(config) != 'active' else ''}",
            f"输出目录：{composer.output_dir(config)}",
            f"素材命名空间：{state.store_name()}",
        ]
        for label, day in (("今天", today), ("昨天", yesterday)):
            if not day:
                continue
            items = await state.load_day(day, fresh=True)
            count = len(items.get("items") or [])
            record = (done.get("days") or {}).get(day)
            mark = "已写" if record else "未写"
            lines.append(f"{label}（{day}）：素材 {count} 条 · {mark}")
        if pending:
            lines.append("待处理：" + "、".join(pending[-5:]))
        else:
            lines.append("待处理：无")

        last_gen = float(runtime.get("last_generate_at") or 0.0)
        if last_gen:
            lines.append(
                f"上次生成：{state.date_of(last_gen)} {state.clock_of(last_gen)}"
                f"（{runtime.get('last_generate_date') or '-'}）"
            )
        streak = int(runtime.get("fail_streak") or 0)
        if streak:
            lines.append(f"⚠ 连续失败 {streak} 次：{runtime.get('last_error') or '-'}")
        await self._reply("\n".join(lines))
        return True, "status"

    @cmd_route("status")
    async def handle_status_en(self) -> tuple[bool, str]:
        """状态总览（英文别名）。"""
        return await self.handle_status()

    @cmd_route("素材")
    async def handle_items(self, date: str = "") -> tuple[bool, str]:
        """看某天的素材清单。"""
        config = self._config()
        target = _resolve_date(date)
        if config is None or not target:
            await self._reply("日期不合法（写成 2026-10-14，或写「昨天」）。")
            return False, "bad date"

        # 顺手做一次兜底抓取，让清单反映「能抓到的全部」
        if self._mode(config) == "active":
            await collector.ingest_for_day(config, target, reason="command")

        day = await state.load_day(target, fresh=True)
        items = sorted(day.get("items") or [], key=lambda entry: float(entry.get("at") or 0.0))
        if not items:
            await self._reply(f"{target} 没有任何素材。")
            return True, "empty"

        verdict = composer.assess_day(
            items,
            min_items=int(config.generate.min_items),
            gap_max_hours=float(config.generate.gap_max_hours),
            date_str=target,
            coverage_min_hours=float(config.generate.coverage_min_hours),
        )
        lines = [
            f"【{target} 素材】共 {len(items)} 条 · "
            f"{'完整' if verdict['complete'] else '不完整（' + '；'.join(verdict['reasons']) + '）'}"
        ]
        for item in items[-30:]:
            clock = state.clock_of(float(item.get("at") or 0.0)) or "--:--"
            source = str(item.get("source") or "")
            text = " ".join(str(item.get("text") or "").split())[:60]
            lines.append(f"{clock} [{source}] {text}")
        if len(items) > 30:
            lines.append("（只显示最近 30 条，更早的省略）")
        await self._reply("\n".join(lines))
        return True, "items"

    @cmd_route("items")
    async def handle_items_en(self, date: str = "") -> tuple[bool, str]:
        """素材清单（英文别名）。"""
        return await self.handle_items(date)

    @cmd_route("演练")
    async def handle_dry_run(self, date: str = "") -> tuple[bool, str]:
        """只调模型看正文，不落盘、不删素材。"""
        config = self._config()
        target = _resolve_date(date)
        if config is None or not target:
            await self._reply("日期不合法。")
            return False, "bad date"
        await self._reply(f"正在演练 {target} 的日记（只调模型，不落盘）…")
        result = await composer.compose_day(config, target, dry_run=True, force=True)
        if result.get("status") != "dry_run":
            await self._reply(f"演练未完成：{result.get('reason') or result.get('status')}")
            return False, str(result.get("status"))
        preview = str(result.get("preview") or "")
        await self._reply(
            f"【演练】{target} · 素材 {result.get('items')} 条 · "
            f"{'完整' if result.get('complete') else '不完整'} · 模型 {result.get('model')}\n\n"
            f"{preview[:1500]}"
        )
        return True, "dry_run"

    @cmd_route("dry")
    async def handle_dry_run_en(self, date: str = "") -> tuple[bool, str]:
        """演练（英文别名）。"""
        return await self.handle_dry_run(date)

    @cmd_route("现在")
    async def handle_now(self, date: str = "") -> tuple[bool, str]:
        """立刻生成（忽略窗口限制）。"""
        config = self._config()
        target = _resolve_date(date)
        if config is None or not target:
            await self._reply("日期不合法。")
            return False, "bad date"
        if self._mode(config) != "active":
            await self._reply(
                "当前是 observer 模式，不会写文件。\n"
                "先执行 /日记 active（只改运行时，重启后回到配置值）。"
            )
            return False, "observer"
        await self._reply(f"正在写 {target} 的日记…")
        result = await composer.compose_day(config, target, force=True)
        await self._reply(_format_result(result))
        return result.get("status") == "written", str(result.get("status"))

    @cmd_route("now")
    async def handle_now_en(self, date: str = "") -> tuple[bool, str]:
        """立刻生成（英文别名）。"""
        return await self.handle_now(date)

    @cmd_route("重写")
    async def handle_rewrite(self, date: str = "") -> tuple[bool, str]:
        """抹掉留档后重新生成（会覆盖已有文件）。"""
        config = self._config()
        target = _resolve_date(date)
        if config is None or not target:
            await self._reply("日期不合法。")
            return False, "bad date"
        if self._mode(config) != "active":
            await self._reply("当前是 observer 模式，不会写文件。")
            return False, "observer"
        await state.forget_done(target)
        await self._reply(f"已抹掉 {target} 的留档，重新生成…")
        result = await composer.compose_day(config, target, force=True)
        await self._reply(_format_result(result))
        return result.get("status") == "written", str(result.get("status"))

    @cmd_route("rewrite")
    async def handle_rewrite_en(self, date: str = "") -> tuple[bool, str]:
        """重写（英文别名）。"""
        return await self.handle_rewrite(date)

    @cmd_route("丢弃")
    async def handle_discard(self, date: str = "") -> tuple[bool, str]:
        """丢弃某天的素材（不生成）。"""
        target = _resolve_date(date)
        if not target:
            await self._reply("日期不合法。")
            return False, "bad date"
        day = await state.load_day(target, fresh=True)
        count = len(day.get("items") or [])
        if count == 0:
            await self._reply(f"{target} 没有素材可丢。")
            return True, "empty"
        deleted = await composer.discard_day(self._config(), target, reason="手动丢弃")
        await self._reply(
            f"{'已丢弃' if deleted else '丢弃失败'}：{target} 的 {count} 条素材（不会生成日记）"
        )
        return deleted, "discarded"

    @cmd_route("discard")
    async def handle_discard_en(self, date: str = "") -> tuple[bool, str]:
        """丢弃（英文别名）。"""
        return await self.handle_discard(date)

    @cmd_route("检查")
    async def handle_check(self) -> tuple[bool, str]:
        """跑一次幂等检查（缺就补）。"""
        config = self._config()
        if config is None:
            await self._reply("插件配置不可用。")
            return False, "no config"
        await self._reply("正在检查「该写但还没写」的日子…")
        results = await composer.run_check(config, reason="command")
        if not results:
            await self._reply("检查完成：没有需要处理的日期。")
            return True, "clean"
        lines = ["检查结果："]
        for item in results:
            status = item.get("status")
            day = item.get("date") or "-"
            if status == "written":
                lines.append(f"✓ {day} 已写 → {item.get('path')}")
            elif status == "observer":
                lines.append(f"· {day} 待写（observer 模式，素材 {item.get('items')} 条）")
            elif status == "discarded":
                lines.append(f"· {day} 已丢弃素材（{item.get('reason')}）")
            else:
                lines.append(f"· {day} {status}：{item.get('reason') or ''}")
        await self._reply("\n".join(lines))
        return True, "checked"

    @cmd_route("check")
    async def handle_check_en(self) -> tuple[bool, str]:
        """检查（英文别名）。"""
        return await self.handle_check()

    @cmd_route("observer")
    async def handle_set_observer(self) -> tuple[bool, str]:
        """切到只采集模式。"""
        config = self._config()
        if config is None:
            return False, "no config"
        config.plugin.mode = "observer"
        await self._reply("✓ 已切到 observer（只采集素材，不写日记文件）")
        logger.info("[real_diary] 运行时模式切换为 observer")
        return True, "observer"

    @cmd_route("active")
    async def handle_set_active(self) -> tuple[bool, str]:
        """切到执行模式。"""
        config = self._config()
        if config is None:
            return False, "no config"
        config.plugin.mode = "active"
        raw_dir = str(config.plugin.diary_dir or "").strip() or "data/diary"
        await self._reply(
            "✓ 已切到 active（到点检查，缺日记就生成）\n"
            f"输出目录：{raw_dir}（相对路径按实例根目录）\n"
            f"回补窗口：{int(config.generate.backfill_days)} 天；"
            f"清理素材：{'开' if config.cleanup.delete_fragments else '关'}\n"
            "注意：本次切换只改运行时，重启后回到 config.toml 里的值。"
        )
        logger.info("[real_diary] 运行时模式切换为 active")
        return True, "active"

    @cmd_route("帮助")
    async def handle_help(self) -> tuple[bool, str]:
        """帮助。"""
        await self._reply(_HELP)
        return True, "help"

    @cmd_route("help")
    async def handle_help_en(self) -> tuple[bool, str]:
        """帮助（英文别名）。"""
        return await self.handle_help()


def _format_result(result: dict[str, Any]) -> str:
    """把生成结果渲染成一句回话。"""
    status = str(result.get("status") or "")
    day = str(result.get("date") or "-")
    if status == "written":
        return (
            f"✓ {day} 的日记已写：{result.get('path')}\n"
            f"素材 {result.get('items')} 条 · "
            f"{'完整' if result.get('complete') else '不完整'} · "
            f"模型 {result.get('model')}"
            f"{' · 素材碎片已删' if result.get('deleted') else ''}"
        )
    if status == "skipped":
        return f"{day} 跳过：{result.get('reason')}"
    if status == "failed":
        return f"{day} 生成失败：{result.get('reason')}（素材保留，下次再试）"
    return f"{day} 结果：{status} {result.get('reason') or ''}"


__all__ = ["DiaryCommand"]
