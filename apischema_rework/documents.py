__all__ = [
    "deep_copy",
    "delete_path",
    "get_object_attr",
    "get_path",
    "has_path",
    "set_path",
]

from copy import deepcopy
from typing import Any

from .model import Missing, Path


def deep_copy(document: Any) -> Any:
    return deepcopy(document)


def has_path(document: Any, path: Path) -> bool:
    current = document
    for key in path:
        if isinstance(current, dict):
            if key not in current:
                return False
            current = current[key]
        elif isinstance(current, list) and isinstance(key, int):
            if key >= len(current):
                return False
            current = current[key]
        else:
            return False
    return True


def get_path(document: Any, path: Path) -> Any:
    """Return the value at ``path`` or :data:`Missing` when it is absent."""

    current = document
    for key in path:
        if isinstance(current, dict):
            if key not in current:
                return Missing
            current = current[key]
        elif isinstance(current, list) and isinstance(key, int):
            if key >= len(current):
                return Missing
            current = current[key]
        else:
            return Missing
    return current


def get_object_attr(obj: Any, path: Path) -> Any:
    """Read an imported object at its warehouse attribute path."""

    current = obj
    for key in path:
        if isinstance(current, dict):
            current = current[key]
        elif isinstance(current, (list, tuple)) and isinstance(key, int):
            current = current[key]
        else:
            current = getattr(current, key)
    return current


def set_path(document: Any, path: Path, value: Any) -> None:
    current = document
    for key in path[:-1]:
        if isinstance(current, dict):
            nxt = current.get(key)
            if not isinstance(nxt, (dict, list)):
                nxt = {}
                current[key] = nxt
            current = nxt
        elif isinstance(current, list) and isinstance(key, int):
            while len(current) <= key:
                current.append({})
            nxt = current[key]
            if not isinstance(nxt, (dict, list)):
                nxt = {}
                current[key] = nxt
            current = nxt
        else:
            raise TypeError(f"cannot descend into {path} on {type(current).__name__}")
    last = path[-1]
    if isinstance(current, list) and isinstance(last, int):
        while len(current) <= last:
            current.append(None)
    current[last] = value


def delete_path(document: Any, path: Path) -> bool:
    current = document
    for key in path[:-1]:
        if isinstance(current, dict):
            if key not in current:
                return False
            current = current[key]
        elif isinstance(current, list) and isinstance(key, int):
            if key >= len(current):
                return False
            current = current[key]
        else:
            return False
    last = path[-1]
    if isinstance(current, dict):
        if last not in current:
            return False
        del current[last]
        return True
    if isinstance(current, list) and isinstance(last, int) and last < len(current):
        current.pop(last)
        return True
    return False
