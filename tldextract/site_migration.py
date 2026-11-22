"""Migrate archived access-log site keys to canonical IDNA site keys.

Legacy archives keyed sites with IDNA 2003 rules. Under those rules the
Unicode host ``straße.de`` and the ASCII host ``strasse.de`` collapsed onto the
same key, because IDNA 2003 maps ``ß`` to ``ss``. A canonical, IDNA 2008 based
key keeps them distinct while still treating the Unicode form and its ACE
(A-label) form as the same host::

    >>> canonical_host("straße.de")
    'xn--strae-oqa.de'
    >>> canonical_host("xn--strae-oqa.de")
    'xn--strae-oqa.de'
    >>> legacy_host("straße.de")
    'strasse.de'

The migration works from two kinds of evidence:

* :class:`DetailRecord` rows retained the original ``Host``. Their old and new
  site keys can be recomputed exactly.
* :class:`UncertainRecord` rows lost the original ``Host`` and only carry the
  set of new sites they may belong to.

Independently saved new-site totals (:meth:`MigrationLedger.add_total`) act as
counting constraints. A row is only migrated when *every* allocation that
satisfies those totals puts it on the same new site. Ambiguity and conflicting
totals are reported, never resolved by input order.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Literal

from .site_key import canonical_host, legacy_host
from .tldextract import TLDExtract

__all__ = [
    "Conflict",
    "DetailRecord",
    "MigrationLedger",
    "MigrationReport",
    "RowOutcome",
    "SiteKey",
    "UncertainRecord",
    "canonical_host",
    "default_extractor",
    "derive_site_key",
    "legacy_host",
    "reconcile",
]

RowId = tuple[str, str]
Status = Literal[
    "confirmed",
    "ambiguous",
    "suffix_only",
    "not_a_site",
    "invalid",
    "conflicted",
]

_DEFAULT_EXTRACTOR: TLDExtract | None = None


def default_extractor() -> TLDExtract:
    """Return the offline extractor backed by the bundled suffix snapshot."""
    global _DEFAULT_EXTRACTOR
    if _DEFAULT_EXTRACTOR is None:
        _DEFAULT_EXTRACTOR = TLDExtract(cache_dir=None, suffix_list_urls=())
    return _DEFAULT_EXTRACTOR


@dataclass(frozen=True)
class SiteKey:
    """Old and new site keys derived from one retained original host.

    Attributes:
        original_host: The retained host, exactly as archived.
        canonical_host: The host in canonical IDNA 2008 A-label form.
        legacy_host: The host as it was keyed by legacy IDNA 2003 rules.
        old_key: Legacy registrable site key, or the empty string if none.
        new_key: Canonical registrable site key, or the empty string if none.
        old_suffix: Legacy public suffix, if any.
        new_suffix: Canonical public suffix, if any.
        is_private: Whether the canonical suffix is a PSL private suffix.
        is_suffix_only: Whether the whole host is only a public suffix.
        is_valid: Whether the host could be canonicalized as IDNA 2008.
    """

    original_host: str
    canonical_host: str
    legacy_host: str
    old_key: str
    new_key: str
    old_suffix: str
    new_suffix: str
    is_private: bool
    is_suffix_only: bool
    is_valid: bool

    @property
    def current_key(self) -> str:
        """Alias for :attr:`new_key`, the key used going forward."""
        return self.new_key

    @property
    def evidence(self) -> str:
        """Alias for :attr:`original_host`, the retained evidence."""
        return self.original_host


def derive_site_key(
    host: str,
    extractor: TLDExtract | None = None,
    *,
    include_psl_private_domains: bool = True,
) -> SiteKey:
    """Derive the old and new site keys for a retained original host.

    The suffix list is applied to both the legacy and canonical host forms, so
    the two keys differ only by the IDNA convention, not by suffix data. A host
    that is itself only a public suffix (for example a Unicode private suffix)
    has an empty :attr:`SiteKey.new_key` and ``is_suffix_only`` set.

    Args:
        host: The retained original host.
        extractor: Suffix-list extractor to use. Defaults to the bundled
            offline snapshot.
        include_psl_private_domains: Whether private suffixes are suffixes.
    """
    extractor = extractor or default_extractor()
    legacy = legacy_host(host)
    try:
        canonical = canonical_host(host)
    except (UnicodeError, ValueError):
        return SiteKey(
            original_host=host,
            canonical_host="",
            legacy_host=legacy,
            old_key="",
            new_key="",
            old_suffix="",
            new_suffix="",
            is_private=False,
            is_suffix_only=False,
            is_valid=False,
        )

    if not canonical:
        return SiteKey(host, "", legacy, "", "", "", "", False, False, False)

    canonical_result = extractor(
        canonical, include_psl_private_domains=include_psl_private_domains
    )
    legacy_result = (
        extractor(legacy, include_psl_private_domains=include_psl_private_domains)
        if legacy
        else canonical_result
    )
    is_suffix_only = (
        bool(canonical_result.suffix)
        and not canonical_result.domain
        and not canonical_result.subdomain
    )
    return SiteKey(
        original_host=host,
        canonical_host=canonical,
        legacy_host=legacy,
        old_key="" if is_suffix_only else legacy_result.top_domain_under_public_suffix,
        new_key=canonical_result.top_domain_under_public_suffix,
        old_suffix=legacy_result.suffix,
        new_suffix=canonical_result.suffix,
        is_private=canonical_result.is_private,
        is_suffix_only=is_suffix_only,
        is_valid=True,
    )


@dataclass(frozen=True)
class DetailRecord:
    """A retained detail row identified by its source and original event id."""

    source: str
    event_id: str
    original_host: str

    @property
    def row_id(self) -> RowId:
        """Return the identity shared by replays of this row."""
        return (self.source, self.event_id)


@dataclass(frozen=True)
class UncertainRecord:
    """A row missing its original host, with its possible new sites."""

    source: str
    event_id: str
    candidate_sites: frozenset[str]
    old_key: str = ""

    @property
    def row_id(self) -> RowId:
        """Return the identity shared by replays of this row."""
        return (self.source, self.event_id)


@dataclass
class MigrationLedger:
    """Order-independent collection of migration material.

    Rows are identified by ``(source, event_id)``. Re-adding the same identity
    with the same payload is a replay and is ignored; re-adding it with a
    different payload is an identity conflict and excludes the row rather than
    preferring whichever input arrived first.
    """

    replayed: int = 0
    identity_conflicts: list[tuple[RowId, str, str]] = field(default_factory=list)
    total_conflicts: list[tuple[str, int, int]] = field(default_factory=list)
    _details: dict[RowId, DetailRecord] = field(default_factory=dict, repr=False)
    _uncertain: dict[RowId, UncertainRecord] = field(default_factory=dict, repr=False)
    _totals: dict[str, int] = field(default_factory=dict, repr=False)
    _conflicted: set[RowId] = field(default_factory=set, repr=False)
    _first_payloads: dict[RowId, str] = field(default_factory=dict, repr=False)

    def add_detail(self, source: str, event_id: str, original_host: str) -> bool:
        """Add one retained detail row. Returns ``False`` for a replay."""
        return self.add_row(DetailRecord(source, event_id, original_host))

    def add_uncertain(
        self,
        source: str,
        event_id: str,
        candidate_sites: Iterable[str],
        old_key: str = "",
    ) -> bool:
        """Add one host-less row with its possible new sites."""
        return self.add_row(
            UncertainRecord(source, event_id, frozenset(candidate_sites), old_key)
        )

    def add_row(self, record: DetailRecord | UncertainRecord) -> bool:
        """Add a row of either kind. Returns ``False`` for a replay."""
        row_id = record.row_id
        existing: DetailRecord | UncertainRecord | None = self._details.get(row_id)
        if existing is None:
            existing = self._uncertain.get(row_id)

        if existing is not None:
            if _payload(existing) == _payload(record):
                self.replayed += 1
                return False
            self.identity_conflicts.append(
                (row_id, _payload(existing), _payload(record))
            )
            self._first_payloads[row_id] = _payload(existing)
            self._details.pop(row_id, None)
            self._uncertain.pop(row_id, None)
            self._conflicted.add(row_id)
            return False

        if row_id in self._conflicted:
            self.identity_conflicts.append(
                (row_id, self._first_payloads.get(row_id, ""), _payload(record))
            )
            return False

        if isinstance(record, DetailRecord):
            self._details[row_id] = record
        else:
            self._uncertain[row_id] = replace(
                record, candidate_sites=frozenset(record.candidate_sites)
            )
        return True

    def replay(self, records: Iterable[DetailRecord | UncertainRecord]) -> None:
        """Replay a batch of rows; duplicate identities are ignored."""
        for record in records:
            self.add_row(record)

    def add_total(self, site: str, count: int) -> bool:
        """Record an independently saved new-site total.

        Repeating the same total is idempotent. A different total for the same
        site is recorded as a conflict and the first value is retained.
        """
        if count < 0:
            raise ValueError("A site total cannot be negative")
        existing = self._totals.get(site)
        if existing is None:
            self._totals[site] = count
            return True
        if existing != count:
            self.total_conflicts.append((site, existing, count))
        return False

    @property
    def detail_records(self) -> tuple[DetailRecord, ...]:
        """Return the active detail rows in identity order."""
        return tuple(self._details[key] for key in sorted(self._details))

    @property
    def uncertain_records(self) -> tuple[UncertainRecord, ...]:
        """Return the active host-less rows in identity order."""
        return tuple(self._uncertain[key] for key in sorted(self._uncertain))

    @property
    def saved_totals(self) -> dict[str, int]:
        """Return the independently saved new-site totals."""
        return dict(self._totals)

    @property
    def conflicted_row_ids(self) -> frozenset[RowId]:
        """Return identities excluded by conflicting duplicate payloads."""
        return frozenset(self._conflicted)


def _payload(record: DetailRecord | UncertainRecord) -> str:
    """Render a row payload for replay comparison."""
    if isinstance(record, DetailRecord):
        return f"detail:{record.original_host}"
    candidates = ",".join(sorted(record.candidate_sites))
    return f"uncertain:{record.old_key}:{candidates}"


@dataclass(frozen=True)
class RowOutcome:
    """The migration outcome for one row.

    The old key, current (new) key, and retained original evidence are all
    available so a report can be reconciled under either convention.
    """

    source: str
    event_id: str
    original_host: str
    old_key: str
    new_key: str
    status: Status
    possible_sites: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    migrated: bool = False

    @property
    def row_id(self) -> RowId:
        """Return the ``(source, event_id)`` identity."""
        return (self.source, self.event_id)

    @property
    def current_key(self) -> str:
        """Alias for the new key."""
        return self.new_key

    @property
    def evidence(self) -> str:
        """Alias for the retained original host."""
        return self.original_host


@dataclass(frozen=True)
class Conflict:
    """A set of rows and totals that cannot be reconciled."""

    kind: str
    message: str
    sites: tuple[str, ...]
    saved_totals: dict[str, int]
    residual: dict[str, int]
    row_ids: tuple[RowId, ...]


@dataclass
class MigrationReport:
    """Result of reconciling a migration ledger."""

    rows: list[RowOutcome]
    conflicts: list[Conflict]
    saved_totals: dict[str, int]
    confirmed_counts: dict[str, int]
    required_from_uncertain: dict[str, int]
    final_totals: dict[str, int]
    total_bounds: dict[str, tuple[int, int]]
    old_key_counts: dict[str, int]
    new_key_counts: dict[str, int]
    old_to_new: dict[str, dict[str, int]]
    replayed: int = 0
    identity_conflicts: list[tuple[RowId, str, str]] = field(default_factory=list)
    total_conflicts: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def reconciled(self) -> bool:
        """Whether every saved total was met and no identity conflict exists."""
        return (
            not self.conflicts
            and not self.identity_conflicts
            and not self.total_conflicts
        )

    @property
    def confirmed(self) -> list[RowOutcome]:
        """Rows that may be migrated."""
        return [row for row in self.rows if row.status == "confirmed"]

    @property
    def ambiguous(self) -> list[RowOutcome]:
        """Rows with more than one possible new site."""
        return [row for row in self.rows if row.status == "ambiguous"]

    @property
    def suffix_only(self) -> list[RowOutcome]:
        """Rows whose original host was only a suffix, excluded from counts."""
        return [row for row in self.rows if row.status == "suffix_only"]

    @property
    def invalid(self) -> list[RowOutcome]:
        """Rows that are neither sites nor suffix-only hosts."""
        return [
            row
            for row in self.rows
            if row.status in {"invalid", "not_a_site", "conflicted"}
        ]

    @property
    def detail_row_count(self) -> int:
        """Number of detail rows represented in the report."""
        return sum(self.old_key_counts.values())


def reconcile(
    ledger: MigrationLedger,
    extractor: TLDExtract | None = None,
    *,
    include_psl_private_domains: bool = True,
) -> MigrationReport:
    """Reconcile a ledger and decide which rows may migrate.

    Confirmed detail rows are counted first. Each saved new-site total then
    leaves a residual count that the host-less rows must supply. Feasibility and
    per-row possibility are decided with flow, so thousands of overlapping
    candidate rows are never enumerated allocation by allocation.
    """
    extractor = extractor or default_extractor()

    detail_state = _DetailState()
    outcomes: list[RowOutcome] = []
    for record in ledger.detail_records:
        outcomes.append(
            detail_state.add(record, extractor, include_psl_private_domains)
        )

    for row_id in sorted(ledger.conflicted_row_ids):
        outcomes.append(
            RowOutcome(
                source=row_id[0],
                event_id=row_id[1],
                original_host="",
                old_key="",
                new_key="",
                status="conflicted",
            )
        )

    groups: dict[frozenset[str], list[UncertainRecord]] = {}
    empty_candidate_rows: list[UncertainRecord] = []
    for uncertain in ledger.uncertain_records:
        candidates = frozenset(site for site in uncertain.candidate_sites if site)
        if not candidates:
            empty_candidate_rows.append(uncertain)
            continue
        groups.setdefault(candidates, []).append(uncertain)

    saved_totals = ledger.saved_totals
    conflicted_totals = {site for site, _, _ in ledger.total_conflicts}
    usable_totals = {
        site: total
        for site, total in saved_totals.items()
        if site not in conflicted_totals
    }
    required, conflicts = _residual_totals(
        usable_totals, detail_state.confirmed_counts, detail_state.detail_by_new_key
    )
    for site in sorted(conflicted_totals):
        conflicts.append(
            Conflict(
                kind="conflicting_totals",
                message=(f"Saved totals disagree for {site!r}; no total is adopted"),
                sites=(site,),
                saved_totals=dict(saved_totals),
                residual={},
                row_ids=(),
            )
        )

    if empty_candidate_rows:
        conflicts.append(
            Conflict(
                kind="empty_candidates",
                message="Rows without an original host have no possible new site",
                sites=(),
                saved_totals={},
                residual={},
                row_ids=tuple(record.row_id for record in empty_candidate_rows),
            )
        )

    allocation = _resolve_groups(groups, required, saved_totals)
    conflicts.extend(allocation.conflicts)
    outcomes.extend(_group_outcomes(groups, allocation))
    outcomes.extend(
        RowOutcome(
            source=record.source,
            event_id=record.event_id,
            original_host="",
            old_key=record.old_key,
            new_key="",
            status="conflicted",
        )
        for record in empty_candidate_rows
    )

    final_totals, total_bounds = _final_totals(
        groups,
        usable_totals,
        required,
        detail_state.confirmed_counts,
        conflicted_groups=allocation.conflicted_groups,
    )
    outcomes.sort(key=lambda row: (row.source, row.event_id))
    return MigrationReport(
        rows=outcomes,
        conflicts=conflicts,
        saved_totals=dict(saved_totals),
        confirmed_counts=dict(detail_state.confirmed_counts),
        required_from_uncertain=dict(required),
        final_totals=final_totals,
        total_bounds=total_bounds,
        old_key_counts=detail_state.old_key_counts,
        new_key_counts=detail_state.new_key_counts,
        old_to_new={key: dict(value) for key, value in detail_state.old_to_new.items()},
        replayed=ledger.replayed,
        identity_conflicts=list(ledger.identity_conflicts),
        total_conflicts=list(ledger.total_conflicts),
    )


@dataclass
class _DetailState:
    """Accumulated results from the retained detail rows."""

    confirmed_counts: dict[str, int] = field(default_factory=dict)
    old_key_counts: dict[str, int] = field(default_factory=dict)
    new_key_counts: dict[str, int] = field(default_factory=dict)
    old_to_new: dict[str, dict[str, int]] = field(default_factory=dict)
    detail_by_new_key: dict[str, list[RowId]] = field(default_factory=dict)

    def add(
        self,
        record: DetailRecord,
        extractor: TLDExtract,
        include_psl_private_domains: bool,
    ) -> RowOutcome:
        """Classify one retained detail row and tally it."""
        site_key = derive_site_key(
            record.original_host,
            extractor,
            include_psl_private_domains=include_psl_private_domains,
        )
        self.old_key_counts[site_key.old_key] = (
            self.old_key_counts.get(site_key.old_key, 0) + 1
        )
        self.new_key_counts[site_key.new_key] = (
            self.new_key_counts.get(site_key.new_key, 0) + 1
        )
        status = _detail_status(site_key)
        if status == "confirmed":
            new_key = site_key.new_key
            self.confirmed_counts[new_key] = self.confirmed_counts.get(new_key, 0) + 1
            by_new = self.old_to_new.setdefault(site_key.old_key, {})
            by_new[new_key] = by_new.get(new_key, 0) + 1
            self.detail_by_new_key.setdefault(new_key, []).append(record.row_id)

        return RowOutcome(
            source=record.source,
            event_id=record.event_id,
            original_host=record.original_host,
            old_key=site_key.old_key,
            new_key=site_key.new_key,
            status=status,
            possible_sites=(site_key.new_key,) if status == "confirmed" else (),
            migrated=status == "confirmed",
        )


def _detail_status(site_key: SiteKey) -> Status:
    """Classify a detail row's migration status."""
    if not site_key.is_valid:
        return "invalid"
    if site_key.is_suffix_only:
        return "suffix_only"
    if not site_key.new_key:
        return "not_a_site"
    return "confirmed"


