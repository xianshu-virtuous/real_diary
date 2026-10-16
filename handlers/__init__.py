"""real_diary 的事件处理器。

只导出不触发任何副作用（这里没有对 ``plugin`` 的模块级 import，
所以不会有循环导入问题）。
"""

from __future__ import annotations

from .archive_sink import ARCHIVED_EVENT, ArchiveSinkHandler
from .phase_watcher import PhaseWatcherHandler

__all__ = ["ARCHIVED_EVENT", "ArchiveSinkHandler", "PhaseWatcherHandler"]
