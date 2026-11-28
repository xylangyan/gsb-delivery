"""Provenance-preserving merging.

The regular :class:`deepmerge.merger.Merger` returns plain values and, for
performance, deliberately re-uses references from the incoming document.  That
is the right default for configuration loading, but it is not enough when a
merge is used to *combine packets on behalf of another system*: the receiver
must be able to tell which original packet supplied every retained value.

This module provides a small, opt-in layer that:

* re-uses the built-in strategy semantics (dict merge, list append,
  append-unique, set union, type-conflict override, ...), including the
  per-path rules a work order may select;
* never aliases the input documents, so a template shared by two packaging
  requests cannot leak edits into a sibling result;
* records, for every retained leaf, the original packet that supplied it.

Values produced by :class:`TrackedMerger` compare equal to values produced by
the corresponding :class:`~deepmerge.merger.Merger`; the provenance tree is an
additional parallel structure.
"""

from __future__ import annotations

import copy
from collections.abc import Sized
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

from .extended_set import ExtendedSet

DICT_STRATEGIES = ("merge", "override")
LIST_STRATEGIES = ("append", "prepend", "append_unique", "override")
SET_STRATEGIES = ("union", "intersect", "override")
FALLBACK_STRATEGIES = ("override", "use_existing")
TYPE_CONFLICT_STRATEGIES = ("override", "use_existing", "override_if_not_empty")

_ALLOWED = {
    "dict": DICT_STRATEGIES,
    "list": LIST_STRATEGIES,
    "set": SET_STRATEGIES,
    "fallback": FALLBACK_STRATEGIES,
    "type_conflict": TYPE_CONFLICT_STRATEGIES,
}


@dataclass(frozen=True)
class PathRule:
    """Strategy selection for a path prefix.

    ``prefix`` is a tuple of dictionary keys.  The empty tuple is the global
    default.  A rule is selected for a path when its prefix matches and it is
    the longest such rule.
    """

    prefix: tuple[Any, ...] = ()
    dict_strategy: str = "merge"
    list_strategy: str = "append"
    set_strategy: str = "union"
    fallback_strategy: str = "override"
    type_conflict_strategy: str = "override"

    def __post_init__(self) -> None:
        for name, value in (
            ("dict", self.dict_strategy),
            ("list", self.list_strategy),
            ("set", self.set_strategy),
            ("fallback", self.fallback_strategy),
            ("type_conflict", self.type_conflict_strategy),
        ):
            allowed = _ALLOWED[name]
            if value not in allowed:
                raise ValueError(f"{name}={value!r} is not one of {allowed}")

    def strategy_for(self, kind: str) -> str:
        if kind == "dict":
            return self.dict_strategy
        if kind == "list":
            return self.list_strategy
        if kind == "set":
            return self.set_strategy
        if kind == "fallback":
            return self.fallback_strategy
        if kind == "type_conflict":
            return self.type_conflict_strategy
        raise ValueError(f"unknown strategy kind {kind!r}")


class MergePolicy:
    """A path-aware selection of the built-in deepmerge strategies."""

    def __init__(self, rules: Sequence[PathRule] | None = None) -> None:
        default = PathRule()
        by_prefix: dict[tuple[Any, ...], PathRule] = {default.prefix: default}
        for rule in rules or ():
            if rule.prefix in by_prefix and rule.prefix != default.prefix:
                raise ValueError(f"duplicate strategy rule for path {rule.prefix!r}")
            by_prefix[rule.prefix] = rule
        # Longest prefix first so lookup is a simple linear scan.
        self._rules = sorted(by_prefix.values(), key=lambda r: len(r.prefix), reverse=True)

    def rule_for(self, path: Sequence[Any]) -> PathRule:
        prefix = tuple(path)
        for rule in self._rules:
            if prefix[: len(rule.prefix)] == rule.prefix:
                return rule
        return self._rules[-1]

    def strategy(self, path: Sequence[Any], kind: str) -> str:
        return self.rule_for(path).strategy_for(kind)

    @classmethod
    def always(cls) -> "MergePolicy":
        return cls()

    @classmethod
    def conservative(cls) -> "MergePolicy":
        return cls(
            [
                PathRule(
                    fallback_strategy="use_existing",
                    type_conflict_strategy="use_existing",
                )
            ]
        )