def _residual_totals(
    saved_totals: dict[str, int],
    confirmed_counts: dict[str, int],
    detail_by_new_key: dict[str, list[RowId]],
) -> tuple[dict[str, int], list[Conflict]]:
    """Compute what each saved total still needs from host-less rows."""
    required: dict[str, int] = {}
    conflicts: list[Conflict] = []
    for site, total in saved_totals.items():
        residual = total - confirmed_counts.get(site, 0)
        required[site] = residual
        if residual < 0:
            conflicts.append(
                Conflict(
                    kind="confirmed_exceeds_total",
                    message=(
                        f"Confirmed detail rows for {site!r}"
                        f" ({confirmed_counts[site]}) already exceed the saved"
                        f" total ({total})"
                    ),
                    sites=(site,),
                    saved_totals={site: total},
                    residual={site: residual},
                    row_ids=tuple(detail_by_new_key.get(site, ())),
                )
            )
    return required, conflicts


@dataclass
class _GroupResolution:
    """Per candidate-group allocation results."""

    possible_by_group: dict[frozenset[str], tuple[str, ...]]
    conflicted_groups: set[frozenset[str]]
    conflicts: list[Conflict]


def _group_outcomes(
    groups: dict[frozenset[str], list[UncertainRecord]],
    allocation: _GroupResolution,
) -> list[RowOutcome]:
    """Build row outcomes for every host-less row with candidates."""
    outcomes: list[RowOutcome] = []
    for candidates, records in groups.items():
        conflicted = candidates in allocation.conflicted_groups
        possible = allocation.possible_by_group.get(
            candidates, tuple(sorted(candidates))
        )
        migrated = not conflicted and len(possible) == 1
        for record in records:
            if conflicted:
                status: Status = "conflicted"
            elif migrated:
                status = "confirmed"
            else:
                status = "ambiguous"
            outcomes.append(
                RowOutcome(
                    source=record.source,
                    event_id=record.event_id,
                    original_host="",
                    old_key=record.old_key,
                    new_key=possible[0] if migrated else "",
                    status=status,
                    possible_sites=() if conflicted else possible,
                    candidates=tuple(sorted(candidates)),
                    migrated=migrated,
                )
            )
    return outcomes


