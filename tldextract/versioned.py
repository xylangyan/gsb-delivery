"""Versioned Public Suffix List lookups, built from sparse rule changes.

Consumers that re-run reports against small revisions of the same large
suffix list can build each revision from a sparse set of rule changes,
without re-enumerating the unchanged rules. Every version is an immutable,
structurally shared trie: applying a change only copies the nodes along the
changed rules' paths. Trie nodes are interned by content, so two versions
that reach the same rules by different paths share one prepared lookup.

>>> store = SuffixListStore()
>>> baseline, _ = store.load_full(["ck", "*.ck"], ["!www.ck", "blogspot.com"])
>>> revised, stats = store.apply(baseline, [remove_private("!www.ck")])
>>> stats.rules_accessed
1
>>> prepared = store.prepare(revised)
>>> prepared.host_keys("a.www.ck", include_psl_private_domains=True).tenant_key
'a.www.ck'
>>> store.prepare(baseline).host_keys(
...     "a.www.ck", include_psl_private_domains=True
... ).tenant_key
'www.ck'

Full lists can still be loaded explicitly with
:meth:`SuffixListStore.load_full`. Sparse updates never re-read or
re-enumerate the baseline, and unchanged versions are never rebuilt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from .tldextract import _decode_punycode

__all__ = [
    "HostKeys",
    "PreparedSuffixList",
    "RuleChange",
    "Section",
    "SuffixListStore",
    "SuffixListVersion",
    "UpdateStats",
    "add_private",
    "add_public",
    "move_rule",
    "remove_private",
    "remove_public",
]


class Section(Enum):
    """The section of the suffix list a rule belongs to."""

    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True)
class RuleChange:
    """A single sparse change: whether one rule is present in one section."""

    rule: str
    section: Section
    present: bool


def add_public(rule: str) -> RuleChange:
    """Add a rule to the public section."""
    return RuleChange(rule, Section.PUBLIC, True)


def add_private(rule: str) -> RuleChange:
    """Add a rule to the private section."""
    return RuleChange(rule, Section.PRIVATE, True)


def remove_public(rule: str) -> RuleChange:
    """Remove a rule from the public section."""
    return RuleChange(rule, Section.PUBLIC, False)


def remove_private(rule: str) -> RuleChange:
    """Remove a rule from the private section."""
    return RuleChange(rule, Section.PRIVATE, False)


def move_rule(
    rule: str, from_section: Section, to_section: Section
) -> tuple[RuleChange, RuleChange]:
    """Move a rule's text between sections, as a remove plus an add."""
    return (
        RuleChange(rule, from_section, False),
        RuleChange(rule, to_section, True),
    )


@dataclass(frozen=True)
class UpdateStats:
    """What one version-building operation actually touched.

    `rules_accessed` counts the rule items read: the sparse change entries
    for `apply`, or the full list for `load_full`. `nodes_visited` counts
    existing trie nodes traversed, and `nodes_created` the new trie nodes
    built. For a sparse update, all of these scale with the number of
    changes, not with the size of the list.
    """

    rules_accessed: int
    nodes_visited: int
    nodes_created: int
    nodes_reused: int
    rule_count: int


@dataclass(frozen=True)
class HostKeys:
    """Lookup keys derived from a hostname under one suffix list version.

    `tenant_key` is the top domain under the (possibly private) public
    suffix, identifying a tenant of a shared platform. `organization_key`
    is the top domain under the registry suffix, identifying the
    organization that registered with a registrar. They differ only when
    the matched suffix is a private domain.
    """

    tenant_key: str
    organization_key: str
    suffix: str
    registry_suffix: str
    is_private: bool


class _Node:
    """An immutable, interned suffix trie node."""

    __slots__ = ("matches", "node_id", "private_end", "public_end")

    def __init__(
        self,
        node_id: int,
        matches: dict[str, _Node],
        public_end: bool,
        private_end: bool,
    ) -> None:
        self.node_id = node_id
        self.matches = matches
        self.public_end = public_end
        self.private_end = private_end

    @property
    def is_private(self) -> bool:
        """Whether this node ends a private rule but not a public one."""
        return self.private_end and not self.public_end


