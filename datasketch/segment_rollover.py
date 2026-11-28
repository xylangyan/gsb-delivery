"""Phased retirement of segmented MinHash/LSH index generations.

This module coordinates taking individual *segments* of an old MinHash/LSH
generation offline while a new generation is still being filled.  The workflow
is deliberately split into two phases:

1. **Prepare** (:meth:`SegmentRolloverManager.prepare_retirement`): a retirement
   plan is drawn up from a fixed input permit.  Raw token shards may only be
   consulted for document versions named in that permit, and the plan records
   the successor location of every still-valid document version for every
   historical group served by the segment.
2. **Publish** (:meth:`SegmentRolloverManager.publish`): the plan is re-validated
   against the *current* live state and atomically committed as a new published
   version.  Publishing never consults raw tokens, so no extra token access can
   be granted at publish time.  If any served group would lose its last query
   entry point, publication is blocked and the offending group/document
   versions are reported while the segment stays online.

Queries always run against an immutable :class:`PublishedView`, so the routing
table and the membership relations observed by one query come from the same
published version, even when another publish happens while the query is in
flight.

Two recall rules are enforced when building current groups:

* LSH is only a candidate recall mechanism, queried separately for each index
  generation (each generation keeps its own sketch parameters).
* A recalled pair only becomes an in-group relation when a currently-valid
  token-shard confirmation backs it.  Conversely, a historical confirmation
  stays valid even when LSH fails to recall the pair.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Hashable, Optional

from datasketch.lsh import MinHashLSH
from datasketch.minhash import MinHash

OLD_GENERATION = "old"
NEW_GENERATION = "new"


class TokenAccessDeniedError(PermissionError):
    """Raised when raw token shards are consulted outside the input permit."""


class RetirementBlockedError(Exception):
    """Raised when publishing a retirement would orphan document versions."""

    def __init__(self, segment: str, blocked: list["BlockedEntry"]):
        self.segment = segment
        self.blocked = blocked
        details = ", ".join(
            f"group {entry.group_id} -> {entry.document_version.doc_id} "
            f"v{entry.document_version.version}"
            for entry in blocked
        )
        super().__init__(f"Retirement of segment {segment!r} blocked: {details}")


@dataclass(frozen=True)
class DocumentVersion:
    """A specific version of a document; identity is ``(doc_id, version)``."""

    doc_id: Hashable
    version: Hashable

    @property
    def key(self) -> tuple[Hashable, Hashable]:
        return self.doc_id, self.version


@dataclass(frozen=True)
class TokenConfirmation:
    """A token-shard confirmation record between two document versions.

    The record carries the raw token shards attested at issuance time so that a
    preparation phase can re-verify them under an explicit access permit.
    """

    left: DocumentVersion
    right: DocumentVersion
    source_id: Hashable
    left_tokens: frozenset
    right_tokens: frozenset
    meets_threshold: bool
    issued_epoch: int

    def involves(self, document_version: DocumentVersion) -> bool:
        return document_version in (self.left, self.right)

    def other(self, document_version: DocumentVersion) -> Optional[DocumentVersion]:
        if document_version == self.left:
            return self.right
        if document_version == self.right:
            return self.left
        return None


@dataclass(frozen=True)
class GroupIssuance:
    """Immutable snapshot of a historical group at the moment it was issued."""

    group_id: Hashable
    serving_segments: frozenset[str]
    members: frozenset[DocumentVersion]
    confirmations: tuple[TokenConfirmation, ...]
    issued_epoch: int


@dataclass(frozen=True)
class BlockedEntry:
    group_id: Hashable
    document_version: DocumentVersion
    reason: str


@dataclass
class RetirementPlan:
    """A prepared (but not yet published) segment retirement."""

    segment: str
    prepared_epoch: int
    permit: frozenset[DocumentVersion]
    placements: dict[Hashable, dict[DocumentVersion, str]]
    blocked: list[BlockedEntry]

    @property
    def ready(self) -> bool:
        return not self.blocked


@dataclass(frozen=True)
class PublishedView:
    """An immutable published version that queries can run to completion."""

    epoch: int
    online_segments: frozenset[str]
    new_index_name: str
    segment_refs: dict[str, "ShardIndex"]
    active_sources: dict[DocumentVersion, frozenset[Hashable]]
    entries_by_segment: dict[str, frozenset[DocumentVersion]]
    routing: dict[DocumentVersion, tuple[str, ...]]
    issuances: dict[Hashable, GroupIssuance]
    ledger: tuple[TokenConfirmation, ...]

    def is_valid(self, document_version: DocumentVersion) -> bool:
        return bool(self.active_sources.get(document_version))

    def locations(self, document_version: DocumentVersion) -> tuple[str, ...]:
        return self.routing.get(document_version, ())


class _TokenVault:
    """Stores raw token shards and enforces per-preparation access permits."""

    def __init__(self) -> None:
        self._tokens: dict[DocumentVersion, frozenset] = {}

    def put(self, document_version: DocumentVersion, tokens: Iterable[Hashable]) -> None:
        self._tokens[document_version] = frozenset(tokens)

    def get(
        self, document_version: DocumentVersion, permit: Collection[DocumentVersion]
    ) -> frozenset:
        if document_version not in permit:
            raise TokenAccessDeniedError(
                f"Raw token shards of {document_version.doc_id} v"
                f"{document_version.version} are not covered by the input permit"
            )
        return self._tokens[document_version]


class ShardIndex:
    """One queryable index shard belonging to one sketch generation."""

    def __init__(
        self,
        name: str,
        generation: str,
        threshold: float,
        num_perm: int,
    ) -> None:
        if generation not in (OLD_GENERATION, NEW_GENERATION):
            raise ValueError(f"Unknown index generation {generation!r}")
        self.name = name
        self.generation = generation
        self.threshold = threshold
        self.num_perm = num_perm
        self._lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self._entries: set[DocumentVersion] = set()

    def insert(self, document_version: DocumentVersion, minhash: MinHash) -> None:
        if document_version in self._entries:
            raise ValueError(f"{document_version} is already indexed in {self.name}")
        self._lsh.insert(document_version.key, minhash)
        self._entries.add(document_version)

    def query(self, minhash: MinHash) -> list[DocumentVersion]:
        return [DocumentVersion(*key) for key in self._lsh.query(minhash)]

    def __contains__(self, document_version: object) -> bool:
        return document_version in self._entries

    @property
    def entries(self) -> frozenset[DocumentVersion]:
        return frozenset(self._entries)


def exact_jaccard(left: Iterable[Hashable], right: Iterable[Hashable]) -> float:
    left_set, right_set = frozenset(left), frozenset(right)
    union = left_set | right_set
    if not union:
        return 0.0
    return len(left_set & right_set) / len(union)


class _UnionFind:
    def __init__(self, nodes: Iterable[Hashable]) -> None:
        self._parent = {node: node for node in nodes}

    def union(self, left: Hashable, right: Hashable) -> None:
        self._parent[self.find(left)] = self.find(right)

    def find(self, node: Hashable) -> Hashable:
        parent = self._parent[node]
        if parent != node:
            self._parent[node] = self.find(parent)
        return self._parent[node]

    def components(self) -> list[frozenset[Hashable]]:
        groups: dict[Hashable, list[Hashable]] = {}
        for node in self._parent:
            groups.setdefault(self.find(node), []).append(node)
        return [frozenset(members) for members in groups.values()]


@dataclass
class _Edge:
    left: DocumentVersion
    right: DocumentVersion
    recalled: bool
    confirmation: TokenConfirmation


class SegmentRolloverManager:
    """Owns index generations, source state and the prepare/publish lifecycle."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self._lock = threading.RLock()
        self._segments: dict[str, ShardIndex] = {}
        self._retired: set[str] = set()
        self._new_index_name: Optional[str] = None
        self._vault = _TokenVault()
        self._sketches: dict[DocumentVersion, dict[int, MinHash]] = {}
        self._active_sources: dict[DocumentVersion, set[Hashable]] = {}
        self._issuances: dict[Hashable, GroupIssuance] = {}
        self._ledger: list[TokenConfirmation] = []
        self._epoch = 0

    # ------------------------------------------------------------------ setup

    def add_segment(self, segment: ShardIndex) -> None:
        with self._lock:
            if segment.name in self._segments:
                raise ValueError(f"Segment {segment.name!r} already registered")
            self._segments[segment.name] = segment
            if segment.generation == NEW_GENERATION:
                if self._new_index_name is not None:
                    raise ValueError("Only one new-generation index is supported")
                self._new_index_name = segment.name

    def register_document(
        self,
        document_version: DocumentVersion,
        tokens: Iterable[Hashable],
        source_ids: Iterable[Hashable],
    ) -> None:
        """Register a document version's raw shards and supporting sources."""
        with self._lock:
            self._vault.put(document_version, tokens)
            sources = self._active_sources.setdefault(document_version, set())
            sources.update(source_ids)

    def index_insert(
        self, segment_name: str, document_version: DocumentVersion, tokens: Iterable[Hashable]
    ) -> None:
        """Insert a queryable index entry into an old segment or the new index."""
        with self._lock:
            segment = self._online_segment(segment_name)
            tokens = frozenset(tokens)
            self._vault.put(document_version, tokens)
            minhash = self._build_sketch(document_version, tokens, segment.num_perm)
            segment.insert(document_version, minhash)

    def register_historical_group(
        self,
        group_id: Hashable,
        serving_segments: Iterable[str],
        members: Iterable[DocumentVersion],
        confirmations: Iterable[TokenConfirmation],
    ) -> GroupIssuance:
        with self._lock:
            serving = frozenset(serving_segments)
            unknown = serving - set(self._segments)
            if unknown:
                raise ValueError(f"Unknown serving segments: {sorted(unknown)}")
            issuance = GroupIssuance(
                group_id=group_id,
                serving_segments=serving,
                members=frozenset(members),
                confirmations=tuple(confirmations),
                issued_epoch=self._epoch,
            )
            self._issuances[group_id] = issuance
            for confirmation in confirmations:
                if confirmation not in self._ledger:
                    self._ledger.append(confirmation)
            return issuance

    # --------------------------------------------------------- document intake

    def apply_correction(
        self,
        document_version: DocumentVersion,
        tokens: Iterable[Hashable],
        source_ids: Iterable[Hashable],
    ) -> None:
        """Intake a corrected document version; old versions retire via revocation."""
        with self._lock:
            self.register_document(document_version, tokens, source_ids)

    def add_sources(
        self, document_version: DocumentVersion, source_ids: Iterable[Hashable]
    ) -> None:
        with self._lock:
            self._active_sources.setdefault(document_version, set()).update(source_ids)

    def revoke_support(self, document_version: DocumentVersion, source_id: Hashable) -> None:
        """Revoke one independent source; the version survives on other sources."""
        with self._lock:
            sources = self._active_sources.get(document_version)
            if not sources or source_id not in sources:
                raise ValueError(
                    f"Source {source_id!r} does not support {document_version}"
                )
            sources.discard(source_id)

    def is_valid(self, document_version: DocumentVersion) -> bool:
        with self._lock:
            return bool(self._active_sources.get(document_version))

    def confirm_tokens(
        self,
        left: DocumentVersion,
        right: DocumentVersion,
        source_id: Hashable,
        permit: Collection[DocumentVersion],
    ) -> TokenConfirmation:
        """Create a confirmation record, re-checking raw shards under the permit."""
        with self._lock:
            left_tokens = self._vault.get(left, permit)
            right_tokens = self._vault.get(right, permit)
            meets = exact_jaccard(left_tokens, right_tokens) >= self.threshold
            confirmation = TokenConfirmation(
                left=left,
                right=right,
                source_id=source_id,
                left_tokens=left_tokens,
                right_tokens=right_tokens,
                meets_threshold=meets,
                issued_epoch=self._epoch,
            )
            self._ledger.append(confirmation)
            return confirmation

    def review_for_new_index(
        self,
        document_version: DocumentVersion,
        permit: Collection[DocumentVersion],
    ) -> None:
        """Promote a version into the new index after an in-permit token review."""
        with self._lock:
            if self._new_index_name is None:
                raise ValueError("No new-generation index registered")
            tokens = self._vault.get(document_version, permit)
            self.index_insert(self._new_index_name, document_version, tokens)

    # ------------------------------------------------------------- prepare / publish

    def prepare_retirement(
        self,
        segment_name: str,
        permit: Collection[DocumentVersion],
    ) -> RetirementPlan:
        # Preparation itself only reads index entries, sources and already
        # attested confirmations; raw token shards are exclusively accessed
        # through permit-gated methods (confirm_tokens, review_for_new_index).
        with self._lock:
            self._online_segment(segment_name)
            permit = frozenset(permit)
            placements, blocked = self._evaluate(segment_name)
            return RetirementPlan(
                segment=segment_name,
                prepared_epoch=self._epoch,
                permit=permit,
                placements=placements,
                blocked=blocked,
            )

    def publish(self, plan: RetirementPlan) -> PublishedView:
        # Publishing never touches the token vault: no permit may be extended here.
        with self._lock:
            self._online_segment(plan.segment)
            _, blocked = self._evaluate(plan.segment)
            if blocked:
                raise RetirementBlockedError(plan.segment, blocked)
            self._retired.add(plan.segment)
            self._epoch += 1
            return self.snapshot()

    def _evaluate(
        self, segment_name: str
    ) -> tuple[dict[Hashable, dict[DocumentVersion, str]], list[BlockedEntry]]:
        placements: dict[Hashable, dict[DocumentVersion, str]] = {}
        blocked: list[BlockedEntry] = []
        new_index = self._segments[self._new_index_name] if self._new_index_name else None
        other_old = [
            segment
            for name, segment in self._segments.items()
            if name != segment_name
            and name not in self._retired
            and segment.generation == OLD_GENERATION
        ]
        for group_id in self._groups_served_by(segment_name):
            issuance = self._issuances[group_id]
            group_placements: dict[DocumentVersion, str] = {}
            for document_version in self._live_group_versions(issuance):
                if new_index is not None and document_version in new_index:
                    group_placements[document_version] = new_index.name
                else:
                    successor = next(
                        (segment.name for segment in other_old if document_version in segment),
                        None,
                    )
                    if successor is None:
                        blocked.append(
                            BlockedEntry(
                                group_id=group_id,
                                document_version=document_version,
                                reason="no queryable entry in new index or surviving old segment",
                            )
                        )
                    else:
                        group_placements[document_version] = successor
            placements[group_id] = group_placements
        return placements, blocked

    # ------------------------------------------------------------------ queries

    def snapshot(self) -> PublishedView:
        with self._lock:
            online = frozenset(
                name for name in self._segments if name not in self._retired
            )
            entries_by_segment = {
                name: frozenset(self._segments[name].entries) for name in sorted(online)
            }
            routing: dict[DocumentVersion, list[str]] = {}
            for name in sorted(online):
                for document_version in entries_by_segment[name]:
                    routing.setdefault(document_version, []).append(name)
            return PublishedView(
                epoch=self._epoch,
                online_segments=online,
                new_index_name=self._new_index_name or "",
                segment_refs={name: self._segments[name] for name in online},
                active_sources={
                    document_version: frozenset(sources)
                    for document_version, sources in self._active_sources.items()
                },
                entries_by_segment=entries_by_segment,
                routing={
                    document_version: tuple(locations)
                    for document_version, locations in routing.items()
                },
                issuances=dict(self._issuances),
                ledger=tuple(self._ledger),
            )

    def historical_group(
        self, view: PublishedView, group_id: Hashable
    ) -> GroupIssuance:
        return view.issuances[group_id]

    def current_groups(
        self, view: PublishedView
    ) -> tuple[list[frozenset[DocumentVersion]], list[_Edge]]:
        """Return connected components and the relations backing them.

        Only document versions that are still valid *and* reachable through a
        routing entry in this view participate.  Candidate pairs come from LSH
        recall (each generation queried with its own sketch parameters) and from
        historical confirmations; a pair is only accepted when a currently-valid
        confirmation meets the threshold.
        """
        nodes = [
            document_version
            for document_version in view.routing
            if view.is_valid(document_version)
        ]
        recalled_pairs = self._recall_pairs(view, nodes)
        edges: list[_Edge] = []
        seen_pairs: set[frozenset[DocumentVersion]] = set()
        for confirmation in view.ledger:
            if not self._confirmation_is_valid(view, confirmation):
                continue
            if not confirmation.meets_threshold:
                continue
            pair = frozenset((confirmation.left, confirmation.right))
            if pair in seen_pairs:
                continue
            if confirmation.left not in view.routing or confirmation.right not in view.routing:
                continue
            seen_pairs.add(pair)
            edges.append(
                _Edge(
                    left=confirmation.left,
                    right=confirmation.right,
                    recalled=pair in recalled_pairs,
                    confirmation=confirmation,
                )
            )
        union_find = _UnionFind(nodes)
        for edge in edges:
            union_find.union(edge.left, edge.right)
        components = sorted(union_find.components(), key=lambda group: tuple(sorted(map(str, group))))
        return components, edges

    def recall_candidates(
        self, view: PublishedView, document_version: DocumentVersion
    ) -> dict[str, list[DocumentVersion]]:
        """Query one document version against each generation separately."""
        result: dict[str, list[DocumentVersion]] = {}
        for segment_name in sorted(view.online_segments):
            segment = view.segment_refs[segment_name]
            sketch = self._sketches.get(document_version, {}).get(segment.num_perm)
            if sketch is None:
                result[segment_name] = []
                continue
            visible = view.entries_by_segment[segment_name]
            result[segment_name] = [
                candidate
                for candidate in segment.query(sketch)
                if candidate in visible and candidate != document_version
            ]
        return result

    # ------------------------------------------------------------------ helpers

    def _online_segment(self, segment_name: str) -> ShardIndex:
        if segment_name not in self._segments:
            raise ValueError(f"Unknown segment {segment_name!r}")
        if segment_name in self._retired:
            raise ValueError(f"Segment {segment_name!r} is already retired")
        return self._segments[segment_name]

    def _groups_served_by(self, segment_name: str) -> list[Hashable]:
        return sorted(
            group_id
            for group_id, issuance in self._issuances.items()
            if segment_name in issuance.serving_segments
        )

    def _live_group_versions(
        self, issuance: GroupIssuance
    ) -> list[DocumentVersion]:
        doc_ids = {member.doc_id for member in issuance.members}
        return sorted(
            (
                document_version
                for document_version, sources in self._active_sources.items()
                if document_version.doc_id in doc_ids and sources
            ),
            key=lambda document_version: (str(document_version.doc_id), str(document_version.version)),
        )

    def _build_sketch(
        self,
        document_version: DocumentVersion,
        tokens: Collection[Hashable],
        num_perm: int,
    ) -> MinHash:
        cached = self._sketches.setdefault(document_version, {})
        if num_perm not in cached:
            minhash = MinHash(num_perm=num_perm)
            for token in tokens:
                minhash.update(str(token).encode("utf-8"))
            cached[num_perm] = minhash
        return cached[num_perm]

    def _recall_pairs(
        self,
        view: PublishedView,
        nodes: Collection[DocumentVersion],
    ) -> set[frozenset[DocumentVersion]]:
        pairs: set[frozenset[DocumentVersion]] = set()
        for document_version in nodes:
            for candidates in self.recall_candidates(view, document_version).values():
                for candidate in candidates:
                    if candidate in view.routing and view.is_valid(candidate):
                        pairs.add(frozenset((document_version, candidate)))
        return pairs

    @staticmethod
    def _confirmation_is_valid(
        view: PublishedView, confirmation: TokenConfirmation
    ) -> bool:
        left_sources = view.active_sources.get(confirmation.left)
        right_sources = view.active_sources.get(confirmation.right)
        if not left_sources or not right_sources:
            return False
        return (
            confirmation.source_id in left_sources
            and confirmation.source_id in right_sources
        )
