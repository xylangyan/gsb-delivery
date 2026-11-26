"""pypyr dynamic modules, namespaces and path discovery.

Load modules dynamically, find things on file-system.
"""
import ast
import builtins
from collections import ChainMap
import importlib
import logging
from pathlib import Path
import sys
from threading import Lock

from pypyr.errors import PyModuleNotFoundError

logger = logging.getLogger(__name__)


class _ChainMapPretendDict(ChainMap, dict):  # type: ignore
    # mypy gets confused with multiple inheritance, thus ignore
    """It's a ChainMap. But it will pass an isinstance check for dict.

    Don't Use Me. Seriously.

    The only reason for this is to use as globals with eval/exec. Because
    eval/exec only accepts dict type as globals argument.

    However, for the purposes of pypyr, rather than make a whole copy of the
    context dict each and every time a py expression evaluates, it's much
    more performance efficient to use a chainmap to chain together the dynamic
    code's global namespace and context.

    That does not mean what you see here is a good idea. It's not. It's the
    least bad of other bad options.

    Don't pickle me. Re-instantiate instead.

    There are 2 storage mechanisms here:
    - the dict instance itself
    - the ChainMap maps attribute (a list of dicts, basically.)

    The ChainMap overrides most of the accessors you'd associate with dict
    (get, set, items, etc.) because it is 1st in the mro.

    See docs/adr/0001-namespace-on-eval-and-exec.md
    """

    __slots__ = ()

    def __init__(self, *maps):
        """Initialize the dict to builtins, and the chainmap to *maps.

        Args:
            maps: mapping dict-like objects to add to the chainmap.
        """
        dict.__setitem__(self, '__builtins__', builtins.__dict__)
        super().__init__(*maps)


class ImportVisitor(ast.NodeVisitor):
    """Parse python import and import from syntax.

    Use this to parse python syntax of import/import from, import the modules
    and save the result to the imported_namespace namespace dictionary.

    Only supports absolute imports, not relative. Does not support wildcard
    style 'from x import *'. But you weren't planning on doing *that* anyway,
    I hope.

    Supports source like:
        import x
        import x as y
        import x.y
        import x.y as z
        from x import y
        from x import y as z
        from x import y, z
        from a.b import c as d, e as f

    Usage example:
        ImportVisitor().visit(ast.parse('from mod.sub import attr as alias'))

    Will result in:
        imported_namespace == {'alias': <<attr in mod.sub module>>}

    Attributes:
        imported_namespace (dict): Namespace dictionary of imported references.
    """

    def __init__(self):
        """Initialize me."""
        self.imported_namespace = {}

    def get_namespace(self, source):
        """Parse source, import modules & return namespace.

        You might as well call this instead of visit().

        Args:
            source (str): String of Python source code.

        Return:
            Namespace dictionary of imported references.
        """
        self.visit(ast.parse(source))
        return self.imported_namespace

    def _set_namespace(self, alias, obj):
        """Add imported object to namespace dictionary.

        Args:
            alias (ast.alias): Use asname for alias if it exists, else fall
                back to name.
            obj (any): Imported module or attribute like class or function.

        Returns: None
        """
        as_name = alias.asname if alias.asname else alias.name
        self.imported_namespace[as_name] = obj

    def visit_Import(self, node):
        """Process syntax nodes of form: import module."""
        for alias in node.names:
            if alias.asname:
                imported_module = importlib.import_module(alias.name)
            else:
                # if no alias, 'import mod.sub' has to bind 'mod' to the
                # imported parent obj.
                parent, dot, _ = alias.name.partition('.')
                if dot:
                    # mod.sub1.sub2 should save to namespace as parent 'mod'
                    alias.asname = parent
                    # __import__(), although discouraged, is how to get the
                    # top-level module - this is the obj that is bound by the
                    # name in the namespace
                    imported_module = __import__(alias.name)
                else:
                    imported_module = importlib.import_module(alias.name)

            self._set_namespace(alias, imported_module)

    def visit_ImportFrom(self, node):
        """Process syntax nodes of form: from parent import child."""
        if node.level > 0:
            raise TypeError("you can't use relative imports here. "
                            "use absolute imports instead.")
        imported_module = importlib.import_module(node.module)
        for alias in node.names:
            try:
                imported_obj = getattr(imported_module, alias.name)
            except AttributeError:
                # if no attribute, might be form: from mod import submod
                imported_obj = importlib.import_module(
                    f'{node.module}.{alias.name}')

            self._set_namespace(alias, imported_obj)


