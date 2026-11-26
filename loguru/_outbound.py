"""Projection and masking of log records sent to "outbound" sinks."""

import copy
import re

from ._colorizer import Colorizer

_STANDARD_KEYS = (
    "elapsed",
    "exception",
    "extra",
    "file",
    "function",
    "level",
    "line",
    "message",
    "module",
    "name",
    "process",
    "template",
    "thread",
    "time",
)

_PATH_PATTERN = re.compile(r'[^.\[\]]+|\[(?:[^"\']*[^"\']|"[^"]*"|\'[^\']*\')\]')

_MISSING = object()


def parse_path(path, *, extra_only=False):
    if not isinstance(path, str):
        raise TypeError("The field path should be a string, not: '%s'" % type(path).__name__)
    if not path:
        raise ValueError("The field path should not be empty")

    parts = []
    for match in _PATH_PATTERN.finditer(path):
        part = match.group(0)
        if part.startswith("[") and part.endswith("]"):
            part = part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
                part = part[1:-1]
            elif part.isdigit():
                part = int(part)
        if part == "":
            raise ValueError("The field path contains an empty key: '%s'" % path)
        parts.append(part)

    if not parts:
        raise ValueError("The field path should not be empty")

    if isinstance(parts[0], int):
        raise ValueError("The field path can't start with an index: '%s'" % path)

    if extra_only:
        if parts[0] != "extra":
            parts.insert(0, "extra")
    elif parts[0] not in _STANDARD_KEYS:
        parts.insert(0, "extra")

    return parts


def _rebuild_tuple(value, items):
    if type(value) is tuple:
        return items
    try:
        return type(value)(*items)
    except TypeError:
        return items


def _copy_container(value, memo):
    if isinstance(value, dict):
        if id(value) in memo:
            return memo[id(value)]
        copied = {}
        memo[id(value)] = copied
        for key, item in value.items():
            copied[key] = _copy_container(item, memo)
        return copied
    if isinstance(value, list):
        if id(value) in memo:
            return memo[id(value)]
        copied = []
        memo[id(value)] = copied
        for item in value:
            copied.append(_copy_container(item, memo))
        return copied
    if isinstance(value, tuple):
        if id(value) in memo:
            return memo[id(value)]
        items = tuple(_copy_container(item, memo) for item in value)
        rebuilt = _rebuild_tuple(value, items)
        memo[id(value)] = rebuilt
        return rebuilt
    return value


def copy_record(record):
    """Return a copy of the record whose mutable containers are independent."""
    copied = _copy_container(record, {})
    for key in ("file", "level", "process", "thread", "exception"):
        value = copied.get(key)
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        clone = copy.copy(value)
        if hasattr(value, "__dict__"):
            clone.__dict__.update(value.__dict__)
        copied[key] = clone
    return copied


class _Trie:
    __slots__ = ("children", "terminal")

    def __init__(self):
        self.children = {}
        self.terminal = False

    def add(self, parts):
        if not parts:
            self.terminal = True
            return
        head, tail = parts[0], parts[1:]
        child = self.children.get(head)
        if child is None:
            child = _Trie()
            self.children[head] = child
        child.add(tail)

    def __getstate__(self):
        return self.children, self.terminal

    def __setstate__(self, state):
        self.children, self.terminal = state


def _project(value, trie, memo):
    if trie.terminal:
        return _copy_container(value, memo)

    if isinstance(value, dict):
        if id(value) in memo:
            return memo[id(value)]
        copied = {}
        memo[id(value)] = copied
        for key, child in trie.children.items():
            if key not in value:
                continue
            copied[key] = _project(value[key], child, {})
        return copied

    if isinstance(value, list):
        if id(value) in memo:
            return memo[id(value)]
        copied = []
        memo[id(value)] = copied
        index_children = {k: v for k, v in trie.children.items() if isinstance(k, int)}
        if not index_children:
            for item in value:
                copied.append(_project(item, trie, memo))
            return copied
        for index, item in enumerate(value):
            child = index_children.get(index)
            if child is None:
                continue
            copied.append(_project(item, child, memo))
        return copied

    return _copy_container(value, memo)


def _replace(value, parts, replacement, memo):
    if not parts:
        return replacement

    if isinstance(value, dict):
        if id(value) in memo:
            return memo[id(value)]
        copied = dict(value)
        memo[id(value)] = copied
        key = parts[0]
        if key in value:
            copied[key] = _replace(value[key], parts[1:], replacement, memo)
        return copied

    if isinstance(value, list):
        if id(value) in memo:
            return memo[id(value)]
        copied = list(value)
        memo[id(value)] = copied
        index = parts[0]
        if isinstance(index, int) and 0 <= index < len(value):
            copied[index] = _replace(value[index], parts[1:], replacement, memo)
        return copied

    if isinstance(value, tuple):
        index = parts[0]
        if isinstance(index, int) and 0 <= index < len(value):
            items = list(value)
            items[index] = _replace(value[index], parts[1:], replacement, memo)
            return _rebuild_tuple(value, tuple(items))
        return value

    return value