def _resolve_groups(
    groups: dict[frozenset[str], list[UncertainRecord]],
    required: dict[str, int],
    saved_totals: dict[str, int],
) -> _GroupResolution:
    """Decide feasibility and per-row possibility for each candidate group."""
    result = _GroupResolution({}, set(), [])
    for group_keys, site_keys in _components(groups, set(required)):
        _resolve_component(
            group_keys, site_keys, groups, required, saved_totals, result
        )
    return result


def _resolve_component(
    group_keys: list[frozenset[str]],
    site_keys: set[str],
    groups: dict[frozenset[str], list[UncertainRecord]],
    required: dict[str, int],
    saved_totals: dict[str, int],
    result: _GroupResolution,
) -> None:
    """Resolve one independent group/site component."""
    component_rows = [record for key in group_keys for record in groups[key]]
    constrained = {site: required[site] for site in site_keys if site in required}
    involved_sites = tuple(sorted(site_keys))
    involved_totals = {
        site: saved_totals[site] for site in involved_sites if site in saved_totals
    }
    group_counts = {key: len(groups[key]) for key in group_keys}
    total_rows = sum(group_counts.values())

    if any(count < 0 for count in constrained.values()):
        result.conflicted_groups.update(group_keys)
        return

    if total_rows == 0:
        if any(count for count in constrained.values()):
            result.conflicts.append(
                Conflict(
                    kind="unsatisfiable_totals",
                    message="Saved totals require rows that are not present",
                    sites=involved_sites,
                    saved_totals=involved_totals,
                    residual=dict(constrained),
                    row_ids=(),
                )
            )
        return

    if not _feasible(group_counts, constrained, total_rows):
        result.conflicted_groups.update(group_keys)
        result.conflicts.append(
            Conflict(
                kind="unsatisfiable_totals",
                message=(
                    "No allocation of the candidate rows satisfies the saved totals"
                ),
                sites=involved_sites,
                saved_totals=involved_totals,
                residual=dict(constrained),
                row_ids=tuple(record.row_id for record in component_rows),
            )
        )
        return

    for key in group_keys:
        if len(key) == 1:
            result.possible_by_group[key] = tuple(key)
            continue
        possible = tuple(
            sorted(
                site
                for site in key
                if _feasible(group_counts, constrained, total_rows, (key, site))
            )
        )
        result.possible_by_group[key] = possible or tuple(sorted(key))


