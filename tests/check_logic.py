"""real_diary 逻辑自检。

用法（用 neo 实例自己的 venv python 跑）::

    $env:NEO_ROOT="F:\\Neo-MoFox-Aemeath"
    & "F:\\Neo-MoFox-Aemeath\\.venv\\Scripts\\python.exe" F:\\mofox插件\\plugin\\repo\\real_diary\\tests\\check_logic.py

**离线**：storage 打桩成内存字典、模型调用打桩成固定文本、输出写到临时目录，
不碰任何真实实例、不联网、不花一分钱。

覆盖的四条硬口径：

1. **不虚构**：素材残缺或超窗口 → 不生成（或生成但如实标注）；
2. **幂等补写**：写过就不重复写；
3. **只删自己的碎片**：日记落盘成功才删素材，失败一律保留；
4. **observer 不动手**：只采集、只报告。
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
PLUGIN_DIR = Path(__file__).resolve().parent.parent
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

# GBK 控制台下 print 非 GBK 字符会抛 UnicodeEncodeError，把「全过」变成 exit 1（踩过）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 改不了就照旧
        pass

_failures: list[str] = []
_passed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """打印一条检查结果。"""
    global _passed
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  — {detail}" if detail else ""))
    if condition:
        _passed += 1
    else:
        _failures.append(label)


def section(title: str) -> None:
    """分节标题。"""
    print("-" * 72)
    print(title)


# --------------------------------------------------------------------------- #
# 打桩
# --------------------------------------------------------------------------- #

_STORE: dict[tuple[str, str], object] = {}


def _patch_storage() -> None:
    """把 storage_api 换成内存字典。"""
    from src.app.plugin_system.api import storage_api

    async def save_json(store: str, name: str, data: object) -> None:
        _STORE[(store, name)] = copy.deepcopy(data)

    async def load_json(store: str, name: str):
        value = _STORE.get((store, name))
        return copy.deepcopy(value) if value is not None else None

    async def delete_json(store: str, name: str) -> bool:
        return _STORE.pop((store, name), None) is not None

    async def list_json(store: str) -> list[str]:
        return [name for (store_name, name) in _STORE if store_name == store]

    storage_api.save_json = save_json  # type: ignore[assignment]
    storage_api.load_json = load_json  # type: ignore[assignment]
    storage_api.delete_json = delete_json  # type: ignore[assignment]
    storage_api.list_json = list_json  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 正文
# --------------------------------------------------------------------------- #


async def main() -> int:
    """跑完全部断言。"""
    _patch_storage()

    from real_diary import collector, composer, llm, sources, state
    from real_diary.config import RealDiaryConfig

    # ── 1. 日期工具 ─────────────────────────────────────────────────────────
    section("1. 日期工具（一律本地自然日）")
    stamp = datetime(2026, 10, 15, 13, 5, 0).timestamp()
    check("date_of 出 YYYY-MM-DD", state.date_of(stamp) == "2026-10-15", state.date_of(stamp))
    check("date_of(0) 是空串", state.date_of(0) == "")
    check("shift_date 往前一天", state.shift_date("2026-10-15", -1) == "2026-10-14")
    check("shift_date 跨月", state.shift_date("2026-03-01", -1) == "2026-02-28")
    start, end = state.day_bounds("2026-10-15")
    check("day_bounds 正好一天", end - start == 86400.0, f"{end - start}")
    check(
        "weekday_cn 与 datetime 对齐",
        state.weekday_cn("2026-10-15")
        == ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[
            datetime(2026, 10, 15).weekday()
        ],
        state.weekday_cn("2026-10-15"),
    )
    check("days_between 算得对", state.days_between("2026-10-12", "2026-10-15") == 3)
    check("clock_of 出 HH:MM", state.clock_of(stamp) == "13:05", state.clock_of(stamp))
    check("非法日期返回空", not state.is_valid_date("2026-10-32"))

    # ── 2. 素材条目 ─────────────────────────────────────────────────────────
    section("2. 素材条目（去重、截断）")
    item = state.make_item(at=stamp, source="context_archiver", kind="archive", text="  今天 聊了   很久 ")
    check(
        "正文空白被压缩",
        bool(item) and item["text"] == "今天 聊了 很久",
        str(item and item["text"]),
    )
    check("day 按自己的时间戳算", bool(item) and item["day"] == "2026-10-15", str(item and item["day"]))
    check("空正文不产条目", state.make_item(at=stamp, source="x", kind="y", text="   ") is None)
    long_item = state.make_item(at=stamp, source="x", kind="y", text="啊" * 500, max_chars=100)
    check(
        "超长正文被截断",
        bool(long_item) and len(long_item["text"]) == 101 and long_item["text"].endswith("…"),
        str(long_item and len(long_item["text"])),
    )
    check(
        "同源同型同文 → 同指纹",
        state.item_fingerprint(item) == state.item_fingerprint(dict(item)),
    )

    # ── 3. 完整度判定 ───────────────────────────────────────────────────────
    section("3. 完整度判定（识别「那段时间没在跑」）")
    def make_items(gaps_hours: list[float]) -> list[dict]:
        rows = []
        cursor = stamp
        for index, gap in enumerate([0.0] + gaps_hours):
            cursor += gap * 3600.0
            built = state.make_item(at=cursor, source="context_archiver", kind="archive", text=f"第{index}件")
            if built:
                rows.append(built)
        return rows

    good = composer.assess_day(make_items([1.0, 1.0]), min_items=3, gap_max_hours=6.0)
    check("3 条、间隔 1 小时 → 完整", good["complete"] is True, str(good["reasons"]))
    few = composer.assess_day(make_items([1.0]), min_items=3, gap_max_hours=6.0)
    check("条数不足 → 不完整并说明", not few["complete"] and "素材太少" in few["reasons"][0], str(few["reasons"]))
    sparse = composer.assess_day(make_items([1.0, 10.0]), min_items=3, gap_max_hours=6.0)
    check(
        "中间空 10 小时 → 不完整并说明",
        not sparse["complete"] and any("没有记录" in reason for reason in sparse["reasons"]),
        str(sparse["reasons"]),
    )
    check("最大间隔算得对", abs(sparse["max_gap_hours"] - 10.0) < 1e-6, f"{sparse['max_gap_hours']:.1f}")

    # 覆盖度判据（2026-10-16 虚拟真机测试抓到：只有晚上一小时有记录，却被判「完整」，
    # 模型于是把没记录的上午写成「白天一直很安静」——那是编的）
    narrow = composer.assess_day(
        make_items([0.4, 0.4]),
        min_items=3,
        gap_max_hours=6.0,
        date_str="2026-10-15",
        coverage_min_hours=2.0,
    )
    check(
        "只有 0.8 小时有记录 → 不完整并说明",
        not narrow["complete"] and any("有记录" in reason for reason in narrow["reasons"]),
        str(narrow["reasons"]),
    )
    wide = composer.assess_day(
        make_items([1.0, 1.0]),
        min_items=3,
        gap_max_hours=6.0,
        date_str="2026-10-15",
        coverage_min_hours=2.0,
    )
    check("跨度够 2 小时 → 完整", wide["complete"] is True, str(wide["reasons"]))
    check(
        "coverage_min_hours=0 时这条判据可关",
        composer.assess_day(
            make_items([0.4, 0.4]), min_items=3, gap_max_hours=6.0, coverage_min_hours=0.0
        )["complete"]
        is True,
    )

    # 提示词必须堵住「用安静填补没记录的时间」这条虚构路径
    system_prompt, user_prompt, _dropped = composer.build_prompt(
        RealDiaryConfig(), "2026-10-15", make_items([1.0, 1.0]), verdict=wide
    )
    check(
        "提示词写明「这段之外没有任何记录」",
        "没有任何记录" in user_prompt,
        user_prompt[:0] or "",
    )
    check(
        "系统提示词禁止用「安静」填补",
        "整段跳过" in system_prompt and "那是编的" in system_prompt,
    )
    check("提示词带上「有记录的时间范围」", "有记录的时间范围" in user_prompt)

    # ── 4. 预算裁剪 ─────────────────────────────────────────────────────────
    section("4. 预算裁剪（先丢低优先级、更早的流水）")
    mixed: list[dict] = []
    for index in range(20):
        for kind in ("note", "memory", "daily"):
            built = state.make_item(
                at=stamp + index * 60,
                source="x",
                kind=kind,
                text=f"{kind}-{index}-" + "字" * 40,
            )
            if built:
                mixed.append(built)
    kept, dropped = composer.fit_items(mixed, limit=1500)
    kept_chars = sum(len(composer.render_item_line(entry)) for entry in kept)
    check("超预算时确实丢了东西", dropped > 0, f"丢 {dropped} 条 / 共 {len(mixed)} 条")
    check("裁剪后总长度不超上限", kept_chars <= 1500, f"{kept_chars} 字符")
    check(
        "最低优先级的 note 被优先丢光",
        not any(entry["kind"] == "note" for entry in kept),
        f"还剩 {sum(1 for entry in kept if entry['kind'] == 'note')} 条 note",
    )
    check(
        "高优先级的 daily 保住的比 note 多",
        sum(1 for entry in kept if entry["kind"] == "daily")
        > sum(1 for entry in kept if entry["kind"] == "note"),
        f"daily={sum(1 for entry in kept if entry['kind'] == 'daily')}",
    )
    all_kept, none_dropped = composer.fit_items(mixed, limit=10**9)
    check("预算够就一条都不丢", none_dropped == 0 and len(all_kept) == len(mixed))

    # ── 5. 输出清洗与文件渲染 ───────────────────────────────────────────────
    section("5. 输出清洗与渲染")
    config = RealDiaryConfig()
    fenced = composer.clean_text(config, "```markdown\n# 2026-10-15\n\n今天挺好。\n```")
    check("去掉代码围栏与自带标题", fenced == "今天挺好。", repr(fenced))
    body = composer.render_file(
        config,
        "2026-10-15",
        "今天挺好。",
        meta={"items": 5, "complete": False, "model": "task:summary"},
    )
    check("标题按模板渲染", body.startswith("# 2026-10-15"), repr(body[:24]))
    check("不完整时追加如实声明", "记得不太全" in body, repr(body))
    check(
        "如实声明在正文**之前**（读到正文前就该知道不是全貌）",
        body.index("记得不太全") < body.index("今天挺好"),
        repr(body[:80]),
    )
    check("文末留元信息（可事后核对）", "素材 5 条" in body and "不完整" in body)

    # 事后体检：模型用「安静」填补没记录的时间时，必须留下痕迹
    check(
        "抓得到「白天没什么动静」这类补白",
        len(composer.spot_blank_filling("今天白天群里没什么动静，晚上才热闹起来。")) > 0,
    )
    check(
        "抓得到「一整天平平淡淡」",
        len(composer.spot_blank_filling("一整天都平平淡淡的过去了。")) > 0,
    )
    check("正常正文不误报", not composer.spot_blank_filling("晚上聊了很久，后来就去睡了。"))
    flagged = composer.render_file(
        config,
        "2026-10-15",
        "白天没什么动静。",
        meta={"items": 3, "complete": False, "model": "m", "blank_warning": True},
    )
    check("补白命中时元信息行标出来", "疑似补白" in flagged, repr(flagged[-90:]))

    # 裸数字体检（2026-10-16 主人一眼看出日记里 7 个成员 ID）
    check(
        "抓得到正文里的裸数字 ID",
        composer.spot_bare_ids("482063 问了一句，995859 回了「因为你没加载」")
        == ["482063", "995859"],
        str(composer.spot_bare_ids("482063 问了一句，995859 回了")),
    )
    check(
        "短数字不误报（4/5、2026）",
        not composer.spot_bare_ids("进度 4/5，2026 年 10 月 13 日"),
    )
    check(
        "体检是提醒性质：长数字（分数）也会列出，所以绝不自动改写",
        composer.spot_bare_ids("累计 143390 分") == ["143390"],
    )
    id_flagged = composer.render_file(
        config,
        "2026-10-15",
        "482063 说了一句。",
        meta={"items": 3, "complete": True, "model": "m", "id_warning": True, "id_count": 1},
    )
    check("裸数字命中时元信息行标出来", "裸数字" in id_flagged, repr(id_flagged[-90:]))

    # persona_hint：填了就用它开场，留空则用中性描述
    hinted = RealDiaryConfig()
    hinted.generate.persona_hint = "你是深深，一只鲸鱼娘女仆，这里是你的私人日记。"
    sys_hint, _user_hint, _ = composer.build_prompt(
        hinted, "2026-10-15", make_items([1.0, 1.0]), verdict=wide
    )
    check("persona_hint 会作为开场写进系统提示词", sys_hint.startswith("你是深深"), repr(sys_hint[:24]))
    sys_plain, _, _ = composer.build_prompt(
        RealDiaryConfig(), "2026-10-15", make_items([1.0, 1.0]), verdict=wide
    )
    check("没填 persona_hint 时用中性描述", "整篇只有一个「我」" in sys_plain)
    check("系统提示词里有「素材是客观记录」的视角约束", "不要把素材里的「我」当成你的视角" in sys_plain)
    check("系统提示词禁止原样写成员代号", "不要原样写进日记" in sys_plain)
    check("系统提示词要求群聊私聊都写", "群聊与私聊都要写" in sys_plain)
    check(
        "user prompt 里有「素材怎么读」的标签说明",
        "【素材怎么读" in user_prompt and "[我的回忆]" in user_prompt and "[归档]" in user_prompt,
    )

    # ── 6. 归档事件 → 素材（含日期归属） ────────────────────────────────────
    section("6. 归档事件落素材（按素材自己的时间戳归日）")
    _STORE.clear()
    state.invalidate()
    payload = {
        "event": "context_archiver:archived",
        "stream_id": "s" * 64,
        "at": stamp,
        "start_ts": stamp - 600,
        "end_ts": stamp,
        "message_count": 12,
        "summary": "聊了会儿晚饭吃什么",
        "memory_ids": ["mem-1", "mem-2"],
        "sink": "booku",
    }
    added = await collector.ingest_archive_event(config, payload)
    check("事件收成 1 条素材", added == 1, str(added))
    again = await collector.ingest_archive_event(config, payload)
    check("同一事件重复上报不再收（指纹去重）", again == 0, str(again))
    day = await state.load_day("2026-10-15", fresh=True)
    check("素材落在它自己的那天", len(day["items"]) == 1, str(len(day["items"])))
    check("素材里带 memory_ids", day["items"][0]["meta"]["memory_ids"] == ["mem-1", "mem-2"])

    # ── 7. 窗口判定 ─────────────────────────────────────────────────────────
    section("7. 窗口判定（今天不写、昨天的写、再早的不写）")
    now = datetime(2026, 10, 15, 3, 0, 0).timestamp()
    window_config = RealDiaryConfig()
    window_config.generate.backfill_days = 1
    check("今天 → 不生成", composer.in_window(window_config, "2026-10-15", now=now)[0] is False)
    check("昨天 → 生成", composer.in_window(window_config, "2026-10-14", now=now)[0] is True)
    check(
        "前天（超窗口 1 天）→ 不生成",
        composer.in_window(window_config, "2026-10-13", now=now)[0] is False,
        composer.in_window(window_config, "2026-10-13", now=now)[1],
    )

    # ── 8. 幂等 + 只删自己的碎片 + 失败保留 ─────────────────────────────────
    section("8. 生成链路（幂等 / 只删自己的碎片 / 失败保留）")
    tmpdir = tempfile.mkdtemp(prefix="real_diary_check_")
    gen_config = RealDiaryConfig()
    gen_config.plugin.mode = "active"
    gen_config.plugin.diary_dir = tmpdir
    gen_config.generate.backfill_days = 1
    gen_config.generate.min_items = 1

    _STORE.clear()
    state.invalidate()
    yesterday = "2026-10-14"
    for hour in (9, 11, 14):
        built = state.make_item(
            at=datetime(2026, 10, 14, hour, 0).timestamp(),
            source="daily_memory",
            kind="daily",
            text=f"[某个群 当天摘要] 第 {hour} 点的记录",
        )
        await state.append_items(yesterday, [built], limit=100, max_chars=500)

    calls: list[str] = []
    original_call = llm.call

    async def fake_call(config, *, system_prompt, user_prompt, **kwargs):
        calls.append(user_prompt)
        return llm.LLMCallResult(ok=True, text="昨天聊了些家常，没什么大事。", model_tag="fake")

    llm.call = fake_call  # type: ignore[assignment]
    try:
        result = await composer.compose_day(gen_config, yesterday, now=now)
        check("生成成功", result["status"] == "written", str(result))
        written = Path(str(result["path"]))
        check("日记文件真的写出来了", written.exists(), str(written))
        check("文件内容含正文", "家常" in written.read_text(encoding="utf-8"))
        check("素材碎片已删（只删自己的）", await state.load_day(yesterday, fresh=True) is not None)
        check(
            "素材确实空了",
            len((await state.load_day(yesterday, fresh=True))["items"]) == 0,
            str(len((await state.load_day(yesterday, fresh=True))["items"])),
        )
        record = await state.get_done_record(yesterday, fresh=True)
        check("留档记下了（写过就不重写）", bool(record) and record.get("deleted") is True, str(record))

        calls.clear()
        second = await composer.run_check(gen_config, now=now, reason="check")
        check("第二次检查不再生成（幂等）", not calls, f"调了 {len(calls)} 次模型")
        check(
            "第二次检查没有产生 written",
            all(item.get("status") != "written" for item in second),
            str(second),
        )

        # 失败路径：模型挂了 → 不删素材、不登记
        _STORE.clear()
        state.invalidate()
        await state.append_items(
            yesterday,
            [state.make_item(at=datetime(2026, 10, 14, 9, 0).timestamp(), source="x", kind="daily", text="一条素材")],
            limit=100,
            max_chars=500,
        )

        async def boom_call(config, *, system_prompt, user_prompt, **kwargs):
            return llm.LLMCallResult(ok=False, error="模型炸了", model_tag="fake")

        llm.call = boom_call  # type: ignore[assignment]
        failed = await composer.compose_day(gen_config, yesterday, now=now)
        check("失败被如实报告", failed["status"] == "failed", str(failed))
        remaining = await state.load_day(yesterday, fresh=True)
        check("失败时素材保留（下次再试）", len(remaining["items"]) == 1, str(len(remaining["items"])))
        check("失败时不登记留档", await state.get_done_record(yesterday, fresh=True) is None)
    finally:
        llm.call = original_call  # type: ignore[assignment]

    # ── 9. 超窗口：丢素材、不生成（不虚构） ─────────────────────────────────
    section("9. 超窗口的日子：丢弃素材、绝不生成")
    _STORE.clear()
    state.invalidate()
    stale = "2026-10-10"
    await state.append_items(
        stale,
        [state.make_item(at=datetime(2026, 10, 10, 9, 0).timestamp(), source="x", kind="daily", text="残缺的一条")],
        limit=100,
        max_chars=500,
    )
    calls.clear()

    async def should_not_call(config, *, system_prompt, user_prompt, **kwargs):
        calls.append(user_prompt)
        return llm.LLMCallResult(ok=True, text="不该被调用", model_tag="fake")

    llm.call = should_not_call  # type: ignore[assignment]
    try:
        results = await composer.run_check(gen_config, now=now, reason="check")
        check("超窗口的日子一次模型都没调", not calls, f"调了 {len(calls)} 次")
        check(
            "结果里说明了丢弃原因",
            any(item.get("status") == "discarded" for item in results),
            str(results),
        )
        check(
            "素材确实被丢弃",
            len((await state.load_day(stale, fresh=True))["items"]) == 0,
            str(len((await state.load_day(stale, fresh=True))["items"])),
        )
    finally:
        llm.call = original_call  # type: ignore[assignment]

    # ── 10. observer：只采集、不动手 ────────────────────────────────────────
    section("10. observer 模式：只报告「打算写」，不写文件、不调模型")
    _STORE.clear()
    state.invalidate()
    obs_config = RealDiaryConfig()
    obs_config.plugin.mode = "observer"
    obs_config.plugin.diary_dir = tmpdir
    await state.append_items(
        yesterday,
        [state.make_item(at=datetime(2026, 10, 14, 9, 0).timestamp(), source="x", kind="daily", text="一条素材")],
        limit=100,
        max_chars=500,
    )
    calls.clear()
    llm.call = should_not_call  # type: ignore[assignment]
    try:
        obs_results = await composer.run_check(obs_config, now=now, reason="check")
        check("observer 不调模型", not calls, f"调了 {len(calls)} 次")
        check(
            "observer 报告为待写",
            any(item.get("status") == "observer" for item in obs_results),
            str(obs_results),
        )
        check(
            "observer 不删素材",
            len((await state.load_day(yesterday, fresh=True))["items"]) == 1,
            str(len((await state.load_day(yesterday, fresh=True))["items"])),
        )
    finally:
        llm.call = original_call  # type: ignore[assignment]

    # ── 11. 素材源：每天一条的 daily_memory 与按天过滤 ──────────────────────
    section("11. 素材源读取（daily_memory / audit / memory_index）")
    _STORE.clear()
    state.invalidate()
    _STORE[("send_to", "daily_memory_streamA_2026-10-14")] = {
        "stream_id": "streamA",
        "group_name": "测试群",
        "chat_type": "group",
        "memory_date": "2026-10-14",
        "summary": "群里聊了晚饭",
        "message_count": 41,
        "updated_at": "2026-10-14 23:58:00",
        "last_summarized_ts": datetime(2026, 10, 14, 23, 58).timestamp(),
    }
    _STORE[("cross_stream_relay", "daily_memory_streamA_2026-10-14")] = {
        "stream_id": "streamA",
        "group_name": "测试群",
        "chat_type": "group",
        "memory_date": "2026-10-14",
        "summary": "旧的那份（应该被更新的顶掉）",
        "message_count": 41,
        "last_summarized_ts": datetime(2026, 10, 14, 22, 0).timestamp(),
    }
    _STORE[("context_archiver", "audit")] = [
        {
            "at": datetime(2026, 10, 14, 20, 0).timestamp(),
            "stream_id": "streamA",
            "trigger": "idle",
            "message_count": 8,
            "start_ts": datetime(2026, 10, 14, 19, 30).timestamp(),
            "end_ts": datetime(2026, 10, 14, 20, 0).timestamp(),
            "summary": "傍晚那一段",
            "memory_ids": ["m1"],
            "sink": "booku",
            "cleared": False,
            "ok": True,
            "error": "",
        },
        {
            "at": datetime(2026, 10, 14, 21, 0).timestamp(),
            "stream_id": "streamA",
            "trigger": "idle",
            "message_count": 0,
            "start_ts": 0.0,
            "end_ts": 0.0,
            "summary": "失败的这次不该被采",
            "memory_ids": [],
            "sink": "booku",
            "cleared": False,
            "ok": False,
            "error": "总结调用失败",
        },
    ]
    _STORE[("context_archiver", "memory_index")] = {
        "entries": [
            {
                "id": "mem-9",
                "title": "晚饭",
                "snippet": "想吃火锅",
                "triggers": ["火锅", "晚饭"],
                "risk": "normal",
                "person": "p1",
                "stream": "streamA",
                "at": datetime(2026, 10, 14, 20, 30).timestamp(),
            },
            {
                "id": "mem-old",
                "title": "前天的",
                "snippet": "不该出现",
                "at": datetime(2026, 10, 12, 10, 0).timestamp(),
            },
        ]
    }

    daily_items = await sources.from_daily_memory(config, "2026-10-14")
    check("daily_memory 取到 1 条（同流的旧那份被顶掉）", len(daily_items) == 1, str(len(daily_items)))
    check(
        "取的是 last_summarized_ts 更新的那份",
        bool(daily_items) and "晚饭" in daily_items[0]["text"],
        str(daily_items[0]["text"]) if daily_items else "",
    )
    audit_items = await sources.from_audit(config, "2026-10-14")
    check("audit 只采成功的那条", len(audit_items) == 1, str(len(audit_items)))
    index_items = await sources.from_memory_index(config, "2026-10-14")
    check("memory_index 按天过滤（只有当天那条）", len(index_items) == 1, str(len(index_items)))
    check(
        "别的天不会混进来",
        bool(index_items) and index_items[0]["meta"]["memory_id"] == "mem-9",
        str(index_items[0]["meta"]) if index_items else "",
    )
    merged = await sources.collect_for_day(config, "2026-10-14")
    check("汇总抓取合并了三路", len(merged) >= 3, str(len(merged)))

    # ── 12. 退避 ────────────────────────────────────────────────────────────
    section("12. 失败退避（别每个检查周期都烧一次模型）")
    check("首次退避 = base", state.backoff_seconds(1, base=300, cap=7200) == 300.0)
    check("第 3 次 = base×4", state.backoff_seconds(3, base=300, cap=7200) == 1200.0)
    check("封顶生效", state.backoff_seconds(20, base=300, cap=1800) == 1800.0)
    check("base=0 表示不退避", state.backoff_seconds(5, base=0) == 0.0)

    print("-" * 72)
    if _failures:
        print(f"结果：{len(_failures)} 项失败 -> {_failures}")
        return 1
    print(f"结果：全部通过（{_passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
