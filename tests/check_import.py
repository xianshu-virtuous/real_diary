"""real_diary 导入冒烟 + manifest 合规自检。

用法::

    $env:NEO_ROOT="F:\\Neo-MoFox-Aemeath"
    & "F:\\Neo-MoFox-Aemeath\\.venv\\Scripts\\python.exe" F:\\mofox插件\\plugin\\repo\\real_diary\\tests\\check_import.py

为什么第 0 节必须最先跑：框架加载插件入口时是**先登记 ``real_diary.plugin``
再执行它**，若 ``__init__.py`` 里 import 了 ``.plugin``，就会报
``cannot import name 'RealDiaryPlugin'``，而普通 ``import_module`` 测不出来
（RISKS R21 实测踩过）。这里复刻框架的加载路径，把那个坑挡在门外。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
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


def main() -> int:
    """跑完全部断言。"""
    # ── 0. 复刻框架加载路径（必须最先，趁 sys.modules 干净） ────────────────
    section("0. 复刻框架加载路径导入 plugin.py")
    try:
        spec = importlib.util.spec_from_file_location(
            "real_diary.plugin",
            str(PLUGIN_DIR / "plugin.py"),
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        module.__package__ = "real_diary"
        sys.modules["real_diary.plugin"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        loaded = getattr(module, "RealDiaryPlugin", None) is not None
        check("plugin.py 能被框架方式加载", loaded)
    except Exception as error:  # noqa: BLE001 - 这一节失败就是插件加载不起来
        check("plugin.py 能被框架方式加载", False, repr(error))
        print("-" * 72)
        print(f"结果：{len(_failures)} 项失败 -> {_failures}")
        return 1

    from real_diary.commands import DiaryCommand
    from real_diary.config import RealDiaryConfig
    from real_diary.handlers import ARCHIVED_EVENT, ArchiveSinkHandler, PhaseWatcherHandler

    # ── 1. manifest 元数据 ──────────────────────────────────────────────────
    section("1. manifest 元数据")
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    plugin_class = module.RealDiaryPlugin
    check("name 是 real_diary", manifest.get("name") == "real_diary", str(manifest.get("name")))
    check(
        "manifest 版本与 plugin_version 一致",
        manifest.get("version") == plugin_class.plugin_version,
        f"manifest={manifest.get('version')} plugin={plugin_class.plugin_version}",
    )
    check("署名 Aemeath", manifest.get("author") == "Aemeath", str(manifest.get("author")))
    check(
        "不写 dependencies.plugins（缺失依赖会被框架静默剔除）",
        (manifest.get("dependencies") or {}).get("plugins") == [],
        str((manifest.get("dependencies") or {}).get("plugins")),
    )

    # ── 2. api_version 声明合法 ─────────────────────────────────────────────
    section("2. api_version 逐模块声明合法")
    try:
        from src.app.plugin_system.api import PLUGIN_API_VERSIONS

        declared = manifest.get("api_version") or {}
        unknown = sorted(set(declared) - set(PLUGIN_API_VERSIONS))
        check("没有拼错的 API 模块名", not unknown, str(unknown))
        check("声明了 event_api（订阅事件要用）", "event_api" in declared)
        check("声明了 storage_api（素材落盘要用）", "storage_api" in declared)
        check("声明了 llm_api（写日记要用）", "llm_api" in declared)
    except Exception as error:  # noqa: BLE001 - 拿不到表就退化成只查格式
        check("能读到框架的 PLUGIN_API_VERSIONS 表", False, repr(error))

    # ── 3. 组件清单与 manifest include 对得上 ───────────────────────────────
    section("3. 组件清单与 manifest include 一一对应")
    config = RealDiaryConfig()
    plugin = plugin_class(config)
    components = plugin.get_components()
    actual = {
        (str(getattr(cls, "component_type", "")) or "event_handler", str(getattr(cls, "name", "")))
        for cls in components
    }
    declared_set = {
        (str(item.get("component_type")), str(item.get("component_name")))
        for item in manifest.get("include", [])
    }
    check("声明的组件都有实现", declared_set <= actual, f"缺: {sorted(declared_set - actual)}")
    check("实现的组件都已声明", actual <= declared_set, f"多: {sorted(actual - declared_set)}")
    check(
        "包含三个组件（归档订阅 / 状态采样 / 命令）",
        actual
        == {
            ("event_handler", "archive_sink"),
            ("event_handler", "phase_watcher"),
            ("command", "日记"),
        },
        str(sorted(actual)),
    )
    check(
        "订阅的是 context_archiver:archived",
        ARCHIVED_EVENT == "context_archiver:archived"
        and list(ArchiveSinkHandler.init_subscribe) == ["context_archiver:archived"],
        str(ArchiveSinkHandler.init_subscribe),
    )
    check(
        "状态采样权重 < 20（要跑在 sisp 之后）",
        int(PhaseWatcherHandler.weight) < 20,
        str(PhaseWatcherHandler.weight),
    )
    check("命令名是 日记", DiaryCommand.name == "日记", DiaryCommand.name)

    # ── 4. 用到的框架 API 能真实导入 ────────────────────────────────────────
    section("4. 用到的框架 API 真实导入")
    try:
        from src.app.plugin_system.api import event_api, llm_api, service_api, storage_api  # noqa: F401
        from src.app.plugin_system.base import (  # noqa: F401
            BaseCommand,
            BaseEventHandler,
            BasePlugin,
            BaseService,
            cmd_route,
        )
        from src.app.plugin_system.types import EventType, PermissionLevel  # noqa: F401
        from src.kernel.event import EventDecision  # noqa: F401
        from src.kernel.scheduler import TriggerType, get_unified_scheduler  # noqa: F401

        check("框架 API 全部可用", True)
    except Exception as error:  # noqa: BLE001
        check("框架 API 全部可用", False, repr(error))

    # ── 5. 插件自身模块可导入 ───────────────────────────────────────────────
    section("5. 插件模块导入")
    for name in ("collector", "composer", "config", "llm", "sources", "state"):
        try:
            __import__(f"real_diary.{name}")
            check(f"real_diary.{name} 可导入", True)
        except Exception as error:  # noqa: BLE001
            check(f"real_diary.{name} 可导入", False, repr(error))

    print("-" * 72)
    if _failures:
        print(f"结果：{len(_failures)} 项失败 -> {_failures}")
        return 1
    print(f"结果：全部通过（{_passed} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
