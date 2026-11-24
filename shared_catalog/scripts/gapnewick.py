"""Gap-aware Newick primitives for the shared catalog.

The third-party ``newick`` package turns a *missing* branch length into ``0.0``
and its ``remove_redundant_nodes`` adds lengths with plain ``+``, which would
silently turn an archived gap into a measured zero.  This module keeps the
distinction explicit:

* a length parsed from ``:0.07`` is the float ``0.07``;
* a missing length (no colon) is ``None`` and stays a gap on serialization;
* adding two edges while suppressing a unary node yields a gap only when **both**
  originals are gaps, otherwise the single measured value survives;
* support annotations attached to suppressed edges are never silently merged or
  duplicated - they are returned in a ledger for the caller to adjudicate.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Optional

import newick
from commonnexus import Nexus

GAP = None


def read_tree(path: pathlib.Path) -> newick.Node:
    """Read a tree from a plain Newick file or a NEXUS ``TREES`` block."""
    text = pathlib.Path(path).read_text(encoding="utf8")
    if text.lstrip().upper().startswith("#NEXUS"):
        nexus = Nexus(text)
        if len(nexus.TREES.trees) != 1:
            raise ValueError(f"{path}: expected exactly one NEXUS tree")
        text = nexus.TREES.trees[0].newick_string
    return newick.loads(text)[0]


def length(node: newick.Node) -> Optional[float]:
    """Return the branch length or ``None`` when the archive stores a gap."""
    if node._length is None:  # noqa: SLF001 - newick keeps the raw token here
        return GAP
    return float(node._length)  # noqa: SLF001


def add_lengths(upper: Optional[float], lower: Optional[float]) -> Optional[float]:
    """Sum two path segments while preserving gap semantics."""
    if upper is GAP and lower is GAP:
        return GAP
    if upper is GAP:
        return lower
    if lower is GAP:
        return upper
    return round(upper + lower, 10)


def _clone(node: newick.Node) -> newick.Node:
    clone = newick.Node(
        name=node.name,
        length=node._length,  # noqa: SLF001 - keep raw token, gap stays None
        comment=node.comment,
    )
    for child in node.descendants:
        clone.add_descendant(_clone(child))
    return clone


def prune(tree: newick.Node, keep: set[str]) -> newick.Node:
    """Return a deep copy containing only leaves whose name is in ``keep``."""

    def filter_node(node: newick.Node) -> Optional[newick.Node]:
        if node.is_leaf:
            return _clone(node) if node.name in keep else None
        kept_children = [c for c in (filter_node(d) for d in node.descendants) if c]
        if not kept_children:
            return None
        clone = newick.Node(name=node.name, length=node._length, comment=node.comment)  # noqa: SLF001
        for child in kept_children:
            clone.add_descendant(child)
        return clone

    filtered = filter_node(tree)
    if filtered is None:
        raise ValueError("prune removed every leaf")
    return filtered


@dataclass
class MergeRecord:
    """One suppression event: a unary node folded into its only child."""

    surviving: str
    suppressed: str
    clade: frozenset[str]
    upper_length: Optional[float]
    lower_length: Optional[float]
    combined_length: Optional[float]
    comments: list[str] = field(default_factory=list)


def suppress_degree_one(tree: newick.Node) -> tuple[newick.Node, list[MergeRecord]]:
    """Suppress unary internal nodes with gap-aware length addition."""
    root = _clone(tree)
    merges: list[MergeRecord] = []

    def handle(node: newick.Node) -> newick.Node:
        node.descendants = [handle(d) for d in node.descendants]
        while len(node.descendants) == 1 and node.ancestor is not None:
            child = node.descendants[0]
            upper, lower = length(node), length(child)
            combined = add_lengths(upper, lower)
            comments = [c for c in (node.comment, child.comment) if c]
            merges.append(MergeRecord(
                surviving=child.name or "(unlabeled)",
                suppressed=node.name or "(unlabeled)",
                clade=clade_set(child),
                upper_length=upper,
                lower_length=lower,
                combined_length=combined,
                comments=comments,
            ))
            child.length = None if combined is GAP else combined
            parent = node.ancestor
            parent.descendants = [d for d in parent.descendants if d is not node]
            child.ancestor = None
            parent.add_descendant(child)
            node = child
        return node

    # Collapse a unary root separately (it has no ancestor).
    handle(root)
    while len(root.descendants) == 1:
        only = root.descendants[0]
        upper, lower = length(root), length(only)
        combined = add_lengths(upper, lower)
        merges.append(MergeRecord(
            surviving=only.name or "(unlabeled)",
            suppressed=root.name or "(root)",
            clade=clade_set(only),
            upper_length=upper,
            lower_length=lower,
            combined_length=combined,
            comments=[c for c in (root.comment, only.comment) if c],
        ))
        only.length = None if combined is GAP else combined
        only.ancestor = None
        root = only
    return root, merges


def clade_set(node: newick.Node) -> frozenset[str]:
    return frozenset(node.get_leaf_names())


def internal_clades(tree: newick.Node) -> set[frozenset[str]]:
    return {clade_set(n) for n in tree.walk() if not n.is_leaf}


def edge_index(tree: newick.Node) -> dict[frozenset[str], tuple[Optional[float], Optional[str]]]:
    """Map each non-root edge (identified by its child clade) to length+comment."""
    index: dict[frozenset[str], tuple[Optional[float], Optional[str]]] = {}
    for node in tree.walk():
        if node.ancestor is None:
            continue
        index[clade_set(node)] = (length(node), node.comment)
    return index


def serialize(tree: newick.Node) -> str:
    """Serialize without turning gaps into zero lengths."""
    parts: list[str] = []

    def emit(node: newick.Node) -> None:
        if node.descendants:
            parts.append("(")
            for index, child in enumerate(node.descendants):
                if index:
                    parts.append(",")
                emit(child)
            parts.append(")")
        if node.name:
            parts.append(node.name)
        if node.comment:
            parts.append(f"[{node.comment}]")
        if node._length is not None:  # noqa: SLF001
            parts.append(f":{node._length}")

    emit(tree)
    parts.append(";")
    return "".join(parts)


def reconstruct(backbone: newick.Node, keep: set[str]) -> tuple[newick.Node, list[MergeRecord]]:
    """Full archive pipeline: prune undocumented leaves then suppress unary nodes."""
    pruned = prune(backbone, keep)
    return suppress_degree_one(pruned)