_sys_path_lock = Lock()
_known_dirs = set()


def add_sys_path(path):
    """Add path to sys.path if it doesn't exist in sys.path already.

    Only add paths that actually exist.

    Do this under a shared lock to prevent duplicates in sys.path.

    _known_dirs does not mean a path exists, it means the logic around
    whether to add a path or not has run.

    Args:
        path (Path-like): path to add to sys.path
    """
    if path in _known_dirs:
        return

    path_obj = path if isinstance(path, Path) else Path(path)

    if not path_obj.exists():
        _known_dirs.add(path)
        return

    # sys path doesn't accept Path
    path_str = str(path_obj)  # .resolve(True)? instead for extended paths?
    logger.debug("adding %s to sys.paths", path_str)
    with _sys_path_lock:
        if path_str not in sys.path:
            # append not insert - don't overwrite user's prior sys.path
            sys.path.append(path_str)

    _known_dirs.add(path)


def get_module(module_abs_import, pipeline_dir=None):
    """Use importlib to get the module dynamically.

    Get instance of the module specified by the module_abs_import.
    This means that module_abs_import must be resolvable from this package.

    If pipeline_dir is specified, the module resolves from pipeline_dir first.
    Modules loaded from a pipeline_dir are cached per pipeline_dir, so that
    different release directories can use same-named modules in the same
    process without leaking one release's module into another.

    Args:
        module_abs_import: string. Absolute name of module to import.
        pipeline_dir: Path-like. Optional. Resolve the module from this
            release/pipeline directory before falling back to the usual
            python import path.

    Raises:
        PyModuleNotFoundError: if module not found.

    """
    logger.debug("starting")
    logger.debug("loading module %s", module_abs_import)

    if pipeline_dir and module_in_dir(module_abs_import, pipeline_dir):
        logger.debug("loading %s from pipeline dir %s",
                     module_abs_import, pipeline_dir)
        module = _get_release_module(module_abs_import, pipeline_dir)
        logger.debug("done")
        return module

    try:
        imported_module = importlib.import_module(module_abs_import)
        logger.debug("done")
        return imported_module
    except ModuleNotFoundError as err:
        if err.name != module_abs_import:
            extended_msg = (
                f'error importing module {err.name} in {module_abs_import}')
            logger.error("Couldn't import module %s in this module: %s",
                         err.name,
                         module_abs_import)
        else:
            extended_msg = (
                f"{module_abs_import}.py should be in your pipeline dir, or "
                "in your working dir, or it should be installed in the "
                "current python env."
                "\nIf you have 'package.sub.mod' your pipeline dir should "
                "contain ./package/sub/mod.py\n"
                "If you specified 'mymodulename', your pipeline dir should "
                "contain ./mymodulename.py\n"
                "If the module is not in your pipeline dir nor in the current "
                "working dir, it must exist in your current python env - so "
                "you should have run pip install or setup.py")
            logger.error("The module doesn't exist. "
                         "Looking for a file like this: %s",
                         module_abs_import)
        raise PyModuleNotFoundError(extended_msg) from err


# region release scoped modules

_release_module_cache = {}
_release_modules_by_dir = {}
_release_roots = set()
_release_module_lock = Lock()


def clear_release_module_cache():
    """Clear all release-scoped modules and their path registrations."""
    with _release_module_lock:
        _release_module_cache.clear()
        _release_modules_by_dir.clear()
        _release_roots.clear()