def _merge_sources(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in left:
        if item not in seen:
            seen.add(item)
            out.append(item)
    for item in right:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


@dataclass
class Node:
    """A merged value plus the original packets that supplied it.

    ``children`` is a ``dict`` for mappings, a ``list`` for lists, a ``list``
    for sets, and ``None`` for scalars.  Nodes are treated as immutable once
    built; merges always create new parent nodes and never mutate children.
    """

    kind: str
    value: Any
    sources: tuple[str, ...]
    children: Any = None

    # -- construction -----------------------------------------------------
    @classmethod
    def from_value(cls, value: Any, source: str) -> "Node":
        if isinstance(value, dict):
            children = {k: cls.from_value(v, source) for k, v in value.items()}
            value = {k: child.value for k, child in children.items()}
            return cls("dict", value, (source,), children)
        if isinstance(value, list):
            children = [cls.from_value(v, source) for v in value]
            return cls("list", [child.value for child in children], (source,), children)
        if isinstance(value, set):
            children = [cls.from_value(v, source) for v in sorted(value, key=repr)]
            return cls("set", set(child.value for child in children), (source,), children)
        return cls("scalar", copy.deepcopy(value), (source,), None)

    # -- inspection -------------------------------------------------------
    def to_value(self) -> Any:
        if self.kind == "dict":
            return {k: child.to_value() for k, child in self.children.items()}
        if self.kind == "list":
            return [child.to_value() for child in self.children]
        if self.kind == "set":
            return set(child.to_value() for child in self.children)
        return self.value

    def clone(self) -> "Node":
        """Return an independent copy, so a merge cannot mutate this node."""
        if self.kind == "dict":
            children = {k: child.clone() for k, child in self.children.items()}
            return Node("dict", {k: v.value for k, v in children.items()}, self.sources, children)
        if self.kind == "list":
            children = [child.clone() for child in self.children]
            return Node("list", [v.value for v in children], self.sources, children)
        if self.kind == "set":
            children = [child.clone() for child in self.children]
            return Node("set", set(v.value for v in children), self.sources, children)
        return Node("scalar", copy.deepcopy(self.value), self.sources, None)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Node):
            return NotImplemented
        return self.kind == other.kind and self.to_value() == other.to_value()

    def origin_at(self, path: Sequence[Any]) -> tuple[str, ...]:
        node: Node = self
        for part in path:
            if node.kind == "dict":
                node = node.children[part]
            elif node.kind in ("list", "set") and isinstance(part, int):
                node = node.children[part]
            else:
                raise KeyError(path)
        return node.sources

    def iter_origins(
        self, path: tuple[Any, ...] = ()
    ) -> Iterator[tuple[tuple[Any, ...], tuple[str, ...]]]:
        """Yield ``(path, sources)`` for retained leaves and list elements."""
        if self.kind == "dict":
            if not self.children:
                yield (path, self.sources)
            for key, child in self.children.items():
                yield from child.iter_origins(path + (key,))
        elif self.kind == "list":
            if not self.children:
                yield (path, self.sources)
            for index, child in enumerate(self.children):
                if child.kind == "scalar":
                    yield (path + (index,), child.sources)
                else:
                    yield from child.iter_origins(path + (index,))
        elif self.kind == "set":
            if not self.children:
                yield (path, self.sources)
            for index, child in enumerate(self.children):
                yield (path + (index,), child.sources)
        else:
            yield (path, self.sources)

    def origin_map(self) -> dict[tuple[Any, ...], tuple[str, ...]]:
        return dict(self.iter_origins())

    def retained_origins(self, order: Sequence[str] | None = None) -> tuple[str, ...]:
        """Origins of values actually retained in the merged document.

        If ``order`` is supplied (for example the original packet order), the
        result follows it; otherwise first-appearance order is used.
        """
        found: list[str] = []
        seen: set[str] = set()
        for _, sources in self.iter_origins():
            for source in sources:
                if source not in seen:
                    seen.add(source)
                    found.append(source)
        if order is None:
            return tuple(found)
        rank = {source: index for index, source in enumerate(order)}
        return tuple(sorted(found, key=lambda source: rank.get(source, len(rank))))

    def origins_under(self, path: Sequence[Any]) -> tuple[str, ...]:
        """All origins of leaves at or below ``path``, in document order."""
        node: Node = self
        for part in path:
            if node.kind == "dict":
                if part not in node.children:
                    return ()
                node = node.children[part]
            elif node.kind in ("list", "set") and isinstance(part, int):
                if not 0 <= part < len(node.children):
                    return ()
                node = node.children[part]
            else:
                return ()
        out: list[str] = []
        seen: set[str] = set()
        for _, sources in node.iter_origins(path):
            for source in sources:
                if source not in seen:
                    seen.add(source)
                    out.append(source)
        return tuple(out)


