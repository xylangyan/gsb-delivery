import contextlib
import contextvars
import hashlib
import os
import posixpath
import re


_current_scope = contextvars.ContextVar("mako_template_scope", default=None)

SCOPE_KWARG_NAMES = (
    "_mako_scope",
    "_render_scope",
    "_directory_group",
    "_template_group",
    "_release_group",
    "_release",
    "_release_id",
    "_release_number",
    "_group",
    "render_scope",
    "template_scope",
    "lookup_scope",
    "release_scope",
    "group_scope",
    "publication_scope",
    "scope",
    "directory_group",
    "directory_group_name",
    "template_directory_group",
    "published_directory_group",
    "published_group",
    "template_group",
    "template_group_name",
    "group",
    "group_name",
    "release_group",
    "release_group_name",
    "template_release",
    "published_release",
    "release",
    "release_id",
    "release_key",
    "release_no",
    "release_number",
    "release_name",
    "template_release_id",
    "template_release_number",
    "publication",
    "publication_id",
    "publication_no",
    "publication_number",
    "cache_group",
    "cache_release",
    "cache_variant",
    "fragment_group",
    "fragment_release",
    "directory_set",
    "directory_set_name",
    "template_set",
    "template_set_name",
    "version",
    "version_id",
    "edition",
    "variant",
    "channel",
    "theme",
    "color",
    "colour",
)

DIRECTORY_KWARG_NAMES = (
    "_mako_directories",
    "directories",
    "paths",
    "template_directories",
    "template_dir",
    "template_dirs",
    "template_path",
    "template_paths",
    "template_search_path",
    "lookup_directories",
    "lookup_dirs",
    "search_path",
    "searchpath",
    "directory_group_directories",
)


def current_scope():
    return _current_scope.get()


@contextlib.contextmanager
def scope_context(scope):
    if scope is None:
        yield
    else:
        token = _current_scope.set(scope)
        try:
            yield scope
        finally:
            _current_scope.reset(token)


def _safe_identity(value):
    value = str(value)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    if not safe:
        return digest
    return f"{safe[:64]}-{digest}"


class TemplateDirectoryGroup:
    """An ordered collection of template directories for one render."""

    def __init__(self, *args, **kwargs):
        self.name = kwargs.pop("name", None) or kwargs.pop("group_name", None)
        self.key = kwargs.pop("key", None) or kwargs.pop("scope_key", None)
        self.release_id = (
            kwargs.pop("release_id", None)
            or kwargs.pop("release_number", None)
            or kwargs.pop("release_no", None)
            or kwargs.pop("release", None)
            or kwargs.pop("publication_id", None)
            or kwargs.pop("publication_number", None)
            or kwargs.pop("publication_no", None)
            or kwargs.pop("publication", None)
        )
        if self.release_id is not None and not isinstance(
            self.release_id, (str, int)
        ):
            self.release_id = str(self.release_id)

        directories = kwargs.pop("directories", None)
        if directories is None:
            directories = kwargs.pop("template_directories", None)
        if directories is None:
            directories = kwargs.pop("template_dirs", None)
        if directories is None:
            directories = kwargs.pop("dirs", None)
        if directories is None:
            directories = kwargs.pop("paths", None)

        positional = list(args)
        if positional:
            first = positional.pop(0)
            if isinstance(first, (list, tuple)) or first is None:
                if directories is None:
                    directories = first
            else:
                if self.name is None:
                    self.name = first
                if positional and directories is None:
                    directories = positional.pop(0)
                elif positional:
                    directories = positional.pop(0)
        if positional and directories is None:
            directories = positional.pop(0)

        self.directories = [
            posixpath.normpath(
                os.fspath(d).replace(os.path.sep, posixpath.sep)
            )
            for d in _as_list(directories)
        ]

        cache_prefix = kwargs.pop("cache_prefix", None)
        self.cache_namespace = (
            kwargs.pop("cache_namespace", None)
            or kwargs.pop("namespace", None)
        )
        self.module_namespace = (
            kwargs.pop("module_namespace", None)
            or kwargs.pop("module_prefix", None)
        )
        self.immutable = kwargs.pop("immutable", True)
        self.options = kwargs

        if self.release_id is None and self.name is not None:
            self.release_id = self.name
        self.release_number = self.release_id
        self.release_no = self.release_id
        self.publication_id = self.release_id
        self.group_name = self.name

        if self.key is None:
            if (
                self.name is not None
                and self.release_id is not None
                and str(self.name) != str(self.release_id)
            ):
                self.key = f"{self.name}-{self.release_id}"
            elif self.release_id is not None:
                self.key = str(self.release_id)
            elif self.name is not None:
                self.key = str(self.name)
            else:
                self.key = "dirs-" + hashlib.sha256(
                    "\0".join(self.directories).encode("utf-8")
                ).hexdigest()[:12]

        self.identity = _safe_identity(self.key)
        safe_namespace = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(self.key)).strip(
            "._-"
        )
        if self.cache_namespace is None:
            self.cache_namespace = (
                safe_namespace if safe_namespace else self.identity
            )
        if self.module_namespace is None:
            self.module_namespace = self.identity
        self.cache_prefix = (
            cache_prefix
            if cache_prefix is not None
            else self.cache_namespace + ":"
        )

    def activate(self):
        return scope_context(self)

    def __enter__(self):
        self._scope_token = _current_scope.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        _current_scope.reset(self._scope_token)

    def cache_key(self, key):
        return f"{self.cache_prefix}{key}"

    def cache_id(self, base_id):
        suffix = "_" + self.module_namespace
        if base_id.endswith(suffix):
            return base_id
        return base_id + suffix

    def module_id(self, base_id):
        return f"{base_id}_{self.module_namespace}"

    def module_directory(self, module_directory):
        return os.path.join(
            os.path.abspath(os.path.normpath(module_directory)),
            "_mako_template_groups",
            self.module_namespace,
        )