def _is_end(node: _Node, include_private: bool) -> bool:
    return node.public_end or (include_private and node.private_end)


@dataclass(frozen=True)
class SuffixListVersion:
    """An immutable snapshot of suffix rules, shared by content.

    Versions with identical rules are equal and share one prepared lookup,
    regardless of the path of changes that produced them.
    """

    _root: _Node = field(repr=False)
    _store: SuffixListStore = field(repr=False)
    rule_count: int

    @property
    def version_id(self) -> int:
        """A content-addressed identifier: identical rules, identical ID."""
        return self._root.node_id

    def apply(
        self, changes: Iterable[RuleChange]
    ) -> tuple[SuffixListVersion, UpdateStats]:
        """Apply sparse rule changes, returning the new version and stats."""
        return self._store.apply(self, changes)

    def prepare(self) -> PreparedSuffixList:
        """Return this version's prepared lookup, shared by content."""
        return self._store.prepare(self)


class PreparedSuffixList:
    """A ready-to-query view of one suffix list version."""

    def __init__(self, root: _Node) -> None:
        """Wrap the version's immutable trie root."""
        self._root = root

    def suffix_index(
        self, spl: list[str], include_psl_private_domains: bool = False
    ) -> tuple[tuple[int, _Node], tuple[int, _Node]] | None:
        """Return the public and registry suffix label indexes and their nodes.

        Mirrors `tldextract`'s suffix index lookup. `spl` is the hostname
        split into labels. Returns `None` if no suffix matches.
        """
        node = self._root
        suffix_node = registry_node = node
        suffix_idx = registry_idx = label_idx = len(spl)
        for label in reversed(spl):
            decoded_label = _decode_punycode(label)
            child = node.matches.get(decoded_label)
            if child is not None:
                label_idx -= 1
                node = child
                if _is_end(node, include_psl_private_domains):
                    suffix_idx = label_idx
                    suffix_node = node
                    if node.public_end:
                        registry_node = node
                        registry_idx = label_idx
                continue

            wildcard = node.matches.get("*")
            if wildcard is not None and _is_end(wildcard, include_psl_private_domains):
                exception = node.matches.get("!" + decoded_label)
                is_exception = exception is not None and _is_end(
                    exception, include_psl_private_domains
                )
                return (
                    (label_idx if is_exception else label_idx - 1, wildcard),
                    (registry_idx, registry_node),
                )

            break

        if suffix_idx == len(spl):
            return None

        return ((suffix_idx, suffix_node), (registry_idx, registry_node))

    def host_keys(
        self, hostname: str, include_psl_private_domains: bool = False
    ) -> HostKeys | None:
        """Return the tenant and organization keys for a hostname.

        `include_psl_private_domains` temporarily treats private rules as
        suffixes for this lookup only; it never mutates the version.
        Returns `None` if the hostname has no known suffix.
        """
        labels = hostname.split(".")
        indexes = self.suffix_index(labels, include_psl_private_domains)
        if indexes is None:
            return None

        (public_idx, public_node), (registry_idx, _) = indexes
        suffix = ".".join(labels[public_idx:])
        is_private = public_node.is_private
        effective_registry_idx = registry_idx if is_private else public_idx
        return HostKeys(
            tenant_key=".".join(labels[public_idx - 1 :]) if public_idx > 0 else "",
            organization_key=(
                ".".join(labels[effective_registry_idx - 1 :])
                if effective_registry_idx > 0
                else ""
            ),
            suffix=suffix,
            registry_suffix=".".join(labels[effective_registry_idx:]),
            is_private=is_private,
        )