def _final_totals(
    groups: dict[frozenset[str], list[UncertainRecord]],
    saved_totals: dict[str, int],
    required: dict[str, int],
    confirmed_counts: dict[str, int],
    *,
    conflicted_groups: set[frozenset[str]],
) -> tuple[dict[str, int], dict[str, tuple[int, int]]]:
    """Compute the new-side totals and their known bounds.

    A site whose constraints were satisfied reports the saved total. A site
    caught in a conflict reports only the confirmed count, with the uncertain
    rows left as the upper bound, rather than silently adopting a total no
    allocation could meet.
    """
    candidate_multiplicity: dict[str, int] = {}
    for candidates, records in groups.items():
        if candidates in conflicted_groups:
            continue
        for site in candidates:
            candidate_multiplicity[site] = candidate_multiplicity.get(site, 0) + len(
                records
            )

    total_bounds: dict[str, tuple[int, int]] = {}
    final_totals: dict[str, int] = {}
    all_sites = set(saved_totals) | set(confirmed_counts) | set(candidate_multiplicity)
    for site in sorted(all_sites):
        confirmed = confirmed_counts.get(site, 0)
        upper = confirmed + candidate_multiplicity.get(site, 0)
        if site in required and required[site] >= 0 and upper >= required[site]:
            final_totals[site] = saved_totals[site]
            total_bounds[site] = (saved_totals[site], saved_totals[site])
        else:
            final_totals[site] = confirmed
            total_bounds[site] = (confirmed, max(upper, confirmed))
    return final_totals, total_bounds


