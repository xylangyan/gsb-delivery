from pyDatalog import versioning as _versioning
from pyDatalog.versioning import *  # noqa: F401,F403
from pyDatalog.versioning import (
    Branch,
    Cursor,
    Page,
    ReadView,
    Version,
    VersionControl,
    VersionManager,
    VersionRegistry,
    VersionStore,
    VersionedDatalog,
    VersionedStore,
    ask,
    continue_reading,
    create_branch,
    create_version,
    gc,
    open_cursor,
    open_view,
    publish,
)

DatalogVersionStore = VersionManager
VersionedAsk = VersionManager
VersionedReader = ReadView
ExportCursor = Cursor
PageCursor = Cursor

__all__ = [
    "Branch",
    "Cursor",
    "DatalogVersionStore",
    "ExportCursor",
    "Page",
    "PageCursor",
    "ReadView",
    "Version",
    "VersionControl",
    "VersionManager",
    "VersionRegistry",
    "VersionStore",
    "VersionedAsk",
    "VersionedDatalog",
    "VersionedReader",
    "VersionedStore",
    "ask",
    "continue_reading",
    "create_branch",
    "create_version",
    "gc",
    "open_cursor",
    "open_view",
    "publish",
]


def __getattr__(name):
    return _versioning.__getattr__(name)
