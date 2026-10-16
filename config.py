"""real_diary 插件配置。

配置文件默认路径：``config/plugins/real_diary/config.toml``。

三条刻意的默认值（都是「先别动，跑几天再说」）：

1. ``plugin.mode`` 默认 ``observer``：只采集、只记录、只打日志，**不写日记文件**。
   先用几天看清素材够不够、时间戳对不对，再切 ``active``。
2. ``cleanup.delete_fragments`` 默认 ``true``，但它**只在日记真的落盘成功后**才执行——
   素材碎片是「已被日记吸收的中间产物」，留着只是重复与隐私二份；写失败一律不删。
3. ``generate.backfill_days`` 默认 ``1``：崩了好几天时只补「昨天」，
   更早的素材按残缺处理、直接丢弃——**不虚构**是硬口径。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class RealDiaryConfig(BaseConfig):
    """真实日记配置模型。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "真实日记配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用本插件（false 时组件整体下线，不采集、不生成）",
        )
        mode: str = Field(
            default="observer",
            description=(
                "运行模式：\n"
                "observer —— 只采集：把素材碎片落进 data、打日志，**不生成日记文件**；\n"
                "active   —— 执行：到点检查，缺日记就生成，成功后删掉已消费的素材碎片。"
            ),
        )
        debug_log: bool = Field(
            default=False,
            description="是否输出调试日志（每一条素材、每次检查与生成决策）",
        )
        diary_dir: str = Field(
            default="",
            description=(
                "日记输出目录。\n"
                "**留空 = 实例根目录下的 ``data/diary/``**（跟 bot 数据放一起，备份不会漏）；\n"
                "想放到别处（例如 ``F:\\\\日记``）就填绝对路径，相对路径按实例根目录解析。"
            ),
        )

    @config_section("collect")
    class CollectSection(SectionBase):
        """素材采集：从哪儿抓、抓多少。"""

        archive_events: bool = Field(
            default=True,
            description=(
                "是否订阅 ``context_archiver:archived``，**归档成功就立刻**把这次摘要收成素材。\n"
                "这是「归档即联动」的实时通路；关掉也不会漏（还有巡检兜底），只是晚一步。"
            ),
        )
        daily_memory: bool = Field(
            default=True,
            description=(
                "是否采集「每日短期记忆」（``cross_stream_relay`` / ``send_to`` 写的\n"
                "``daily_memory_<流>_<日期>.json``）。它天生按天一条、带 ``memory_date``，\n"
                "是性价比最高的现成素材；两个插件字段完全一致，取较新的那份。"
            ),
        )
        memory_index: bool = Field(
            default=True,
            description=(
                "是否采集 ``context_archiver`` 的记忆索引增量（``memory_index.json``，\n"
                "每条带 id/title/snippet/triggers/risk/person/at）。按 ``at`` 切到某一天。"
            ),
        )
        booku_digest: bool = Field(
            default=False,
            description=(
                "是否直接从 ``booku_memory`` 记忆库拉当天写入的记忆条目。\n"
                "**默认关**：booku 没有「按时间范围查」的接口，只能拉一批（默认 200 条）\n"
                "再本地按 ``metadata.created_at`` 筛，而且它的检索是全局的、不按账号隔离；\n"
                "而 ``memory_index`` 通常已经覆盖了同一批内容（那是 context_archiver 写记忆时\n"
                "顺手登记的索引），所以这里多半是重复劳动。\n"
                "想连「别的插件/模型自己写进 booku 的记忆」也收进来时再打开。\n"
                "**只读**：本插件永远不会修改或删除记忆库。"
            ),
        )
        audit_timeline: bool = Field(
            default=True,
            description=(
                "是否采集 ``context_archiver`` 的归档审计（``audit.json``）作为时间线：\n"
                "它记录「那天到底归档了什么、成功没有、写了几条记忆」。"
            ),
        )
        state_phases: bool = Field(
            default=True,
            description=(
                "是否采样状态/阶段轨迹。\n"
                "**注意**：sisp 本身不记录转变历史，所以这里靠本插件「读快照 + 自己比对」\n"
                "来记流水——没有消息的时段采不到，这是已知局限、不是 bug。"
            ),
        )
        sisp_stores: list[str] = Field(
            default_factory=lambda: ["sisp", "deep_neo_sisp"],
            description=(
                "读状态快照的存储命名空间，按顺序试第一个存在的。\n"
                "实例里的 ``sisp`` 与工作区的 ``deep_neo_sisp`` 是两套命名空间，默认都覆盖。"
            ),
        )
        poll_seconds: int = Field(
            default=300,
            description="巡检兜底间隔（秒）：事件丢了、进程当时没在，靠它把素材补齐。",
            ge=30,
            le=7200,
        )
        max_items_per_day: int = Field(
            default=400,
            description=(
                "单日素材上限（条）。超了丢最旧的，并补一条「素材被截断」的说明素材——\n"
                "宁可少素材，也不能让单日文件无限膨胀。"
            ),
            ge=20,
            le=5000,
        )
        max_item_chars: int = Field(
            default=1200,
            description="单条素材正文的字符上限（超出截断并加省略号）。",
            ge=100,
            le=8000,
        )

    @config_section("generate")
    class GenerateSection(SectionBase):
        """生成策略：什么时候写、写到什么程度。"""

        check_on_start: bool = Field(
            default=True,
            description=(
                "启动时是否检查一次「昨天的日记写了没」，没写就立刻补。\n"
                "**这是主要通路**：不依赖定时器一定跑到，崩溃/凌晨关机都不会漏。"
            ),
        )
        check_time: str = Field(
            default="02:00",
            description=(
                "运行期每天的第二次检查时点（``HH:MM``，本地时间）。\n"
                "到点了进程还开着就查一次，缺了就补；已经写过就什么都不做（幂等）。\n"
                "留空 = 只在启动时检查。"
            ),
        )
        backfill_days: int = Field(
            default=1,
            description=(
                "允许往回补几天。\n"
                "1 = 只补「昨天」（推荐）；0 = 只认严格意义上的昨天；\n"
                "**超过这个窗口的日期一律丢弃素材、不生成日记**——素材残缺时宁可空着，也不编。"
            ),
            ge=0,
            le=7,
        )
        min_items: int = Field(
            default=3,
            description=(
                "一天素材少于这么多条就判「信息不完整」：窗口外的丢弃，窗口内的生成但**标注不完整**。"
            ),
            ge=1,
            le=100,
        )
        gap_max_hours: float = Field(
            default=6.0,
            description=(
                "相邻素材的最大间隔（小时）。超过就算「中间有一段时间没记录」→ 判不完整。\n"
                "它是识别「进程当时根本没在跑」的主要手段。"
            ),
            ge=0.5,
            le=48.0,
        )
        coverage_min_hours: float = Field(
            default=2.0,
            description=(
                "当天「有记录的时间跨度」至少要有这么长，否则判不完整。\n"
                "**为什么必须有这条**：只按「相邻素材间隔」判，会把「只有晚上一小时有记录」\n"
                "当成完整的一天——于是模型顺手把没记录的上午写成「白天一直很安静」，\n"
                "而那句话是**编的**（2026-10-16 的虚拟真机测试当场抓到过）。\n"
                "默认 2 小时只拦这种极端情况；设 0 关掉这条判据。"
            ),
            ge=0.0,
            le=24.0,
        )
        persona_hint: str = Field(
            default="",
            description=(
                "给模型的一句自我说明（可选），例如：\n"
                "  「你是深深，一只鲸鱼娘女仆，这里是你的私人日记。」\n"
                "留空时用中性描述（「你在替这个角色自己写日记」）。\n"
                "**填上它，日记的自我认知与语气会明显更稳**——尤其是实例里有多个角色时。"
            ),
        )
        max_prompt_chars: int = Field(
            default=16000,
            description=(
                "喂给模型的素材文本上限（字符）。超了按优先级丢：先丢最早的流水，\n"
                "保时间线与当日短期记忆——它们是叙事的骨架。"
            ),
            ge=2000,
            le=120000,
        )
        retry_backoff_seconds: int = Field(
            default=300,
            description=(
                "生成失败后的退避（秒）。失败**不删素材、不标记完成**，退避到点再试，\n"
                "避免每个检查周期都烧一次模型调用。"
            ),
            ge=0,
            le=86400,
        )

    @config_section("model")
    class ModelSection(SectionBase):
        """模型选择（默认跟实例已有的 ``summary`` 任务，无需新建任务）。"""

        task_name: str = Field(
            default="summary",
            description=(
                "用哪个模型任务写日记。默认 ``summary``（实例里通常已配，且多半是便宜模型）；\n"
                "没有该任务时会回落到 ``actor``（主回复模型）。"
            ),
        )
        model_name: str = Field(
            default="",
            description=(
                "直接点名一个模型（按名字，非空时优先于 task_name）。\n"
                "例如 ``deepseek-v4-flash``。"
            ),
        )
        temperature: float = Field(
            default=0.6,
            description="写日记的温度。比总结高一点（0.6 左右）文风更像人写的，别开太高免得出戏。",
            ge=0.0,
            le=2.0,
        )
        max_tokens: int = Field(
            default=8192,
            description=(
                "输出上限。**必须给思考模型留足**：思考链可能吃掉 3000~6200 token，\n"
                "设太小会让正文根本没机会输出（表现是 ``empty response``）。\n"
                "提高上限本身不额外花钱（按实际输出计费）。"
            ),
            ge=256,
            le=64000,
        )

    @config_section("output")
    class OutputSection(SectionBase):
        """输出格式。"""

        file_format: str = Field(
            default="{date}.md",
            description="文件名模板，占位符 ``{date}``（``YYYY-MM-DD``）。",
        )
        title_format: str = Field(
            default="# {date}",
            description="正文首行标题模板，占位符 ``{date}`` / ``{weekday}``。留空 = 不写标题。",
        )
        write_meta_line: bool = Field(
            default=True,
            description=(
                "是否在文末附一行元信息（素材条数 / 模型 / 是否完整）。\n"
                "**建议留着**：日记是给人看的，但那行能让事后核对「这篇是不是残缺时写的」。"
            ),
        )
        incomplete_note: str = Field(
            default="（这一天我记得不太全）",
            description=(
                "素材被判「不完整」但在窗口内、仍要生成时，正文末尾追加的一句**如实声明**。\n"
                "宁可承认记得不全，也不要编。留空 = 不加。"
            ),
        )

    @config_section("cleanup")
    class CleanupSection(SectionBase):
        """生成之后的清理（**只动自己的素材碎片**）。"""

        delete_fragments: bool = Field(
            default=True,
            description=(
                "日记落盘成功后，是否立刻删掉当天的素材碎片（``day-YYYY-MM-DD``）。\n"
                "碎片是「已被日记吸收的中间产物」，留着只是重复与隐私二份。\n"
                "**注意**：本插件永远不会删 booku 记忆库、context_archiver 索引或任何外部数据。"
            ),
        )
        keep_done_records: int = Field(
            default=400,
            description="``done`` 留档最多保留多少天（用于「这天写过没」与事后核对）。",
            ge=30,
            le=5000,
        )

    @config_section("storage")
    class StorageSection(SectionBase):
        """落盘命名空间。"""

        namespace: str = Field(
            default="real_diary",
            description=(
                "素材与状态的存储命名空间（``data/json_storage/<namespace>/``）。\n"
                "多个 Bot 共用同一个 data 目录时，给每个实例配不同的 namespace。"
            ),
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    collect: CollectSection = Field(default_factory=CollectSection)
    generate: GenerateSection = Field(default_factory=GenerateSection)
    model: ModelSection = Field(default_factory=ModelSection)
    output: OutputSection = Field(default_factory=OutputSection)
    cleanup: CleanupSection = Field(default_factory=CleanupSection)
    storage: StorageSection = Field(default_factory=StorageSection)


__all__ = ["RealDiaryConfig"]
