"""real_diary 插件包。

**这里一个字都别 import**（RISKS R21 实测踩过）：框架加载入口时是先登记
``real_diary.plugin`` 再执行它，若本文件里写 ``from .plugin import RealDiaryPlugin``，
就会在 ``plugin.py`` 还没执行完时触发父包导入 → 报
``cannot import name 'RealDiaryPlugin'``，整个插件加载失败，而且普通
``import_module`` 的自检还测不出来。
"""

from __future__ import annotations

__all__: list[str] = []