def _remove(value, parts, memo):
    if not parts:
        return value

    if isinstance(value, dict):
        if id(value) in memo:
            return memo[id(value)]
        key = parts[0]
        if key not in value:
            return value
        copied = dict(value)
        memo[id(value)] = copied
        if len(parts) == 1:
            del copied[key]
        else:
            copied[key] = _remove(value[key], parts[1:], memo)
        return copied

    if isinstance(value, list):
        if id(value) in memo:
            return memo[id(value)]
        index = parts[0]
        if isinstance(index, int) and 0 <= index < len(value):
            copied = list(value)
            memo[id(value)] = copied
            if len(parts) == 1:
                del copied[index]
            else:
                copied[index] = _remove(value[index], parts[1:], memo)
            return copied
        return value

    return value


class MessageContext:
    """State needed to re-render the message of a single logging call."""

    __slots__ = ("args", "captured", "colors", "kwargs", "template", "with_record")

    def __init__(self, template, args, kwargs, colors, captured, with_record=False):
        self.template = template
        self.args = args
        self.kwargs = kwargs
        self.colors = colors
        self.captured = captured
        self.with_record = with_record


def _flatten_into(mapping, flattened, prefix=""):
    for key, value in mapping.items():
        path = "%s%s" % (prefix, key)
        flattened[path] = value
        if isinstance(value, dict) and value:
            _flatten_into(value, flattened, path + ".")


class Outbound:
    """Describe how a record is projected before being sent to an "outbound" sink.

    Parameters
    ----------
    fields : iterable of str, optional
        Columns of the ``extra`` dict which are allowed to reach the sink. If not provided,
        all the columns are kept. A path can target nested dictionaries (e.g.
        ``"summary.station"``) or the whole ``extra`` dict (``"extra"``).
    exclude : iterable of str, optional
        Field paths removed from the projected record. Unlike ``fields``, paths can also
        target top-level record keys (e.g. ``"file.path"``).
    mask : str, iterable of str or dict, optional
        Field paths whose values are replaced. If a mapping is provided, it associates each
        path to its own replacement value. Paths are resolved against the record, but values
        which were also used to format the message are masked in the message text too.
    mask_value : any, optional
        Default replacement value used when ``mask`` is not a mapping.
    """

    __slots__ = ("_exclude", "_fields", "_mask", "mask_value")

    def __init__(self, fields=None, exclude=None, mask=None, mask_value="***"):
        self._fields = self._parse_fields(fields)
        self._exclude = self._parse_paths(exclude, "exclude")
        self._mask = self._parse_mask(mask, mask_value)
        self.mask_value = mask_value

    def __repr__(self):
        return "Outbound(fields=%r, exclude=%r, mask=%r, mask_value=%r)" % (
            self._fields,
            self._exclude,
            self._mask,
            self.mask_value,
        )

    @staticmethod
    def _parse_fields(fields):
        if fields is None:
            return None
        if isinstance(fields, str):
            fields = [fields]
        try:
            paths = [parse_path(path, extra_only=True) for path in fields]
        except TypeError:
            raise TypeError("The outbound 'fields' should be an iterable of strings") from None
        if not paths:
            raise ValueError("The outbound 'fields' should not be empty")
        return [path[1:] for path in paths]

    @staticmethod
    def _parse_paths(paths, name):
        if paths is None:
            return None
        if isinstance(paths, str):
            paths = [paths]
        try:
            return [parse_path(path) for path in paths]
        except TypeError:
            raise TypeError("The outbound '%s' should be an iterable of strings" % name) from None

    @staticmethod
    def _parse_mask(mask, mask_value):
        if mask is None:
            return None
        if isinstance(mask, str):
            mask = [mask]
        if isinstance(mask, dict):
            flattened = {}
            _flatten_into(mask, flattened)
            return [(parse_path(path), value) for path, value in flattened.items()]
        try:
            return [(parse_path(path), mask_value) for path in mask]
        except TypeError:
            raise TypeError(
                "The outbound 'mask' should be a string, an iterable of strings or a dict"
            ) from None

    @property
    def fields(self):
        return None if self._fields is None else [".".join(map(str, path)) for path in self._fields]

    @property
    def exclude(self):
        return (
            None if self._exclude is None else [".".join(map(str, path)) for path in self._exclude]
        )

    @property
    def mask(self):
        return (
            None
            if self._mask is None
            else {(".".join(map(str, path))): value for path, value in self._mask}
        )

    def project(self, record):
        projected = _copy_container(record, {})

        if self._fields is not None:
            trie = _Trie()
            for path in self._fields:
                trie.add(path)
            extra = record.get("extra")
            projected["extra"] = _project(extra, trie, {}) if isinstance(extra, dict) else {}

        if self._exclude is not None:
            for path in self._exclude:
                projected = _remove(projected, path, {})

        if self._mask is not None:
            for path, replacement in self._mask:
                projected = _replace(projected, path, replacement, {})

        return projected

    @staticmethod
    def _message_mask_value(mask):
        for path, replacement in mask or ():
            if path == ["message"]:
                return replacement
        return _MISSING

    def _masked_arguments(self, context, projected):
        args = context.args
        kwargs = dict(context.kwargs)

        if context.with_record:
            kwargs["record"] = projected

        if context.captured:
            extra = projected.get("extra")
            if not isinstance(extra, dict):
                extra = {}
            for key in context.captured:
                if key in extra:
                    kwargs[key] = extra[key]
                else:
                    kwargs.pop(key, None)

        if self._mask is not None:
            for path, replacement in self._mask:
                memo = {}
                if path[0] == "extra":
                    key_path = path[1:]
                    if not key_path:
                        continue
                    key = key_path[0]
                    if key in kwargs:
                        kwargs[key] = _replace(kwargs[key], key_path[1:], replacement, memo)
                elif path[0] == "record":
                    key_path = path[1:]
                    if not key_path or "record" not in kwargs:
                        continue
                    kwargs["record"] = _replace(kwargs["record"], key_path, replacement, memo)
                elif path[0] in _STANDARD_KEYS:
                    key = path[0]
                    if key in kwargs:
                        kwargs[key] = _replace(kwargs[key], path[1:], replacement, memo)

        return args, kwargs

    def build_message(self, context, projected, *, colorize, ansi_level):
        """Return a re-rendered ``(text, colored_message)`` for the outbound record."""
        if context is None:
            return None, None

        if self._fields is None and self._mask is None:
            return None, None

        mask_value = self._message_mask_value(self._mask)
        if mask_value is not _MISSING:
            text = str(mask_value)
            if context.colors and colorize:
                colored = Colorizer.prepare_simple_message(text)
                return colored.stripped, colored
            return text, None

        if not context.args and not context.kwargs and not context.with_record:
            return None, None

        args, kwargs = self._masked_arguments(context, projected)
        template = context.template

        try:
            if context.colors:
                colored = Colorizer.prepare_message(template, args, kwargs)
                text = colored.stripped
                if colorize:
                    return text, colored
                return text, None
            text = template.format(*args, **kwargs)
            return text, None
        except (KeyError, IndexError, AttributeError, ValueError, TypeError):
            raise ValueError(
                "The outbound log message could not be formatted with the projected arguments.\n"
                "Verify that the message template only references fields permitted by the "
                "outbound contract."
            ) from None

    def __getstate__(self):
        return (self._fields, self._exclude, self._mask, self.mask_value)

    def __setstate__(self, state):
        self._fields, self._exclude, self._mask, self.mask_value = state


