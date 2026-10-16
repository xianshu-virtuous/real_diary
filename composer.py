"""real_diary 的生成链路。

一条链路（顺序是刻意的）：

1. **兜底抓取**：为这一天再抓一次素材（事件通路可能漏了）；
2. **窗口判定**：只认「昨天」以及 ``backfill_days`` 以内的日子。更早的
   **直接丢弃素材、不生成**——崩溃跨了好几天，那几天的记录必然是残缺的，
   **宁可空着也不编**（这是主人定的硬口径）；
3. **完整度判定**：素材条数太少、或相邻素材间隔超过 ``gap_max_hours``，
   判「不完整」；窗口内照写，但正文里**如实说明**；
4. **写**：组装 prompt → 调指定模型 → 清洗 → **原子写盘**；
5. **登记**：写成功才进 ``done`` 留档（"这天写过没"的判据）；
6. **清理**：**落盘成功后**才删掉当天的素材碎片。

第 4 步失败一律**不删素材、不登记**，退避到下次检查再试。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from . import collector, llm, state
from .config import RealDiaryConfig

logger = get_logger("real_diary.composer")

#: 素材类型在「预算裁剪」时的优先级：数值越大越舍不得丢。
_KIND_PRIORITY = {
    "daily": 3,
    "state": 2,
    "archive": 2,
    "timeline": 1,
    "memory": 1,
    "note": 0,
}

#: 代码围栏（模型爱把正文包在 ``` 里）。
_FENCE_PATTERN = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)

#: 模型自己加的「标题行」形态（我们会自己写标题，避免重复）。
_LEADING_TITLE_PATTERN = re.compile(r"^\s*#+\s*.{0,40}\n+")

#: 「用安静填补没记录的时间」的句式（事后体检用，见 :func:`spot_blank_filling`）。
_BLANK_FILL_PATTERNS = (
    r"白天.{0,8}(安静|没什么|没事|没动静|冷清)",
    r"(一整天|全天).{0,8}(安静|没什么|没事|没动静|平淡)",
    r"(上午|早上|下午).{0,8}(安静|没什么|没事|没动静|冷清)",
    r"(安静|平静|平淡)的一天",
    r"没什么事(发生|可写|好写)",
)

#: 正文里的裸数字 ID（≥6 位）：群成员代号 / QQ 号片段。
#: 阈值定 6 位：覆盖常见 QQ 号，又不会把「4/5」「2026」这种短数字算进来。
#: **它是体检不是清洗**——「143390 分」这类长数字也会被列出来，这是**刻意**的取舍：
#: 自动删数字会误伤真实数据（分数、编号），提醒一句的成本低得多。
_BARE_ID_PATTERN = re.compile(r"(?<!\d)\d{6,}(?!\d)")

_SYSTEM_PROMPT = """你在替一个人写「当天的日记」。你只写真实发生过的事。

硬性要求：

1. 第一人称，语气自然、口语化，像晚上自己回想这一天，而不是在汇报；
2. **只写素材里出现过的事**——素材没提到的人物、事件、地点、数字，
   一个字都不许编；想不起来的部分就跳过；
3. 时间拿不准就别写具体时刻（「下午」「后来」可以，「15:23」要有素材依据）；
4. 不要出现「根据素材」「记录显示」「今天共有 N 条」这类元话术，也不要罗列条目；
5. 不要照抄素材原文，用自己的话把这一天串成一段连贯的回忆；
6. 篇幅以素材为准：素材多就多写，素材少就短，**绝不为凑字数注水**；
7. **没有记录的时间段就整段跳过**：不要用「白天一直很安静」「没什么事发生」
   「一整天都在忙别的」这类话去填补素材没覆盖的时间——**那是编的**，
   和编造事件一样严重；
