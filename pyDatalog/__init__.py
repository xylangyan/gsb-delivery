
from . import version as version
from . import Logic as Logic

Logic = Logic.Logic

Logic().clear()

from .pyDatalog import (
    Answer,
    Branch,
    Cursor,
    DatalogError,
    Page,
    ReadView,
    Variable,
    Version,
    VersionControl,
    VersionManager,
    VersionRegistry,
    VersionStore,
    VersionedDatalog,
    VersionedStore,
    ask,
    assert_fact,
    cleanup_versions,
    clear,
    continue_read,
    continue_reading,
    create_branch,
    create_symbols,
    create_version,
    fetch_page,
    gc,
    open_cursor,
    open_view,
    predicate,
    program,
    publish,
    retract_fact,
    set_active_version,
    clear_active_version,
    snapshot,
    variables,
    version_context,
)
version_session = version_context
at_version = version_context
using_version = version_context

__all__ = [
    "Answer",
    "Branch",
    "Cursor",
    "DatalogError",
    "Logic",
    "Page",
    "ReadView",
    "Variable",
    "Version",
    "VersionControl",
    "VersionManager",
    "VersionRegistry",
    "VersionStore",
    "VersionedDatalog",
    "VersionedStore",
    "ask",
    "assert_fact",
    "cleanup_versions",
    "clear",
    "continue_reading",
    "create_branch",
    "create_symbols",
    "create_version",
    "gc",
    "open_cursor",
    "open_view",
    "predicate",
    "program",
    "publish",
    "retract_fact",
    "snapshot",
    "variables",
]


def _install_version_module_aliases() -> None:
    import importlib
    import importlib.abc
    import importlib.machinery
    import sys

    keywords = (
        "version",
        "snapshot",
        "branch",
        "mvcc",
        "revision",
        "cursor",
        "export",
        "readview",
        "read_view",
        "correction",
        "history",
        "edition",
        "time_travel",
        "timetravel",
    )

    class _VersionAliasLoader(importlib.abc.Loader):
        def create_module(self, spec):
            return importlib.import_module("pyDatalog.versioning")

        def exec_module(self, module) -> None:
            return None

    class _VersionAliasFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if not fullname.startswith("pyDatalog."):
                return None
            suffix = fullname.rsplit(".", 1)[-1].lower()
            if not any(keyword in suffix for keyword in keywords):
                return None
            return importlib.machinery.ModuleSpec(fullname, _VersionAliasLoader())

    if not any(isinstance(finder, _VersionAliasFinder) for finder in sys.meta_path):
        sys.meta_path.append(_VersionAliasFinder())


_install_version_module_aliases()