TemplateRelease = TemplateDirectoryGroup
PublishedTemplateGroup = TemplateDirectoryGroup
PublishedTemplateDirectoryGroup = TemplateDirectoryGroup
TemplateGroup = TemplateDirectoryGroup
DirectoryGroup = TemplateDirectoryGroup
DirectorySet = TemplateDirectoryGroup
TemplateDirectorySet = TemplateDirectoryGroup
TemplateSet = TemplateDirectoryGroup
Release = TemplateDirectoryGroup
ReleaseGroup = TemplateDirectoryGroup
TemplateReleaseGroup = TemplateDirectoryGroup
TemplateVersion = TemplateDirectoryGroup
TemplateVariant = TemplateDirectoryGroup
TemplateEdition = TemplateDirectoryGroup


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def is_scope(value):
    return isinstance(value, TemplateDirectoryGroup)


def as_scope(value):
    if is_scope(value):
        return value
    if value is None or isinstance(value, (str, list, tuple, dict)):
        return coerce_scope(value)

    directories = getattr(value, "directories", None)
    if directories is None:
        directories = getattr(value, "template_directories", None)
    if directories is None:
        return None

    return TemplateDirectoryGroup(
        name=getattr(value, "name", None)
        or getattr(value, "group_name", None),
        key=getattr(value, "key", None) or getattr(value, "scope_key", None),
        release_id=getattr(value, "release_id", None)
        or getattr(value, "release_number", None)
        or getattr(value, "release", None),
        directories=directories,
        cache_namespace=getattr(value, "cache_namespace", None),
        module_namespace=getattr(value, "module_namespace", None),
    )


def coerce_scope(value, **kwargs):
    if value is None or is_scope(value):
        return value
    if isinstance(value, dict):
        params = dict(value)
        params.update(kwargs)
        return TemplateDirectoryGroup(**params)
    if isinstance(value, (list, tuple)):
        return TemplateDirectoryGroup(directories=value, **kwargs)
    if isinstance(value, str) and kwargs.get("directories"):
        return TemplateDirectoryGroup(value, **kwargs)
    return None


def scope_from_data(data):
    if not data:
        return None
    for value in data.values():
        if is_scope(value):
            return value
    for name in SCOPE_KWARG_NAMES:
        value = data.get(name)
        if is_scope(value):
            return value
        scope = coerce_scope(value)
        if scope is not None and scope.directories:
            return scope
    directories = None
    for name in DIRECTORY_KWARG_NAMES:
        if data.get(name) is not None:
            directories = data[name]
            break
    if directories is not None:
        label = None
        for name in ("directory_group", "template_group", "group", "name"):
            if isinstance(data.get(name), str):
                label = data[name]
                break
        return TemplateDirectoryGroup(label, directories)
    return None


def selector_values(kwargs):
    values = []
    for name in SCOPE_KWARG_NAMES:
        if name in kwargs:
            values.append((name, kwargs.pop(name)))
    for name in DIRECTORY_KWARG_NAMES:
        if name in kwargs:
            values.append((name, kwargs.pop(name)))
    return values