def module_in_dir(module_abs_import, pipeline_dir):
    """Return True if module_abs_import exists in pipeline_dir.

    Args:
        module_abs_import: str. Absolute module name, e.g package.mod.
        pipeline_dir: Path-like. Directory to check.

    Returns:
        bool. True if the module's file or package exists in pipeline_dir.
    """
    current = Path(pipeline_dir)
    parts = module_abs_import.split('.')

    for index, part in enumerate(parts):
        is_last = index == len(parts) - 1
        module_file = current.joinpath(f'{part}.py')
        package_dir = current.joinpath(part)

        if is_last:
            return (module_file.is_file() or
                    package_dir.joinpath('__init__.py').is_file())

        if not package_dir.is_dir():
            return False

        current = package_dir

    return False


def _is_under(path, root):
    """Return True if path is root or a descendant of root."""
    try:
        path_obj = Path(path).resolve()
    except (TypeError, ValueError):
        return False

    return path_obj == root or root in path_obj.parents


def _belongs_to_release(module, roots):
    """Return True if module's file is under any registered release root."""
    module_file = getattr(module, '__file__', None)
    if not module_file:
        return False

    return any(_is_under(module_file, root) for root in roots)


def _get_release_module(module_abs_import, pipeline_dir):
    """Import and cache module_abs_import from pipeline_dir.

    The import happens with pipeline_dir at the front of sys.path. Any modules
    already loaded from a release directory, and any existing modules with the
    same top-level package name, are temporarily removed from sys.modules so
    this import cannot pick up another release's version. The previous
    sys.modules state and sys.path restore afterwards.
    """
    root = Path(pipeline_dir).resolve()
    cache_key = (str(root), module_abs_import)

    cached = _release_module_cache.get(cache_key)
    if cached is not None:
        return cached

    with _release_module_lock:
        cached = _release_module_cache.get(cache_key)
        if cached is not None:
            return cached

        _release_roots.add(root)
        top_level = module_abs_import.partition('.')[0]
        saved_modules = {}
        saved_path = list(sys.path)
        loaded_modules = {}

        try:
            for name, module in list(sys.modules.items()):
                is_top_level = (name == top_level or
                                name.startswith(f'{top_level}.'))
                if is_top_level or _belongs_to_release(
                        module, _release_roots):
                    saved_modules[name] = module
                    sys.modules.pop(name, None)

            # a previous load of this same release might have newly loaded
            # helper modules that aren't in the python namespace right now.
            release_modules = _release_modules_by_dir.get(root, {})
            for name, module in release_modules.items():
                if name not in saved_modules:
                    existing = sys.modules.get(name)
                    if existing is not None:
                        saved_modules[name] = existing
                sys.modules.pop(name, None)

            # put this release's already loaded modules back so a package is
            # initialized once per release, not once per submodule import.
            # retain any older submodule objects already loaded by callers.
            for name, module in release_modules.items():
                if name not in loaded_modules:
                    loaded_modules[name] = module
            sys.modules.update(release_modules)

            sys.path.insert(0, str(root))
            importlib.invalidate_caches()

            imported_module = importlib.import_module(module_abs_import)

            for name, module in list(sys.modules.items()):
                module_file = getattr(module, '__file__', None)
                if module_file and _is_under(module_file, root):
                    loaded_modules[name] = module
                elif name == module_abs_import:
                    loaded_modules[name] = module

            loaded_modules[module_abs_import] = imported_module

            for name in loaded_modules:
                sys.modules.pop(name, None)

            _release_modules_by_dir.setdefault(root, {}).update(
                loaded_modules)
            _release_module_cache[cache_key] = imported_module

            return imported_module
        finally:
            sys.path[:] = saved_path

            # remove anything else that might have leaked from this import
            for name, module in list(sys.modules.items()):
                if _belongs_to_release(module, _release_roots):
                    sys.modules.pop(name, None)

            sys.modules.update(saved_modules)

# endregion release scoped modules
