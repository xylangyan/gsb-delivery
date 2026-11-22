"""Enterprise operating zones layered over public suffix rules.

An *operating zone* configuration lets an organization publish its own named
suffix lists on top of the public suffix rules. Each list carries an explicit
priority. Zone rules use the Public Suffix List format: exact rules
(``team.example.com``), wildcards (``*.example.com``), and exceptions
(``!hq.example.com``).

Matching compares the number of labels a rule declaration covers: a rule
covering more labels always wins. Ties between rules covering the same number
of labels are broken by source priority (the explicit list priority; public
rules have priority 0), never by the order the lists were passed in. When an
exception rule wins, the suffix actually used for grouping falls back one
label shorter than the exception's declaration.

Zone rules may subdivide a single original registered organization, but they
must never merge two different original registered organizations into one
operating group. `ZoneEvaluator.validate` checks this over the whole
potential host space beneath caller-provided managed DNS parent domains —
without enumerating sites and without any network access. If the combined
lists would merge across organizations, the report names a pair of legal
witness addresses and the rules involved, before anything is published.

    >>> zones = ZoneEvaluator(
    ...     public_rules=["com", "co.uk"],
    ...     zone_lists=[
    ...         ZoneList("regions", 10, ("*.example.com", "!hq.example.com")),
    ...         ZoneList("teams", 20, ("team.example.com",)),
    ...     ],
    ... )
    >>> match = zones.explain("app.team.example.com")
    >>> (match.suffix, match.group, match.rule, match.source)
    ('team.example.com', 'app.team.example.com', 'team.example.com', 'teams')
    >>> report = zones.validate(managed_parents=["example.com"])
    >>> report.boundary_unchanged
    True
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tldextract import TLDExtract

PUBLIC_SOURCE = "public"
"""The source name reported for rules from the public suffix list."""

PUBLIC_PRIORITY = 0
"""The priority of public suffix rules. Zone lists usually use higher values."""


class RuleKind(Enum):
    """The shape of a suffix rule declaration."""

    EXACT = "exact"
    WILDCARD = "wildcard"
    EXCEPTION = "exception"


_KIND_RANK = {
    RuleKind.WILDCARD: 0,
    RuleKind.EXACT: 1,
    RuleKind.EXCEPTION: 2,
}

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class ZoneRule:
    """A single parsed suffix rule in Public Suffix List format."""

    raw: str
    """The rule text as declared, e.g. ``*.example.com``."""

    kind: RuleKind
    """Whether the rule is exact, a wildcard, or an exception."""

    labels: tuple[str, ...]
    """The declared labels, left to right, without ``*`` or ``!`` markers."""

    @classmethod
    def parse(cls, raw: str) -> ZoneRule:
        """Parse a rule string in Public Suffix List format."""
        text = raw.strip().lower().rstrip(".")
        if text.startswith("!"):
            kind, body = RuleKind.EXCEPTION, text[1:]
        elif text == "*":
            kind, body = RuleKind.WILDCARD, ""
        elif text.startswith("*."):
            kind, body = RuleKind.WILDCARD, text[2:]
        else:
            kind, body = RuleKind.EXACT, text
        labels = tuple(body.split(".")) if body else ()
        if not labels and kind is not RuleKind.WILDCARD:
            raise ValueError(f"Empty suffix rule: {raw!r}")
        if any(not label for label in labels):
            raise ValueError(f"Invalid suffix rule: {raw!r}")
        return cls(raw=raw, kind=kind, labels=labels)

    @property
    def declared_levels(self) -> int:
        """The number of host labels this rule's declaration covers."""
        return len(self.labels) + (1 if self.kind is RuleKind.WILDCARD else 0)


@dataclass(frozen=True)
class ZoneList:
    """A named enterprise suffix list with an explicit priority."""

    name: str
    """The list's identifier, reported back in matches and violations."""

    priority: int
    """The explicit source priority. Higher wins same-level ties."""

    rules: Iterable[str] = ()
    """The list's rules, in Public Suffix List format."""

    def __post_init__(self) -> None:
        """Normalize and validate the declaration."""
        object.__setattr__(self, "rules", tuple(self.rules))
        if not self.name:
            raise ValueError("Zone lists require a non-empty name.")


@dataclass(frozen=True)
class ZoneMatch:
    """Which rule a host hits, from which list, and the grouping suffix used."""

    host: str
    """The host that was looked up."""

    matched: bool
    """Whether any rule matched the host."""

    suffix: str
    """The suffix actually used for grouping, after exception fallback."""

    group: str
    """The operating group: one label left of `suffix`, plus `suffix`."""

    rule: str
    """The winning rule's raw declaration, or the empty string."""

    source: str
    """The name of the list the winning rule came from, or the empty string."""

    priority: int
    """The explicit priority of the winning rule's source list."""

    kind: RuleKind | None
    """The winning rule's kind, or None if nothing matched."""