class TrackedMerger:
    """Merge values while keeping the origin of every retained value."""

    def __init__(self, policy: MergePolicy | None = None) -> None:
        self.policy = policy or MergePolicy.always()

    def merge(self, base: Node, nxt: Node, path: tuple[Any, ...] = ()) -> Node:
        kind = self._kind(base, nxt)
        if kind in ("dict", "list", "set"):
            strategy = self.policy.strategy(path, kind)
            return self._merge_container(kind, strategy, base, nxt, path)
        if kind == "fallback":
            strategy = self.policy.strategy(path, "fallback")
            return self._merge_fallback(strategy, base, nxt)
        strategy = self.policy.strategy(path, "type_conflict")
        return self._merge_conflict(strategy, base, nxt)

    # -- dispatch mirrors Merger.value_strategy ---------------------------
    @staticmethod
    def _kind(base: Node, nxt: Node) -> str:
        if base.kind == nxt.kind and base.kind in ("dict", "list", "set"):
            return base.kind
        base_type = type(base.value)
        nxt_type = type(nxt.value)
        if isinstance(base.value, nxt_type) or isinstance(nxt.value, base_type):
            return "fallback"
        return "type_conflict"

    # -- strategies -------------------------------------------------------
    def _merge_container(
        self, kind: str, strategy: str, base: Node, nxt: Node, path: tuple[Any, ...]
    ) -> Node:
        if kind == "dict":
            return self._merge_dict(strategy, base, nxt, path)
        if kind == "list":
            return self._merge_list(strategy, base, nxt)
        return self._merge_set(strategy, base, nxt)

    def _merge_dict(
        self, strategy: str, base: Node, nxt: Node, path: tuple[Any, ...]
    ) -> Node:
        if strategy == "override":
            return self._clone(nxt)
        if strategy != "merge":
            raise ValueError(f"unsupported dict strategy {strategy!r}")
        children = dict(base.children)
        for key, child in nxt.children.items():
            if key in children:
                children[key] = self.merge(children[key], child, path + (key,))
            else:
                children[key] = self._clone(child)
        value = {key: child.value for key, child in children.items()}
        sources = _merge_sources(base.sources, nxt.sources)
        return Node("dict", value, sources, children)

    def _merge_list(self, strategy: str, base: Node, nxt: Node) -> Node:
        if strategy == "override":
            return self._clone(nxt)
        if strategy == "append":
            children = list(base.children) + [self._clone(c) for c in nxt.children]
        elif strategy == "prepend":
            children = [self._clone(c) for c in nxt.children] + list(base.children)
        elif strategy == "append_unique":
            seen = ExtendedSet([child.value for child in base.children])
            children = list(base.children)
            for child in nxt.children:
                if child.value not in seen:
                    children.append(self._clone(child))
                    seen._insert(child.value)
        else:
            raise ValueError(f"unsupported list strategy {strategy!r}")
        value = [child.value for child in children]
        sources = _merge_sources(base.sources, nxt.sources)
        if strategy == "append_unique":
            # Values dropped as duplicates are not sources of the result.
            sources = _merge_sources(
                base.sources,
                (s for child in children[len(base.children) :] for s in child.sources),
            )
        return Node("list", value, sources, children)

    def _merge_set(self, strategy: str, base: Node, nxt: Node) -> Node:
        if strategy == "override":
            return self._clone(nxt)
        if strategy == "union":
            seen = ExtendedSet([child.value for child in base.children])
            children = list(base.children)
            for child in nxt.children:
                if child.value not in seen:
                    children.append(self._clone(child))
                    seen._insert(child.value)
        elif strategy == "intersect":
            nxt_values = ExtendedSet([child.value for child in nxt.children])
            children = [child for child in base.children if child.value in nxt_values]
        else:
            raise ValueError(f"unsupported set strategy {strategy!r}")
        value = set(child.value for child in children)
        sources = _merge_sources(
            (s for child in children for s in child.sources),
            (),
        )
        return Node("set", value, sources, children)

    def _merge_fallback(self, strategy: str, base: Node, nxt: Node) -> Node:
        if strategy == "override":
            return self._clone(nxt)
        if strategy == "use_existing":
            return self._clone(base)
        raise ValueError(f"unsupported fallback strategy {strategy!r}")

    def _merge_conflict(self, strategy: str, base: Node, nxt: Node) -> Node:
        if strategy == "override":
            return self._clone(nxt)
        if strategy == "use_existing":
            return self._clone(base)
        if strategy == "override_if_not_empty":
            if nxt.value is None:
                return self._clone(base)
            if isinstance(nxt.value, Sized) and len(nxt.value) == 0:
                return self._clone(base)
            return self._clone(nxt)
        raise ValueError(f"unsupported type-conflict strategy {strategy!r}")

    # -- helpers ----------------------------------------------------------
    @classmethod
    def _clone(cls, node: Node) -> Node:
        if node.kind == "dict":
            children = {k: cls._clone(v) for k, v in node.children.items()}
            return Node("dict", {k: v.value for k, v in children.items()}, node.sources, children)
        if node.kind == "list":
            children = [cls._clone(v) for v in node.children]
            return Node("list", [v.value for v in children], node.sources, children)
        if node.kind == "set":
            children = [cls._clone(v) for v in node.children]
            return Node("set", set(v.value for v in children), node.sources, children)
        return Node("scalar", node.value, node.sources, None)

    def merge_documents(
        self,
        base: Any,
        nxt: Any,
        base_origin: str = "base",
        nxt_origin: str = "next",
    ) -> Node:
        return self.merge(
            Node.from_value(base, base_origin), Node.from_value(nxt, nxt_origin)
        )

    def reduce_documents(
        self, documents: Sequence[Any], origins: Sequence[str] | None = None
    ) -> Node:
        if not documents:
            raise ValueError("at least one document is required")
        if origins is None:
            origins = [f"#{index}" for index in range(len(documents))]
        if len(origins) != len(documents):
            raise ValueError("origins and documents must have the same length")
        result = Node.from_value(documents[0], origins[0])
        for document, origin in zip(documents[1:], origins[1:]):
            result = self.merge(result, Node.from_value(document, origin))
        return result