8. **整篇只有一个「我」，就是你**：
   · 素材里出现「bot」「机器人」，说的就是你，写的时候用「我」；
   · 素材里**别人**说的「我」（别人的话、别人自己的回忆、别人眼里的你）**不是你的视角**，
     要写成「他问我…」「他说…」；
   · 素材里出现的**纯数字**（例如 482063、995859）是群成员的代号，**不要原样写进日记**——
     用素材里已有的昵称称呼他们，实在没有昵称就写「有个群友」；
   · 群聊与私聊都要写，但**始终是同一个你在回忆**，别只挑一个场景；
9. **素材是客观记录，不是你的回忆**：它们可能是别人的话、别人的视角，
   甚至是别人对你说的话、别人眼里的你。请把它们**转写成你自己的第一人称回忆**，
   **不要把素材里的「我」当成你的视角**——整篇日记只有一个「我」，就是你自己；
10. 直接输出日记正文（markdown 段落），不要大写标题，不要输出 JSON。"""


# --------------------------------------------------------------------------- #
# 完整度 / 渲染
# --------------------------------------------------------------------------- #


def render_item_line(item: dict[str, Any]) -> str:
    """把一条素材渲染成一行（按时刻升序排列用）。"""
    clock = state.clock_of(float(item.get("at") or 0.0)) or "--:--"
    kind = str(item.get("kind") or "")
    return f"{clock} [{kind}] {item.get('text') or ''}"


def assess_day(
    items: list[dict[str, Any]],
    *,
    min_items: int = 3,
    gap_max_hours: float = 6.0,
    date_str: str = "",
    coverage_min_hours: float = 0.0,
) -> dict[str, Any]:
    """判断这一天的素材是否「完整」。

    判据有三条（简单、可解释）：

    - 条数少于 ``min_items``；
    - 相邻素材的最大间隔超过 ``gap_max_hours``（进程当时多半根本没在跑）；
    - **有记录的时间跨度**短于 ``coverage_min_hours``（只有一小段有记录）。

    第三条是 2026-10-16 虚拟真机测试抓出来的：只看前两条时，「只有晚上一小时有记录」
    会被判成完整的一天，模型于是顺手把没记录的上午写成「白天一直很安静」——**那是编的**。

    Args:
        items: 当天素材（会被按时刻排序）。
        min_items: 最少条数。
        gap_max_hours: 最大允许间隔（小时）。
        date_str: 目标日期（用于把跨度与「一整天」对照，仅用于日志/诊断）。
        coverage_min_hours: 有记录的时间跨度下限（小时）；``0`` 表示关掉这条判据。

    Returns:
        ``{"complete", "reasons", "count", "first_at", "last_at", "max_gap_hours", "span_hours", "items"}``。
    """
    ordered = sorted(
        (dict(item) for item in items or []),
        key=lambda entry: float(entry.get("at") or 0.0),
    )
    count = len(ordered)
    reasons: list[str] = []

    if count < max(1, int(min_items)):
        reasons.append(f"素材太少（{count} 条 < {int(min_items)} 条）")

    stamps = [float(entry.get("at") or 0.0) for entry in ordered]
    max_gap = 0.0
    for index in range(1, len(stamps)):
        gap = stamps[index] - stamps[index - 1]
        if gap > max_gap:
            max_gap = gap
    if stamps and (max_gap / 3600.0) > float(gap_max_hours):
        reasons.append(
            f"中间有 {(max_gap / 3600.0):.1f} 小时没有记录（上限 {float(gap_max_hours):.1f} 小时）"
        )

    first_at = stamps[0] if stamps else 0.0
    last_at = stamps[-1] if stamps else 0.0
    span_hours = (last_at - first_at) / 3600.0 if stamps else 0.0

    if coverage_min_hours > 0 and stamps and span_hours < float(coverage_min_hours):
        where = f"{state.clock_of(first_at)} ~ {state.clock_of(last_at)}"
        reasons.append(
            f"当天只有 {span_hours:.1f} 小时有记录（{where}，不足 "
            f"{float(coverage_min_hours):.1f} 小时）——其余时间没有记录，别当作「什么都没发生」"
        )

    return {
        "complete": not reasons,
        "reasons": reasons,
        "count": count,
        "first_at": first_at,
        "last_at": last_at,
        "max_gap_hours": max_gap / 3600.0,
        "span_hours": span_hours,
        "items": ordered,
    }


def fit_items(
    items: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """按预算裁剪素材文本（超限时先丢低优先级、且更早的流水）。

    Args:
        items: 素材（已按时刻升序）。
        limit: 文本字符上限。

    Returns:
        ``(保留的素材, 丢掉的条数)``。
    """
    if limit <= 0:
        return list(items), 0
    rendered = {id(item): len(render_item_line(item)) for item in items}
    total = sum(rendered.values())
    if total <= limit:
        return list(items), 0

    # 低优先级优先丢；同优先级里先丢更早的（越早的越可能被后来的覆盖）
    order = sorted(
        items,
        key=lambda entry: (
            _KIND_PRIORITY.get(str(entry.get("kind") or ""), 1),
            float(entry.get("at") or 0.0),
        ),
    )
    dropped: set[int] = set()
    for item in order:
        if total <= limit:
            break
        key = id(item)
        dropped.add(key)
        total -= rendered.get(key, 0)
    kept = [item for item in items if id(item) not in dropped]
    return kept, len(dropped)


def build_prompt(
    config: RealDiaryConfig,
    date_str: str,
    items: list[dict[str, Any]],
    *,
    verdict: dict[str, Any],
    now: float | None = None,
) -> tuple[str, str, int]:
    """组装 ``(system, user, 丢弃条数)``。"""
    moment = float(now if now is not None else time.time())
    weekday = state.weekday_cn(date_str)
    keep, dropped = fit_items(items, limit=int(config.generate.max_prompt_chars))

    lines: list[str] = []
    lines.append(f"日期：{date_str}（{weekday}）")
    if verdict.get("complete"):
        lines.append("素材完整度：完整")
    else:
        lines.append(
            "素材完整度：**不完整**（" + "；".join(verdict.get("reasons") or []) + "）"
        )
        lines.append("→ 请照实写，缺的部分跳过；不要为了把一天写圆而补细节。")

    first_at = float(verdict.get("first_at") or 0.0)
    last_at = float(verdict.get("last_at") or 0.0)
    if first_at and last_at:
        lines.append(
            f"**有记录的时间范围**：{state.clock_of(first_at)} ~ {state.clock_of(last_at)}"
            f"（约 {float(verdict.get('span_hours') or 0.0):.1f} 小时）"
        )
        lines.append(
            "这段之外的时间**没有任何记录**：不要写那段时间发生了什么，也不要用"
            "「一直很安静」「没什么事」这类话去填——那是编造，不是回忆。"
        )
    lines.append(f"当前时刻：{state.date_of(moment)} {state.clock_of(moment)}")
    lines.append("")
    lines.append("【素材怎么读（很重要）】")
    lines.append("· [归档]：别人事后整理的**客观记录**（第三人称，可能带成员代号）→ 转成你的回忆")
    lines.append("· [我的回忆]：你当天留下的第一人称回忆 → 可以直接用，但别照抄原句")
    lines.append("· [记忆]：记忆库里的一条（常来自私聊 / 群聊之后记下的事）。里面的「我」")
    lines.append("  **通常指的是对方**（记录时带的是他的口吻）→ 对方一律用「他」或「她」，")
    lines.append("  你写成「他跟我说…」「我回他…」；实在看不出是谁说的、说了什么，就略过")
    lines.append("· [状态]：你自己当时的身体 / 状态事实")
    lines.append("无论素材来自群聊还是私聊，全篇都只有一个「我」，就是你。")
    lines.append("")
    lines.append("【当天素材（按时间升序，只有这些是真实发生过的）】")
    if keep:
        lines.extend(render_item_line(item) for item in keep)
    else:
        lines.append("（没有任何素材）")
    if dropped:
        lines.append(f"（另有 {dropped} 条素材因预算被省略）")
    lines.append("")
    lines.append("请只写上面那段时间里真实发生过的事。")
    lines.append(
        "**最后再强调一次**：不要在正文里描述没有记录的时间——"
        "比如「白天很安静」「上午没什么事」「一整天都平平淡淡」这类句子，"
        "它们不是回忆而是编造。写不出来就少写，宁可日记短。"
    )

    # 「我是谁」：填了 persona_hint 就用它，日记的自我认知与语气会稳很多；
    # 留空时靠系统提示词里的中性描述兜住（通用、可上架）。
    system_prompt = _SYSTEM_PROMPT
    hint = str(getattr(config.generate, "persona_hint", "") or "").strip()
    if hint:
        system_prompt = f"{hint}\n\n{system_prompt}"

    return system_prompt, "\n".join(lines), dropped


def clean_text(config: RealDiaryConfig, raw: str) -> str:
    """清洗模型输出：去代码围栏、去它自己加的标题。"""
    text = str(raw or "").strip()
    fenced = _FENCE_PATTERN.match(text)
    if fenced:
        text = fenced.group(1).strip()
    text = _LEADING_TITLE_PATTERN.sub("", text, count=1).strip()
    return text


def spot_blank_filling(text: str) -> list[str]:
    """扫一遍正文里「用安静填补没记录的时间」的句式。

    这是**事后体检**，不是改写：模型偶尔还是会写「白天没什么动静」这种句子
    （2026-10-16 虚拟真机测试里连写两次，换着说法来）。既然拦不住它百分百，
    就至少让这件事**可见**——命中时元信息行会标出来，你一眼能看到「这篇可能补白了」。

    Args:
        text: 日记正文。

    Returns:
        命中的句式列表（空表示没发现）。
    """
    hits: list[str] = []
    for pattern in _BLANK_FILL_PATTERNS:
        if re.search(pattern, text):
            hits.append(pattern)
    return hits


def spot_bare_ids(text: str) -> list[str]:
    """找出正文里残留的裸数字（群成员代号）。

    素材里的发言人是数字代号（``482063``）；模型偶尔会照抄进日记，读起来像号码
    流水账，也是隐私面。这里是**事后体检，不自动改写**——自动删数字有误伤风险
    （「143390 分」也是数字，而且它是真的）。

    Args:
        text: 日记正文。

    Returns:
        命中的数字串列表（空表示干净）。
    """
    return _BARE_ID_PATTERN.findall(str(text or ""))


def render_file(
    config: RealDiaryConfig,
    date_str: str,
    body: str,
    *,
    meta: dict[str, Any],
) -> str:
    """组装最终写进文件的内容（标题 + 如实声明 + 正文 + 元信息行）。"""
    parts: list[str] = []
    title = str(config.output.title_format or "")
    if title:
        parts.append(title.format(date=date_str, weekday=state.weekday_cn(date_str)))

    # 「记得不太全」放在**正文之前**：读者第一眼就该知道自己看到的不是全貌，
    # 放在末尾等于读完才发现（也就等于没起到提醒作用）。
    note = str(config.output.incomplete_note or "").strip()
    if note and not meta.get("complete", True):
        parts.append(note)

    parts.append(body.strip())

    if config.output.write_meta_line:
        model_tag = str(meta.get("model") or "?")
        count = int(meta.get("items") or 0)
        flag = "完整" if meta.get("complete") else "不完整"
        extra = " · ⚠ 疑似补白（正文提到没记录的时间）" if meta.get("blank_warning") else ""
        if meta.get("id_warning"):
            extra += f" · ⚠ 含 {int(meta.get('id_count') or 0)} 处裸数字（疑似成员代号）"
        parts.append(
            f"<!-- 素材 {count} 条 · 模型 {model_tag} · 完整度：{flag}{extra} · "
            f"生成于 {time.strftime('%Y-%m-%d %H:%M', time.localtime())} -->"
        )
    return "\n\n".join(part for part in parts if part) + "\n"


# --------------------------------------------------------------------------- #
# 输出路径
# --------------------------------------------------------------------------- #


def output_dir(config: RealDiaryConfig) -> Path:
    """日记输出目录（相对路径按进程工作目录=实例根解析）。"""
    raw = str(config.plugin.diary_dir or "").strip() or "data/diary"
    return Path(raw).expanduser()


def output_path(config: RealDiaryConfig, date_str: str) -> Path:
    """某个日期的日记文件路径。"""
    filename = str(config.output.file_format or "{date}.md").format(date=date_str)
    return output_dir(config) / filename


def write_atomic(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再 ``os.replace``。

    这样中途崩了也不会留下半篇日记（``os.replace`` 在同一分区上是原子的）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# --------------------------------------------------------------------------- #
# 主链路
# --------------------------------------------------------------------------- #


def in_window(config: RealDiaryConfig, date_str: str, *, now: float | None = None) -> tuple[bool, str]:
    """日期是否在允许生成的窗口内。

    Args:
        config: 插件配置。
        date_str: 目标日期。
        now: 参考时刻。

    Returns:
        ``(是否允许, 原因)``。窗口外**一律不生成**（丢素材、不虚构）。
    """
    moment = float(now if now is not None else time.time())
    today = state.date_of(moment)
    age = state.days_between(date_str, today)
    window = int(config.generate.backfill_days)
    if age < 1:
        return False, f"{date_str} 还没过完（今天）"
    if age > window:
        return False, f"{date_str} 距今 {age} 天，超出回补窗口 {window} 天"
    return True, "在窗口内"


async def compose_day(
    config: RealDiaryConfig,
    date_str: str,
    *,
    now: float | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """为某一天生成日记。

    Args:
        config: 插件配置。
        date_str: 目标自然日。
        now: 参考时刻。
        dry_run: 只组装 prompt 并调用模型，**不落盘、不删素材、不登记**（演练用）。
        force: 忽略窗口与完整度限制（``/日记 现在`` 手动触发用）。

    Returns:
        结果字典（``status`` / ``path`` / ``items`` / ``complete`` / ``model`` / ``reason`` ...）。
    """
    if not state.is_valid_date(date_str):
        return {"status": "skipped", "date": date_str, "reason": "日期不合法"}

    moment = float(now if now is not None else time.time())

    # 1) 兜底抓取（事件通路可能漏了；这也是「缺则补」的一部分）
    if not dry_run:
        await collector.ingest_for_day(config, date_str, reason="compose")

    # 2) 窗口判定
    if not force:
        allowed, reason = in_window(config, date_str, now=moment)
        if not allowed:
            return {"status": "skipped", "date": date_str, "reason": reason}

    # 3) 读素材 + 完整度
    day = await state.load_day(date_str, fresh=True)
    items = list(day.get("items") or [])
    verdict = assess_day(
        items,
        min_items=int(config.generate.min_items),
        gap_max_hours=float(config.generate.gap_max_hours),
        date_str=date_str,
        coverage_min_hours=float(config.generate.coverage_min_hours),
    )

    if verdict["count"] == 0:
        return {
            "status": "skipped",
            "date": date_str,
            "reason": "没有任何素材",
            "items": 0,
        }

    # 4) 组装 prompt + 调模型
    system_prompt, user_prompt, dropped = build_prompt(
        config, date_str, items, verdict=verdict, now=moment
    )
    result = await llm.call(
        config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        request_name=f"real_diary_{date_str}",
    )
    if not result.ok:
        return {
            "status": "failed",
            "date": date_str,
            "reason": result.error,
            "items": verdict["count"],
            "model": result.model_tag,
        }

    body = clean_text(config, result.text)
    if not body:
        return {
            "status": "failed",
            "date": date_str,
            "reason": "清洗后正文为空",
            "items": verdict["count"],
            "model": result.model_tag,
        }

    # 事后体检：模型是不是又用「白天很安静」填了没记录的时间？
    # 拦不住它百分百，但至少让这件事在文件里可见、在日志里有声。
    blank_hits = spot_blank_filling(body)
    if blank_hits and not verdict["complete"]:
        logger.warning(
            f"[real_diary] {date_str} 的正文疑似补白（提到没记录的时间），"
            f"命中 {len(blank_hits)} 处；素材完整度本来就不完整，这篇请斟酌采信"
        )

    # 裸数字体检：模型把群成员代号原样抄进来了（读起来像号码流水账，也是隐私面）
    id_hits = spot_bare_ids(body)
    if id_hits:
        logger.warning(
            f"[real_diary] {date_str} 的正文残留 {len(id_hits)} 个裸数字"
            f"（疑似成员代号）：{id_hits[:6]}——已在元信息行标注，可考虑调 persona_hint 或重写"
        )

    meta = {
        "items": verdict["count"],
        "complete": bool(verdict["complete"]),
        "model": result.model_tag,
        "dropped": dropped,
        "blank_warning": bool(blank_hits) and not verdict["complete"],
        "id_warning": bool(id_hits),
        "id_count": len(id_hits),
    }

    if dry_run:
        return {
            "status": "dry_run",
            "date": date_str,
            "items": verdict["count"],
            "complete": bool(verdict["complete"]),
            "model": result.model_tag,
            "blank_warning": meta["blank_warning"],
            "preview": body,
        }

    # 5) 原子写盘（写成功才继续）
    path = output_path(config, date_str)
    try:
        write_atomic(path, render_file(config, date_str, body, meta=meta))
    except Exception as error:  # noqa: BLE001 - 写不下来就不登记、不删素材
        logger.warning(f"[real_diary] 写日记失败（{date_str} → {path}）: {error}")
        return {
            "status": "failed",
            "date": date_str,
            "reason": f"写盘失败: {error}",
            "items": verdict["count"],
            "model": result.model_tag,
        }

    # 6) 登记留档
    await state.mark_done(
        date_str,
        {
            "at": time.time(),
            "path": str(path),
            "items": verdict["count"],
            "complete": bool(verdict["complete"]),
            "reasons": list(verdict["reasons"]),
            "model": result.model_tag,
            "deleted": False,
        },
        keep=int(config.cleanup.keep_done_records),
    )

    # 7) 清理：**只删自己抓来的素材碎片**（booku 与外部数据一律不碰）
    deleted = False
    if config.cleanup.delete_fragments:
        deleted = await state.delete_day(date_str)
        if deleted:
            await state.mark_done(
                date_str,
                {
                    "at": time.time(),
                    "path": str(path),
                    "items": verdict["count"],
                    "complete": bool(verdict["complete"]),
                    "reasons": list(verdict["reasons"]),
                    "model": result.model_tag,
                    "deleted": True,
                },
                keep=int(config.cleanup.keep_done_records),
            )

    logger.info(
        f"[real_diary] 日记已写：{date_str} → {path}"
        f"（素材 {verdict['count']} 条，完整度 "
        f"{'完整' if verdict['complete'] else '不完整'}，模型 {result.model_tag}）"
    )
    await state.save_runtime(
        {
            "last_generate_at": time.time(),
            "last_generate_date": date_str,
            "fail_streak": 0,
            "last_error": "",
        },
        force=True,
    )
    return {
        "status": "written",
        "date": date_str,
        "path": str(path),
        "items": verdict["count"],
        "complete": bool(verdict["complete"]),
        "model": result.model_tag,
        "deleted": deleted,
    }


async def discard_day(config: RealDiaryConfig, date_str: str, *, reason: str = "") -> bool:
    """丢弃某天的素材碎片（**不生成日记**）。

    窗口外、或主人手动 ``/日记 丢弃`` 时用。**扔掉就是不写**——这正是
    「不虚构」的落地方式：没有把握的一天，就让它空着。

    Args:
        config: 插件配置。
        date_str: 目标日期。
        reason: 原因（写日志）。

    Returns:
        是否真的删掉了素材。
    """
    deleted = await state.delete_day(date_str)
    if deleted:
        logger.info(f"[real_diary] 已丢弃 {date_str} 的素材碎片（{reason or '未说明原因'}）")
    return deleted


async def run_check(
    config: RealDiaryConfig,
    *,
    now: float | None = None,
    reason: str = "check",
) -> list[dict[str, Any]]:
    """幂等检查：把「该写但还没写」的日期补齐。

    这是启动与每天 02:00 的同一个入口——**判据只有一条**：``done`` 里有没有它。

    Args:
        config: 插件配置。
        now: 参考时刻。
        reason: 触发原因（写日志）。

    Returns:
        每次尝试的结果列表。
    """
    moment = float(now if now is not None else time.time())
    today = state.date_of(moment)
    yesterday = state.shift_date(today, -1)
    results: list[dict[str, Any]] = []

    runtime = await state.load_runtime(fresh=True)
    streak = int(runtime.get("fail_streak") or 0)
    last_fail = float(runtime.get("last_fail_at") or 0.0)
    if streak > 0:
        wait = state.backoff_seconds(streak, base=int(config.generate.retry_backoff_seconds))
        if wait > 0 and (moment - last_fail) < wait:
            remain = wait - (moment - last_fail)
            logger.info(
                f"[real_diary] 上次生成失败（连续 {streak} 次），还要等 {remain:.0f}s 再试"
            )
            return [{"status": "backoff", "reason": f"退避中，剩 {remain:.0f}s"}]

    # 先看昨天（主通路），再看窗口内更早但还没写的日子
    targets: list[str] = []
    if yesterday:
        targets.append(yesterday)
    for day in await collector.pending_days(config):
        if day not in targets and in_window(config, day, now=moment)[0]:
            targets.append(day)

    for day in sorted(targets, reverse=True):
        if await state.get_done_record(day, fresh=True) is not None:
            continue
        if config.plugin.mode != "active":
            # observer：只采集、只报告「打算写」，绝不动模型与文件
            day_data = await state.load_day(day, fresh=True)
            count = len(day_data.get("items") or [])
            logger.info(
                f"[real_diary][observer] 将生成 {day} 的日记（素材 {count} 条）——"
                f"把 plugin.mode 改成 active 才会真的写"
            )
            results.append({"status": "observer", "date": day, "items": count, "reason": reason})
            continue

        outcome = await compose_day(config, day, now=moment)
        results.append(outcome)
        if outcome.get("status") == "failed":
            await state.save_runtime(
                {
                    "fail_streak": streak + 1,
                    "last_fail_at": time.time(),
                    "last_error": str(outcome.get("reason") or ""),
                },
                force=True,
            )
            streak += 1
        if outcome.get("status") in ("written", "failed"):
            # 一次检查里不要连打两次模型：写完/失败都先收手，剩下的交给下次
            break

    # 窗口外还有素材的日子：按口径丢弃（不虚构），并说清为什么
    for day in await collector.pending_days(config):
        allowed, why = in_window(config, day, now=moment)
        if allowed:
            continue
        if config.plugin.mode != "active":
            continue
        if await state.delete_day(day):
            logger.info(f"[real_diary] {day} 的素材已丢弃：{why}（不生成、不虚构）")
            results.append({"status": "discarded", "date": day, "reason": why})

    await state.save_runtime({"last_check_at": moment}, force=False)
    return results


__all__ = [
    "assess_day",
    "build_prompt",
    "clean_text",
    "compose_day",
    "discard_day",
    "fit_items",
    "in_window",
    "output_dir",
    "output_path",
    "render_file",
    "render_item_line",
    "run_check",
    "spot_bare_ids",
    "spot_blank_filling",
    "write_atomic",
]