class CallableOutbound:
    """Adapt a user function taking a record and returning its outbound projection."""

    __slots__ = ("function",)

    def __init__(self, function):
        self.function = function

    def project(self, record):
        projected = self.function(record)
        if not isinstance(projected, dict):
            raise TypeError(
                "The outbound function should return a record dict, not: '%s'"
                % type(projected).__name__
            )
        return projected

    def build_message(self, context, projected, *, colorize, ansi_level):
        return None, None


_FIELD_ALIASES = ("fields", "columns", "allow", "allowed", "approved", "public")
_EXCLUDE_ALIASES = ("exclude", "deny", "hidden")
_MASK_ALIASES = ("mask", "redact", "redacted", "sensitive")
_MASK_VALUE_ALIASES = ("mask_value", "replacement", "redacted_with", "mask_with")


def _normalize_config(config):
    normalized = {}
    for key, value in config.items():
        if key in _FIELD_ALIASES:
            normalized.setdefault("fields", value)
        elif key in _EXCLUDE_ALIASES:
            normalized.setdefault("exclude", value)
        elif key in _MASK_ALIASES:
            normalized.setdefault("mask", value)
        elif key in _MASK_VALUE_ALIASES:
            normalized.setdefault("mask_value", value)
        else:
            normalized[key] = value
    return normalized


def make_outbound(value):
    if value is None:
        return None
    if isinstance(value, (Outbound, CallableOutbound)):
        return value
    if isinstance(value, dict):
        try:
            return Outbound(**_normalize_config(value))
        except TypeError as e:
            raise TypeError("Invalid outbound configuration: %s" % e) from None
    if callable(value):
        return CallableOutbound(value)
    raise TypeError(
        "Invalid outbound configuration, it should be an 'Outbound' instance, a dict or "
        "a function, not: '%s'" % type(value).__name__
    )