def _components(
    groups: dict[frozenset[str], list[UncertainRecord]],
    constrained_sites: set[str],
) -> list[tuple[list[frozenset[str]], set[str]]]:
    """Split the group/site graph into independent components."""
    parent: dict[object, object] = {}

    def find(node: object) -> object:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: object, right: object) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for candidates in groups:
        group_node = ("group", candidates)
        find(group_node)
        for site in candidates:
            union(group_node, ("site", site))
    for site in constrained_sites:
        find(("site", site))

    components: dict[object, tuple[list[frozenset[str]], set[str]]] = {}
    for candidates in groups:
        root = find(("group", candidates))
        group_list, site_set = components.setdefault(root, ([], set()))
        group_list.append(candidates)
        site_set.update(candidates)
    for site in constrained_sites:
        root = find(("site", site))
        group_list, site_set = components.setdefault(root, ([], set()))
        site_set.add(site)
    return list(components.values())


def _feasible(
    group_counts: dict[frozenset[str], int],
    constrained: dict[str, int],
    total_rows: int,
    force: tuple[frozenset[str], str] | None = None,
) -> bool:
    """Whether an assignment exists, optionally forcing one row to a site."""
    group_keys = list(group_counts)
    sites = sorted({site for key in group_keys for site in key} | set(constrained))

    source, sink = 0, 1
    group_base = 2
    site_base = group_base + len(group_keys)
    super_source = site_base + len(sites)
    super_sink = super_source + 1
    dinic = _Dinic(super_sink + 1)
    demand = [0] * (super_sink + 1)

    def add_edge(start: int, end: int, lower: int, upper: int) -> None:
        dinic.add_edge(start, end, upper - lower)
        demand[start] -= lower
        demand[end] += lower

    for offset, key in enumerate(group_keys):
        count = group_counts[key]
        node = group_base + offset
        add_edge(source, node, count, count)
        for site in key:
            lower = 1 if force == (key, site) else 0
            add_edge(node, site_base + sites.index(site), lower, count)

    for offset, site in enumerate(sites):
        node = site_base + offset
        if site in constrained:
            count = constrained[site]
            add_edge(node, sink, count, count)
        else:
            add_edge(node, sink, 0, total_rows)

    add_edge(sink, source, 0, total_rows)

    required_flow = 0
    for node, balance in enumerate(demand):
        if balance > 0:
            dinic.add_edge(super_source, node, balance)
            required_flow += balance
        elif balance < 0:
            dinic.add_edge(node, super_sink, -balance)

    return dinic.max_flow(super_source, super_sink) == required_flow