class _Stats:
    """Mutable counters accumulated while the store builds trie structure."""

    def __init__(self) -> None:
        self.rules_accessed = 0
        self.nodes_visited = 0
        self.nodes_created = 0
        self.nodes_reused = 0


class SuffixListStore:
    """Owns the interned trie nodes shared by all suffix list versions."""

    def __init__(self) -> None:
        """Create an empty store."""
        self._nodes: dict[tuple[bool, bool, frozenset[tuple[str, _Node]]], _Node] = {}
        self._prepared: dict[_Node, PreparedSuffixList] = {}
        self._next_node_id = 0
        self._empty = self._intern({}, False, False, None)

    def load_full(
        self,
        public_rules: Iterable[str],
        private_rules: Iterable[str] = (),
    ) -> tuple[SuffixListVersion, UpdateStats]:
        """Explicitly load a complete suffix list, returning its version and stats."""
        changes = [add_public(rule) for rule in public_rules]
        changes += [add_private(rule) for rule in private_rules]
        return self.apply(SuffixListVersion(self._empty, self, 0), changes)

    def apply(
        self, version: SuffixListVersion, changes: Iterable[RuleChange]
    ) -> tuple[SuffixListVersion, UpdateStats]:
        """Apply sparse rule changes, returning the new version and stats.

        Only the changed rules and the trie nodes along their paths are
        touched; unchanged rules are never re-read or re-enumerated, and
        the input version is left intact for concurrent readers.
        """
        stats = _Stats()
        root = version._root
        rule_count = version.rule_count
        for change in changes:
            stats.rules_accessed += 1
            labels = change.rule.split(".")
            labels.reverse()
            root, delta = self._set_rule(
                root, labels, 0, change.section, change.present, stats
            )
            rule_count += delta
        return SuffixListVersion(root, self, rule_count), UpdateStats(
            rules_accessed=stats.rules_accessed,
            nodes_visited=stats.nodes_visited,
            nodes_created=stats.nodes_created,
            nodes_reused=stats.nodes_reused,
            rule_count=rule_count,
        )

    def prepare(self, version: SuffixListVersion) -> PreparedSuffixList:
        """Return the prepared lookup for a version.

        Versions with identical rules share one prepared lookup, keyed by
        content, not by the versions they derived from.
        """
        root = version._root
        prepared = self._prepared.get(root)
        if prepared is None:
            prepared = PreparedSuffixList(root)
            self._prepared[root] = prepared
        return prepared

    def _intern(
        self,
        matches: dict[str, _Node],
        public_end: bool,
        private_end: bool,
        stats: _Stats | None,
    ) -> _Node:
        key = (public_end, private_end, frozenset(matches.items()))
        node = self._nodes.get(key)
        if node is not None:
            if stats is not None:
                stats.nodes_reused += 1
            return node
        node = _Node(self._next_node_id, dict(matches), public_end, private_end)
        self._next_node_id += 1
        self._nodes[key] = node
        if stats is not None:
            stats.nodes_created += 1
        return node

    def _set_rule(
        self,
        node: _Node,
        labels: list[str],
        depth: int,
        section: Section,
        present: bool,
        stats: _Stats,
    ) -> tuple[_Node, int]:
        stats.nodes_visited += 1
        if depth == len(labels):
            if section is Section.PUBLIC:
                delta = int(present) - int(node.public_end)
                return self._intern(
                    node.matches, present, node.private_end, stats
                ), delta
            delta = int(present) - int(node.private_end)
            return self._intern(node.matches, node.public_end, present, stats), delta

        label = labels[depth]
        child = node.matches.get(label)
        if child is None:
            if not present:
                return node, 0
            child = self._empty
        new_child, delta = self._set_rule(
            child, labels, depth + 1, section, present, stats
        )
        if new_child is child:
            return node, delta

        new_matches = dict(node.matches)
        if new_child is self._empty:
            del new_matches[label]
        else:
            new_matches[label] = new_child
        return self._intern(
            new_matches, node.public_end, node.private_end, stats
        ), delta
