"""
Incremental, block based extraction of product relations.

A product page is fetched block by block.  Each block is a self contained
HTML fragment whose skeleton position (document order, in-site identifiers,
vocabulary declarations and base addresses) is known even before the body of
the block has been downloaded.  This module keeps the *relations* found inside
the blocks that have arrived so far, and updates only the relations affected
when a late (or replaced) block shows up.  Relations contributed by blocks that
were already archived are never recomputed.

The public entry point is :class:`BlockRelationStore`.  Every block keeps a
stable :class:`BlockKey`, independent of the arrival order, so the identity of
the original DOM nodes is preserved across versions, including cross-block
references and reference cycles.

Relations mirror the W3C microdata model (``itemscope`` / ``itemprop`` /
``itemref``) but are expressed as plain, JSON serialisable records.  Once every
block of a version is present, :meth:`BlockRelationStore.verify_with_extract`
runs the existing :func:`extruct.extract` path over the assembled HTML so the
incremental result can be checked against the reference implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from lxml import etree

from extruct.utils import parse_html

# Relation kinds.
PROPERTY = "property"
REFERENCE = "reference"
ANCHOR_DEF = "anchor-definition"

# What happened to a relation after a block (re)arrived.
ADDED = "added"
REMOVED = "removed"
REBOUND = "rebound"


@dataclass(frozen=True)
class BlockKey:
    """Stable identity of a block, independent of arrival order.

    ``block_id`` is the in-site identifier carried by the block itself (its
    element ``id``) when it has one.  Anonymous blocks are addressed by their
    skeleton position, the child index path from the assembled document root.
    """

    block_id: str | None
    path: tuple[int, ...]

    def __str__(self) -> str:
        if self.block_id is not None:
            return f"block#{self.block_id}"
        return "block@" + ",".join(str(part) for part in self.path)


@dataclass(frozen=True)
class NodeId:
    """Identity of a DOM node, stable across versions and block arrivals.

    Public nodes keep the element ``id`` they declare in the source document.
    Nodes without a public identifier are addressed by the block they live in
    plus their relative position inside that block, so two anonymous nodes with
    identical content stay distinguishable by their original parent.
    """

    block: BlockKey
    element_id: str | None
    rel_path: tuple[int, ...]

    @property
    def is_anonymous(self) -> bool:
        return self.element_id is None

    def to_ref(self) -> dict[str, object]:
        """JSON serialisable pointer back to the original DOM position."""
        ref: dict[str, object] = {
            "block": str(self.block),
            "block_path": list(self.block.path),
            "path": list(self.rel_path),
        }
        if self.element_id is not None:
            ref["id"] = self.element_id
        return ref


@dataclass
class Relation:
    """A single relation extracted from an arrived block."""

    kind: str
    source: NodeId
    name: str
    target: NodeId | None = None
    value: object | None = None
    anchor: str | None = None
    origin_block: BlockKey | None = None
    origin_node: NodeId | None = None
    # block scope (base url / vocabulary) captured from the ancestor skeleton
    base_url: str | None = None
    vocabulary: str | None = None
    # materialised target resolution, kept so dangling links stay readable
    target_ref: dict[str, object] | None = None
    target_resolved: bool = False

    def key(self) -> tuple[object, ...]:
        """Identity of the relation, independent of its current binding."""
        if self.kind == REFERENCE:
            return (REFERENCE, _node_key(self.source), self.name, self.anchor)
        if self.kind == ANCHOR_DEF:
            return (ANCHOR_DEF, self.anchor, _node_key(self.source))
        return (PROPERTY, _node_key(self.source), self.name, _value_key(self.value))

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "kind": self.kind,
            "source": self.source.to_ref(),
            "name": self.name,
        }
        if self.target is not None:
            data["target"] = self.target.to_ref()
        if self.value is not None:
            data["value"] = self.value
        if self.anchor is not None:
            data["anchor"] = self.anchor
        if self.origin_block is not None:
            data["origin_block"] = str(self.origin_block)
        if self.origin_node is not None:
            data["origin_node"] = self.origin_node.to_ref()
        if self.base_url is not None:
            data["base_url"] = self.base_url
        if self.vocabulary is not None:
            data["vocabulary"] = self.vocabulary
        data["target_resolved"] = self.target_resolved
        if self.target_ref is not None:
            data["target_ref"] = self.target_ref
        return data


@dataclass
class BlockState:
    """Everything known about a single block."""

    key: BlockKey
    arrived: bool = False
    html: str | None = None
    base_url: str | None = None
    relations: list[Relation] = field(default_factory=list)
    anchor_defs: dict[str, NodeId] = field(default_factory=dict)
    # anchors this block used to expose, kept after the block is replaced so an
    # older version stays readable
    removed_anchor_defs: dict[str, NodeId] = field(default_factory=dict)
    version: int = 0


@dataclass
class RelationChange:
    """A relation that was genuinely added, removed or rebound."""

    action: str
    relation: Relation
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "action": self.action,
            "relation": self.relation.to_dict(),
        }
        if self.before is not None:
            data["before"] = self.before
        if self.after is not None:
            data["after"] = self.after
        return data


def _node_key(node_id: NodeId) -> tuple[object, ...]:
    return (
        node_id.element_id,
        node_id.block.block_id,
        node_id.block.path,
        node_id.rel_path,
    )


def _binding(rel: Relation) -> dict[str, object]:
    return {
        "target_ref": rel.target_ref,
        "target_resolved": rel.target_resolved,
    }


def _value_key(value: object) -> object:
    if isinstance(value, list):
        return ("list", tuple(_value_key(item) for item in value))
    if isinstance(value, dict):
        return (
            "dict",
            tuple(sorted((key, _value_key(val)) for key, val in value.items())),
        )
    return ("scalar", value)


def _rel_path(node: etree._Element) -> tuple[int, ...]:
    path: list[int] = []
    current: etree._Element | None = node
    while current is not None and current.getparent() is not None:
        parent = current.getparent()
        assert parent is not None
        path.append(list(parent).index(current))
        current = parent
    path.reverse()
    return tuple(path)


def _local_path(node: etree._Element, block_root: etree._Element) -> tuple[int, ...]:
    full = _rel_path(node)
    root_path = _rel_path(block_root)
    return full[len(root_path):]


def _first_word(value: str | None) -> str | None:
    if not value:
        return None
    for token in value.split():
        return token
    return None


class BlockRelationStore:
    """Accumulate product relations from blocks that arrive incrementally.

    The store is versioned.  A block is fed through :meth:`add_skeleton` while
    it is still missing its body, and through :meth:`ingest_block` once its
    HTML is available (the same call can both create the skeleton and ingest
    it).  Replacing a block only recomputes the relations of that block and the
    bindings that point at the anchors it defines.
    """

    def __init__(self, base_url: str | None = None):
        self.default_base_url = base_url
        self._blocks: dict[BlockKey, BlockState] = {}
        self._version = 0
        # relation identity -> relation, across every arrived block
        self._relations: dict[tuple[object, ...], Relation] = {}
        # anchor -> (defining block, node), the currently live definition
        self._anchor_index: dict[str, tuple[BlockKey, NodeId]] = {}

    # ------------------------------------------------------------------
    # Skeleton and block ingestion
    # ------------------------------------------------------------------
    def add_skeleton(
        self,
        block_id: str | None,
        path: tuple[int, ...] | list[int],
        base_url: str | None = None,
    ) -> BlockKey:
        """Register the skeleton position of a block whose body is pending."""
        key = BlockKey(block_id, tuple(path))
        if key not in self._blocks:
            self._blocks[key] = BlockState(
                key=key, base_url=base_url or self.default_base_url
            )
        elif base_url is not None:
            self._blocks[key].base_url = base_url
        return key

    def ingest_block(
        self,
        html: str,
        block_id: str | None,
        path: tuple[int, ...] | list[int],
        base_url: str | None = None,
    ) -> list[RelationChange]:
        """Add a block that now has content, or replace an existing one.

        Returns the relations that were genuinely added, removed or rebound
        because of this block.  Relations owned by every other block are left
        untouched, even when one of their references is rebound.
        """
        key = self.add_skeleton(block_id, path, base_url)
        state = self._blocks[key]
        effective_base = state.base_url or self.default_base_url

        self._version += 1
        state.version = self._version

        previous = {rel.key(): rel for rel in state.relations}
        self._detach_block_anchors(state)
        for rel_key in previous:
            self._relations.pop(rel_key, None)
        state.relations = []
        state.anchor_defs = {}
        state.arrived = True
        state.html = html

        tree = parse_html(html, encoding="UTF-8")
        block_root = tree
        extracted = self._extract_block(block_root, key, effective_base)
        state.relations = extracted
        for rel in extracted:
            self._relations[rel.key()] = rel

        self._index_block_anchors(state)

        changes = self._diff_relations(previous, extracted)
        changes.extend(self._rebind_affected(key))
        return changes

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def known_blocks(self) -> list[BlockKey]:
        return list(self._blocks.keys())

    def pending_blocks(self) -> list[BlockKey]:
        return [key for key, state in self._blocks.items() if not state.arrived]

    def arrived_blocks(self) -> list[BlockKey]:
        return [key for key, state in self._blocks.items() if state.arrived]

    def relations(
        self, block: BlockKey | None = None, include_unresolved: bool = False
    ) -> list[Relation]:
        """Return the currently known relations, optionally for one block.

        Pending blocks contribute no relations: only their skeleton position is
        retained.  Reference relations carry their last known target binding
        (``target_ref``) and whether the target currently resolves.

        By default a cross-block reference whose anchor no longer exists in the
        current version is left out: the new version only keeps associations
        that have an actual target.  Pass ``include_unresolved=True`` to still
        read those records (their retained ``target_ref`` points back at the
        target they used to have, so an older version stays readable).
        """
        candidates = (
            list(self._blocks[block].relations)
            if block is not None and block in self._blocks
            else list(self._relations.values())
        )
        if include_unresolved:
            return candidates
        return [rel for rel in candidates if self._is_active(rel)]

    def relation_dicts(
        self, block: BlockKey | None = None, include_unresolved: bool = False
    ) -> list[dict[str, object]]:
        return [
            rel.to_dict()
            for rel in self.relations(block, include_unresolved=include_unresolved)
        ]

    def item_relations(
        self, node: NodeId, include_unresolved: bool = False
    ) -> list[Relation]:
        """All relations whose source item is ``node`` (its product graph)."""
        return [
            rel
            for rel in self.relations(include_unresolved=include_unresolved)
            if _node_key(rel.source) == _node_key(node)
        ]

    @staticmethod
    def _is_active(rel: Relation) -> bool:
        # only anchor based references can dangle; nested items always resolve
        if (
            rel.kind == REFERENCE
            and rel.anchor is not None
            and not rel.target_resolved
        ):
            return False
        return True

    def anchor(self, name: str) -> NodeId | None:
        entry = self._anchor_index.get(name)
        return entry[1] if entry else None

    def get_block(self, key: BlockKey) -> BlockState | None:
        return self._blocks.get(key)

    # ------------------------------------------------------------------
    # Full document verification and version semantics
    # ------------------------------------------------------------------
    def assemble_html(self) -> str:
        """Assemble the arrived blocks into one document, in skeleton order."""
        ordered = sorted(self._blocks.values(), key=lambda state: state.key.path)
        parts = ["<!DOCTYPE html><html><body>"]
        for state in ordered:
            if state.arrived and state.html is not None:
                parts.append(state.html)
        parts.append("</body></html>")
        return "".join(parts)

    def is_complete(self) -> bool:
        return bool(self._blocks) and all(
            state.arrived for state in self._blocks.values()
        )

    def verify_with_extract(
        self, **extract_kwargs: Any
    ) -> dict[str, list[dict[str, Any]]]:
        """Run the existing extruct path over the assembled full HTML.

        Only meaningful once every block has arrived, which guarantees the
        assembled document is the complete version.  ``extract_kwargs`` are
        forwarded to :func:`extruct.extract` (``base_url`` defaults to the one
        given to the store).
        """
        if not self.is_complete():
            raise RuntimeError(
                "cannot verify: blocks are still missing their content: "
                + ", ".join(str(key) for key in self.pending_blocks())
            )
        import extruct

        extract_kwargs.setdefault("base_url", self.default_base_url)
        return extruct.extract(self.assemble_html(), **extract_kwargs)

    # ------------------------------------------------------------------
    # Extraction of a single block
    # ------------------------------------------------------------------
    def _extract_block(
        self, block_root: etree._Element, block: BlockKey, base_url: str | None
    ) -> list[Relation]:
        relations: list[Relation] = []
        scopes = block_root.xpath("descendant-or-self::*[@itemscope]")
        for scope in scopes:
            source = self._node_id(block, block_root, scope)
            vocabulary = _first_word(scope.get("itemtype"))
            scope_base = self._effective_base(scope, base_url)

            for ref_name in scope.get("itemref", "").split():
                relations.append(
                    self._make_reference(
                        block,
                        block_root,
                        source,
                        scope,
                        ref_name,
                        vocabulary,
                        scope_base,
                    )
                )

            for prop in self._own_properties(scope):
                relations.extend(
                    self._property_relations(
                        block,
                        block_root,
                        source,
                        scope,
                        prop,
                        vocabulary,
                        scope_base,
                    )
                )

        for node in block_root.xpath("descendant-or-self::*[@id]"):
            anchor = node.get("id")
            if not anchor:
                continue
            target = self._node_id(block, block_root, node)
            nearest = self._nearest_scope(node)
            itemtype = nearest.get("itemtype") if nearest is not None else None
            relations.append(
                Relation(
                    kind=ANCHOR_DEF,
                    source=target,
                    name=anchor,
                    anchor=anchor,
                    origin_block=block,
                    origin_node=target,
                    base_url=self._effective_base(node, base_url),
                    vocabulary=_first_word(itemtype),
                )
            )
        return relations

    def _make_reference(
        self,
        block: BlockKey,
        block_root: etree._Element,
        source: NodeId,
        scope: etree._Element,
        ref_name: str,
        vocabulary: str | None,
        scope_base: str | None,
    ) -> Relation:
        origin = self._node_id(block, block_root, scope)
        return Relation(
            kind=REFERENCE,
            source=source,
            name=ref_name,
            anchor=ref_name,
            origin_block=block,
            origin_node=origin,
            base_url=scope_base,
            vocabulary=vocabulary,
        )

    def _property_relations(
        self,
        block: BlockKey,
        block_root: etree._Element,
        source: NodeId,
        scope: etree._Element,
        prop: etree._Element,
        vocabulary: str | None,
        scope_base: str | None,
    ) -> list[Relation]:
        prop_names = prop.get("itemprop", "").split()
        origin = self._node_id(block, block_root, prop)
        prop_base = self._effective_base(prop, scope_base)

        if prop.get("itemscope") is not None:
            target = self._node_id(block, block_root, prop)
            return [
                Relation(
                    kind=REFERENCE,
                    source=source,
                    name=name,
                    target=target,
                    origin_block=block,
                    origin_node=origin,
                    base_url=prop_base,
                    vocabulary=vocabulary,
                )
                for name in prop_names
            ]

        value = self._text_value(prop, prop_base)
        return [
            Relation(
                kind=PROPERTY,
                source=source,
                name=name,
                value=value,
                origin_block=block,
                origin_node=origin,
                base_url=prop_base,
                vocabulary=vocabulary,
            )
            for name in prop_names
        ]

    def _own_properties(self, scope: etree._Element) -> list[etree._Element]:
        props = scope.xpath(".//*[@itemprop]")
        own: list[etree._Element] = []
        for prop in props:
            parent_scope = prop.xpath("ancestor::*[@itemscope][1]")
            if parent_scope and parent_scope[0] is scope:
                own.append(prop)
        return own

    def _text_value(self, node: etree._Element, base_url: str | None) -> object:
        tag = node.tag
        if tag == "meta":
            return node.get("content", "")
        if tag in ("a", "area", "link"):
            return urljoin(base_url or "", node.get("href", "").strip())
        if tag in ("img", "audio", "video", "source", "iframe", "embed", "track"):
            return urljoin(base_url or "", node.get("src", "").strip())
        if tag in ("data", "meter"):
            return node.get("value", "")
        if tag == "time":
            return node.get("datetime", "")
        if tag == "object":
            return urljoin(base_url or "", node.get("data", "").strip())
        if node.get("content"):
            return node.get("content")
        return " ".join(node.text_content().split())

    # ------------------------------------------------------------------
    # Identity, anchors and binding
    # ------------------------------------------------------------------
    def _node_id(
        self, block: BlockKey, block_root: etree._Element, node: etree._Element
    ) -> NodeId:
        return NodeId(
            block=block,
            element_id=node.get("id"),
            rel_path=_local_path(node, block_root),
        )

    def _nearest_scope(self, node: etree._Element) -> etree._Element | None:
        scopes = node.xpath("ancestor-or-self::*[@itemscope][1]")
        return scopes[0] if scopes else None

    def _effective_base(
        self, node: etree._Element, fallback: str | None
    ) -> str | None:
        bases = node.xpath("ancestor-or-self::*/@xml:base")
        if bases:
            return urljoin(fallback or "", str(bases[-1]))
        return fallback

    def _index_block_anchors(self, state: BlockState) -> None:
        for rel in state.relations:
            if rel.kind == ANCHOR_DEF and rel.anchor is not None:
                state.anchor_defs[rel.anchor] = rel.source
                self._anchor_index[rel.anchor] = (state.key, rel.source)

    def _detach_block_anchors(self, state: BlockState) -> None:
        state.removed_anchor_defs = dict(state.anchor_defs)
        for anchor in state.anchor_defs:
            current = self._anchor_index.get(anchor)
            if current is not None and current[0] == state.key:
                self._anchor_index.pop(anchor, None)

    def _resolve(self, rel: Relation) -> tuple[NodeId | None, dict[str, object], bool]:
        # Only references carry a binding.  Anchor based references (itemref)
        # always follow the live anchor index; nested items resolve directly.
        if rel.kind != REFERENCE:
            return None, {}, False
        if rel.anchor is not None:
            entry = self._anchor_index.get(rel.anchor)
            if entry is None:
                # the anchor disappeared: keep the last known target so an
                # older version stays readable, but mark it unresolved
                old_ref = rel.target_ref
                return rel.target, old_ref if old_ref is not None else {}, False
            target = entry[1]
            return target, target.to_ref(), True
        if rel.target is not None:
            return rel.target, rel.target.to_ref(), True
        return None, {}, False

    def _rebind_affected(self, changed_block: BlockKey) -> list[RelationChange]:
        affected_anchors = set(self._blocks[changed_block].anchor_defs)
        affected_anchors.update(
            self._blocks[changed_block].removed_anchor_defs
        )
        changes: list[RelationChange] = []
        for rel in self._relations.values():
            if rel.kind != REFERENCE or rel.anchor is None:
                continue
            if rel.anchor not in affected_anchors:
                continue
            before = _binding(rel)
            target, ref, resolved = self._resolve(rel)
            rel.target = target
            rel.target_ref = ref
            rel.target_resolved = resolved
            after = _binding(rel)
            if before != after:
                if before["target_resolved"] and not resolved:
                    action = REMOVED
                elif not before["target_resolved"] and resolved:
                    action = ADDED
                else:
                    action = REBOUND
                changes.append(
                    RelationChange(
                        action=action, relation=rel, before=before, after=after
                    )
                )
        return changes

    def _diff_relations(
        self,
        previous: dict[tuple[object, ...], Relation],
        current: list[Relation],
    ) -> list[RelationChange]:
        changes: list[RelationChange] = []
        current_keys = {rel.key() for rel in current}
        for rel_key, old in previous.items():
            if rel_key not in current_keys:
                changes.append(RelationChange(action=REMOVED, relation=old))
        for rel in current:
            target, ref, resolved = self._resolve(rel)
            rel.target = target
            rel.target_ref = ref
            rel.target_resolved = resolved
            previous_rel = previous.get(rel.key())
            if previous_rel is None:
                changes.append(RelationChange(action=ADDED, relation=rel))
            elif _binding(previous_rel) != _binding(rel):
                changes.append(
                    RelationChange(
                        action=REBOUND,
                        relation=rel,
                        before=_binding(previous_rel),
                        after=_binding(rel),
                    )
                )
        return changes