@dataclass(frozen=True)
class ZoneViolation:
    """A reproducible cross-organization merge found before publishing."""

    rule: str
    """The zone rule whose application merges organizations."""

    source: str
    """The name of the list declaring `rule`."""

    priority: int
    """The explicit priority of the list declaring `rule`."""

    group: str
    """The single operating group both witnesses are merged into."""

    witnesses: tuple[str, str]
    """Two legal host addresses that recompute to the same operating group."""

    organizations: tuple[str, str]
    """The witnesses' different original registered organizations."""

    public_rules: tuple[str, str]
    """The public rules that apply to each witness."""


@dataclass(frozen=True)
class ZoneValidationReport:
    """The pre-publish check result for a combination of zone lists."""

    zone_lists: tuple[str, ...]
    """The checked lists, as ``name(priority)``, highest priority first."""

    managed_parents: tuple[str, ...]
    """The managed DNS parent domains delimiting the checked host space."""

    violations: tuple[ZoneViolation, ...]
    """Every cross-organization merge found, empty if the boundary holds."""

    @property
    def ok(self) -> bool:
        """Whether the combination is safe to publish."""
        return not self.violations

    @property
    def boundary_unchanged(self) -> bool:
        """Whether the combination leaves the business boundary unchanged."""
        return not self.violations

    def summary(self) -> str:
        """Return a human-readable verdict for the pre-publish check."""
        if not self.violations:
            return (
                "Combination does not change the registered-organization "
                f"boundary: checked {len(self.zone_lists)} zone list(s) over "
                f"{len(self.managed_parents)} managed parent domain(s); no two "
                "potential hosts from different registered organizations "
                "resolve to the same operating group."
            )
        lines = [
            f"Combination merges across registered organizations: "
            f"{len(self.violations)} violation(s) found before publishing."
        ]
        for violation in self.violations:
            lines.append(
                f"- rule {violation.rule!r} from list {violation.source!r} "
                f"(priority {violation.priority}) merges "
                f"{violation.witnesses[0]!r} (organization "
                f"{violation.organizations[0]!r}, public rule "
                f"{violation.public_rules[0]!r}) and "
                f"{violation.witnesses[1]!r} (organization "
                f"{violation.organizations[1]!r}, public rule "
                f"{violation.public_rules[1]!r}) into operating group "
                f"{violation.group!r}."
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class _Entry:
    """A parsed rule plus its source list metadata."""

    rule: ZoneRule
    source: str
    priority: int


@dataclass(frozen=True)
class _Match:
    """A rule matching a host, with the suffix it yields."""

    entry: _Entry
    suffix_labels: tuple[str, ...]


class _Node:
    """A reversed-label trie node holding rule entries."""

    __slots__ = ("children", "exact", "exceptions", "wildcard")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.exact: list[_Entry] = []
        self.wildcard: list[_Entry] = []
        self.exceptions: dict[str, list[_Entry]] = {}


def _add_entry(root: _Node, entry: _Entry) -> None:
    rule = entry.rule
    node = root
    path = rule.labels[1:] if rule.kind is RuleKind.EXCEPTION else rule.labels
    for label in reversed(path):
        node = node.children.setdefault(label, _Node())
    if rule.kind is RuleKind.EXACT:
        node.exact.append(entry)
    elif rule.kind is RuleKind.WILDCARD:
        node.wildcard.append(entry)
    else:
        node.exceptions.setdefault(rule.labels[0], []).append(entry)


def _match_key(match: _Match) -> tuple[int, int, int, str, str]:
    entry = match.entry
    return (
        entry.rule.declared_levels,
        entry.priority,
        _KIND_RANK[entry.rule.kind],
        entry.rule.raw,
        entry.source,
    )


def _best_match(root: _Node, host_labels: tuple[str, ...]) -> _Match | None:
    best: _Match | None = None
    node = root
    for index in range(len(host_labels) - 1, -1, -1):
        label = host_labels[index]
        for entry in node.wildcard:
            candidate = _Match(entry, host_labels[index:])
            if best is None or _match_key(candidate) > _match_key(best):
                best = candidate
        for entry in node.exceptions.get(label, ()):
            candidate = _Match(entry, host_labels[index + 1 :])
            if best is None or _match_key(candidate) > _match_key(best):
                best = candidate
        child = node.children.get(label)
        if child is None:
            break
        node = child
        for entry in node.exact:
            candidate = _Match(entry, host_labels[index:])
            if best is None or _match_key(candidate) > _match_key(best):
                best = candidate
    return best


def _split_host(host: str) -> tuple[str, ...]:
    text = (
        host.strip()
        .lower()
        .replace("。", ".")
        .replace("．", ".")
        .replace("｡", ".")
        .rstrip(".")
    )
    return tuple(label for label in text.split(".") if label)


def _is_suffix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return (
        len(shorter) <= len(longer) and longer[len(longer) - len(shorter) :] == shorter
    )


def _group_labels(
    host_labels: tuple[str, ...], suffix_labels: tuple[str, ...]
) -> tuple[str, ...]:
    cut = len(host_labels) - len(suffix_labels) - 1
    return host_labels[cut:] if cut >= 0 else host_labels


class ZoneEvaluator:
    """Matches hosts against public rules plus prioritized enterprise zone lists."""

    def __init__(
        self,
        public_rules: Iterable[str] = (),
        zone_lists: Sequence[ZoneList] = (),
    ) -> None:
        """Combine public suffix rules with enterprise zone lists.

        `public_rules` are rule strings in Public Suffix List format. Each
        `ZoneList` contributes its rules under its explicit `priority`. The
        order lists are passed in does not affect any result; only the
        explicit priorities do.
        """
        self._public_root = _Node()
        self._combined_root = _Node()
        self._zone_entries: list[_Entry] = []
        self._rule_labels: set[str] = set()

        for raw in public_rules:
            entry = _Entry(ZoneRule.parse(raw), PUBLIC_SOURCE, PUBLIC_PRIORITY)
            _add_entry(self._public_root, entry)
            _add_entry(self._combined_root, entry)
            self._rule_labels.update(entry.rule.labels)

        self.zone_lists: tuple[ZoneList, ...] = tuple(
            sorted(
                zone_lists, key=lambda zone_list: (-zone_list.priority, zone_list.name)
            )
        )
        for zone_list in self.zone_lists:
            for raw in zone_list.rules:
                entry = _Entry(ZoneRule.parse(raw), zone_list.name, zone_list.priority)
                _add_entry(self._combined_root, entry)
                self._zone_entries.append(entry)
                self._rule_labels.update(entry.rule.labels)

    @classmethod
    def from_tldextract(
        cls,
        extractor: TLDExtract,
        zone_lists: Sequence[ZoneList] = (),
        include_psl_private_domains: bool | None = None,
    ) -> ZoneEvaluator:
        """Build an evaluator from a `TLDExtract` instance's loaded public rules.

        Uses the suffix rules the extractor already resolved (cache, fetched
        URLs, or bundled snapshot); this method performs no network access of
        its own beyond what loading the extractor's rules requires.
        """
        tlds = extractor._get_tld_extractor().tlds(include_psl_private_domains)
        return cls(public_rules=sorted(tlds), zone_lists=zone_lists)

    def explain(self, host: str) -> ZoneMatch:
        """Report the zone-mode match: which rule hits, from which list, and the grouping suffix."""
        return self._explain(host, self._combined_root)

    def explain_public(self, host: str) -> ZoneMatch:
        """Report the non-zone-mode match, using the original public rules only."""
        return self._explain(host, self._public_root)

    def validate(self, managed_parents: Iterable[str]) -> ZoneValidationReport:
        """Pre-publish check that the combination never merges across organizations.

        The checked host space is every legal host beneath the given managed
        DNS parent domains, including hosts never seen in any sample. Zone
        rules may subdivide one original registered organization; a violation
        is reported only if two potential hosts with different original
        registered organizations (per the public rules) resolve to the same
        operating group. Violations come with a pair of legal witness
        addresses and the rules involved, so the finding can be recomputed
        before publishing.
        """
        parents = tuple(_split_host(parent) for parent in managed_parents)
        forbidden = set(self._rule_labels)
        for parent in parents:
            forbidden.update(parent)

        used: set[str] = set()
        counter = 0

        def fresh_label() -> str:
            nonlocal counter
            while True:
                candidate = f"zxq{counter:x}"
                counter += 1
                if candidate not in forbidden and candidate not in used:
                    used.add(candidate)
                    return candidate

        violations: list[ZoneViolation] = []
        for entry in self._zone_entries:
            if entry.rule.kind is not RuleKind.EXCEPTION:
                continue
            activated = self._find_activations(entry, parents, fresh_label)
            violation = self._build_violation(
                entry, entry.rule.labels, activated, parents, fresh_label
            )
            if violation is not None:
                violations.append(violation)

        return ZoneValidationReport(
            zone_lists=tuple(
                f"{zone_list.name}({zone_list.priority})"
                for zone_list in self.zone_lists
            ),
            managed_parents=tuple(".".join(parent) for parent in parents),
            violations=tuple(violations),
        )

    def _find_activations(
        self,
        entry: _Entry,
        parents: tuple[tuple[str, ...], ...],
        fresh_label: Callable[[], str],
    ) -> list[tuple[tuple[str, ...], tuple[str, ...], bool]]:
        """Probe each managed parent for hosts where an exception shortens the grouping suffix."""
        exception_labels = entry.rule.labels
        fallback_labels = exception_labels[1:]
        activated: list[tuple[tuple[str, ...], tuple[str, ...], bool]] = []
        for parent in parents:
            if not (
                _is_suffix(parent, exception_labels)
                or _is_suffix(exception_labels, parent)
            ):
                continue
            base = exception_labels if _is_suffix(parent, exception_labels) else parent
            probe = (fresh_label(),) + base
            zone_match = _best_match(self._combined_root, probe)
            if (
                zone_match is None
                or zone_match.entry is not entry
                or zone_match.suffix_labels != fallback_labels
            ):
                continue
            public_match = _best_match(self._public_root, probe)
            public_labels = (
                public_match.suffix_labels if public_match is not None else ()
            )
            if len(public_labels) <= len(fallback_labels):
                continue
            if len(probe) <= len(public_labels):
                continue
            organization = probe[len(probe) - len(public_labels) - 1 :]
            activated.append(
                (probe, organization, _is_suffix(parent, exception_labels))
            )
        return activated

    def _explain(self, host: str, root: _Node) -> ZoneMatch:
        labels = _split_host(host)
        match = _best_match(root, labels)
        if match is None:
            return ZoneMatch(
                host=host,
                matched=False,
                suffix="",
                group=labels[-1] if labels else "",
                rule="",
                source="",
                priority=PUBLIC_PRIORITY,
                kind=None,
            )
        return ZoneMatch(
            host=host,
            matched=True,
            suffix=".".join(match.suffix_labels),
            group=".".join(_group_labels(labels, match.suffix_labels)),
            rule=match.entry.rule.raw,
            source=match.entry.source,
            priority=match.entry.priority,
            kind=match.entry.rule.kind,
        )

    def _organization(self, host_labels: tuple[str, ...]) -> tuple[str, ...]:
        public_match = _best_match(self._public_root, host_labels)
        if public_match is None or len(host_labels) <= len(public_match.suffix_labels):
            return ()
        return _group_labels(host_labels, public_match.suffix_labels)

    def _verify_witnesses(
        self,
        first: tuple[str, ...],
        second: tuple[str, ...],
        parents: tuple[tuple[str, ...], ...],
    ) -> bool:
        for witness in (first, second):
            if not any(_is_suffix(parent, witness) for parent in parents):
                return False
            if not all(_DNS_LABEL_RE.match(label) for label in witness):
                return False
        groups = []
        for witness in (first, second):
            match = _best_match(self._combined_root, witness)
            suffix_labels = match.suffix_labels if match is not None else ()
            groups.append(_group_labels(witness, suffix_labels))
        if not groups[0] or groups[0] != groups[1]:
            return False
        first_org = self._organization(first)
        second_org = self._organization(second)
        return bool(first_org) and bool(second_org) and first_org != second_org

    def _build_violation(
        self,
        entry: _Entry,
        exception_labels: tuple[str, ...],
        activated: list[tuple[tuple[str, ...], tuple[str, ...], bool]],
        parents: tuple[tuple[str, ...], ...],
        fresh_label: Callable[[], str],
    ) -> ZoneViolation | None:
        if not activated:
            return None
        candidates: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        if any(
            exception_extends_parent for _, _, exception_extends_parent in activated
        ):
            for _ in range(4):
                candidates.append(
                    (
                        (fresh_label(),) + exception_labels,
                        (fresh_label(),) + exception_labels,
                    )
                )
        by_organization: dict[tuple[str, ...], tuple[str, ...]] = {}
        for probe, organization, _ in activated:
            by_organization.setdefault(organization, probe)
        probes = list(by_organization.values())
        for later in range(1, len(probes)):
            for earlier in range(later):
                candidates.append((probes[earlier], probes[later]))
        for first, second in candidates:
            if not self._verify_witnesses(first, second, parents):
                continue
            public_rules = []
            for witness in (first, second):
                public_match = _best_match(self._public_root, witness)
                public_rules.append(
                    public_match.entry.rule.raw if public_match is not None else ""
                )
            group_match = _best_match(self._combined_root, first)
            group_labels = _group_labels(
                first, group_match.suffix_labels if group_match is not None else ()
            )
            return ZoneViolation(
                rule=entry.rule.raw,
                source=entry.source,
                priority=entry.priority,
                group=".".join(group_labels),
                witnesses=(".".join(first), ".".join(second)),
                organizations=(
                    ".".join(self._organization(first)),
                    ".".join(self._organization(second)),
                ),
                public_rules=(public_rules[0], public_rules[1]),
            )
        return None
