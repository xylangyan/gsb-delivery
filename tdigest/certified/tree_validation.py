"""
Aggregation-tree verification harness.

The same batch of windows is reduced along two differently shaped trees with
intermediate aggregation nodes. A correct mergeable summary must produce:

* byte-identical canonical summaries at both roots, and
* identical certification outcomes (same certificate hash, or the same set of
  named gap sources).

Transport model: every window belongs to one partition (its stable routing
key). At-least-once delivery may repeat a message, but repeats travel down the
*same* partition path and are therefore de-duplicated at the first
aggregation node, before that node is pruned. The two trees consume the same
partitioned message queues; only the fan-out above the partitions differs.
"""

from __future__ import annotations

from .certified_digest import CertifiedDigest, CertificationError


class TreeValidationResult(object):

    def __init__(self, root_a, root_b, canonical_equal,
                 certification_equal, details):
        self.root_a = root_a
        self.root_b = root_b
        self.canonical_equal = canonical_equal
        self.certification_equal = certification_equal
        self.details = details

    def __bool__(self):
        return self.canonical_equal and self.certification_equal

    __nonzero__ = __bool__


def _partition_messages(windows, partitions, retries):
    """Route each window (plus its redeliveries) to one stable partition."""
    queues = [[] for _ in range(partitions)]
    for index, window in enumerate(windows):
        target = index % partitions
        copies = 1 + int(retries.get(index, 0))
        queues[target].extend([window] * copies)
    return [q for q in queues if q]


def _reduce(partitions, group_sizes, prune_internal):
    # First level: each partition is one aggregation node. Redeliveries of
    # the same attempt are de-duplicated here while full records are present.
    nodes = []
    for messages in partitions:
        merged = messages[0].merge(*messages[1:])
        if prune_internal:
            merged = merged.prune()
        nodes.append(merged)

    while len(nodes) > 1:
        next_level = []
        start = 0
        idx = 0
        while start < len(nodes):
            size = group_sizes[idx % len(group_sizes)]
            group = nodes[start:start + size]
            start += size
            idx += 1
            if not group:
                continue
            merged = group[0].merge(*group[1:])
            if prune_internal and len(group) > 1:
                merged = merged.prune()
            next_level.append(merged)
        nodes = next_level
    return nodes[0]


def validate_two_trees(windows, partitions=3, tree_a_sizes=(2,),
                       tree_b_sizes=(3,), retries=None, prune_internal=True):
    """Run the same windows through two aggregation trees.

    ``windows`` is a list of :class:`CertifiedDigest` leaf summaries.
    ``retries`` maps a window index to the number of extra identical
    deliveries. Both trees receive the same partitioned message multiset.
    """
    retries = retries or {}
    queues = _partition_messages(windows, partitions, retries)

    root_a = _reduce([list(q) for q in queues], tree_a_sizes,
                     prune_internal)
    root_b = _reduce([list(q) for q in queues], tree_b_sizes,
                     not prune_internal)

    canonical_a = root_a.to_canonical_bytes()
    canonical_b = root_b.to_canonical_bytes()
    canonical_equal = canonical_a == canonical_b

    cert_a = _certification_view(root_a)
    cert_b = _certification_view(root_b)
    certification_equal = cert_a == cert_b

    details = {
        "tree_a_certified": root_a.certified,
        "tree_b_certified": root_b.certified,
        "tree_a_gaps": root_a.gap_sources,
        "tree_b_gaps": root_b.gap_sources,
        "tree_a_certificate": cert_a,
        "tree_b_certificate": cert_b,
        "tree_a_pruned": root_a.is_pruned,
        "tree_b_pruned": root_b.is_pruned,
    }
    return TreeValidationResult(root_a, root_b, canonical_equal,
                                certification_equal, details)


def _certification_view(root):
    if not root.certified:
        return {
            "certified": False,
            "gaps": sorted(
                (g["window_id"], g["reason"]) for g in root.gap_sources),
        }
    try:
        cert = root.certificate()
    except CertificationError as exc:
        return {"certified": False,
                "gaps": sorted((g["window_id"], g["reason"])
                               for g in exc.gap_sources)}
    return {"certified": True, "certificate_hash": cert["certificate_hash"],
            "n": cert["n"], "epsilon": cert["epsilon"]}