class _Edge:
    """A residual edge in the flow network."""

    __slots__ = ("to", "rev", "cap")

    def __init__(self, to: int, rev: int, cap: int) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap


class _Dinic:
    """A small integer-capacity Dinic max-flow implementation."""

    def __init__(self, size: int) -> None:
        self._graph: list[list[_Edge]] = [[] for _ in range(size)]
        self._level = [0] * size
        self._next = [0] * size

    def add_edge(self, start: int, end: int, capacity: int) -> None:
        """Add a directed edge and its residual counterpart."""
        forward = _Edge(end, len(self._graph[end]), capacity)
        backward = _Edge(start, len(self._graph[start]), 0)
        self._graph[start].append(forward)
        self._graph[end].append(backward)

    def max_flow(self, source: int, sink: int) -> int:
        """Return the maximum flow from source to sink."""
        flow = 0
        while self._build_levels(source, sink):
            self._next = [0] * len(self._graph)
            while True:
                pushed = self._send(source, sink, 1 << 60)
                if not pushed:
                    break
                flow += pushed
        return flow

    def _build_levels(self, source: int, sink: int) -> bool:
        from collections import deque

        self._level = [-1] * len(self._graph)
        self._level[source] = 0
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for edge in self._graph[node]:
                if edge.cap > 0 and self._level[edge.to] < 0:
                    self._level[edge.to] = self._level[node] + 1
                    queue.append(edge.to)
        return self._level[sink] >= 0

    def _send(self, node: int, sink: int, amount: int) -> int:
        if node == sink:
            return amount
        while self._next[node] < len(self._graph[node]):
            edge = self._graph[node][self._next[node]]
            if edge.cap > 0 and self._level[edge.to] == self._level[node] + 1:
                pushed = self._send(edge.to, sink, min(amount, edge.cap))
                if pushed:
                    edge.cap -= pushed
                    self._graph[edge.to][edge.rev].cap += pushed
                    return pushed
            self._next[node] += 1
        return 0
