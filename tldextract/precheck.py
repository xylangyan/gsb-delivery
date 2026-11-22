"""Replayable site-log prechecks with versioned missing-value policies.

The Public Suffix List tells us how to split a hostname, but site logs often
need a second decision: is this row a real site to archive, an IP address, an
internal host, a bare suffix, a missing value, or something that needs a human?
That decision can change after the fact, because data providers deliver late
rows and late corrections to the rules that say which tokens mean "missing".

This module keeps the *facts* of every row revision, not just its last
classification. A correction to a rule can therefore be applied to the affected
data days without re-reading the original log files. Every accounting change is
published as a replayable withdrawal plus a new classification, so a downstream
consumer can re-apply the stream without double counting.

Rows are identified by ``source``, ``data day`` and ``record number``. The record
number is only unique inside one source and one day. Within that key, the
highest row revision wins. Two different contents at the same highest revision
are a conflict: the row is counted once as pending verification and any earlier
site record for it is withdrawn.

Only local suffix rules are read. Pass a local suffix-list file with
``suffix_list_path`` or rely on the package's bundled snapshot.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .remote import lenient_netloc
from .tldextract import TLDExtract

ARCHIVED: Final = "archived"
IP: Final = "ip"
INTERNAL: Final = "internal"
SUFFIX_ONLY: Final = "suffix_only"
INVALID: Final = "invalid"
MISSING: Final = "missing"
REVIEW_TEXT: Final = "review_text"
CONFLICT: Final = "conflict"
PENDING: Final = "pending"

CATEGORIES: Final = (
    ARCHIVED,
    IP,
    INTERNAL,
    SUFFIX_ONLY,
    INVALID,
    MISSING,
    REVIEW_TEXT,
    CONFLICT,
)

BUCKETS: Final = (
    ARCHIVED,
    IP,
    INTERNAL,
    SUFFIX_ONLY,
    INVALID,
    MISSING,
    PENDING,
)

# Review text and same-revision conflicts both wait for a human. They share one
# accounting bucket so a record can never be counted twice as "pending".
PENDING_BUCKETS: Final = frozenset({PENDING})

PRIMARY: Final = "primary"
FALLBACK: Final = "fallback"

RowKey = tuple[str, str, str]
RowContent = tuple[str | None, str | None]


class PrecheckError(ValueError):
    """Raised when a precheck event or rule is not usable."""


@dataclass(frozen=True)
class RuleConfig:
    """One revision of a source's missing-value policy.

    A config covers a data day when the day is inside
    ``[effective_from, effective_to]``. Open ends are ``None``. When several
    revisions cover the same day, the highest revision wins.
    """

    source: str
    revision: int
    missing_values: tuple[str, ...] = ("-", "")
    review_values: tuple[str, ...] = ()
    effective_from: str | None = None
    effective_to: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        """Normalize and validate the revision."""
        if not self.source.strip():
            raise PrecheckError("config source must not be empty")
        if self.revision < 0:
            raise PrecheckError("config revision must be non-negative")
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(
            self,
            "missing_values",
            tuple(str(value) for value in self.missing_values),
        )
        object.__setattr__(
            self,
            "review_values",
            tuple(str(value) for value in self.review_values),
        )
        _validate_day(self.effective_from, "effective_from")
        _validate_day(self.effective_to, "effective_to")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_from > self.effective_to
        ):
            raise PrecheckError("effective_from must not be after effective_to")

    def covers(self, day: str) -> bool:
        """Return whether this revision applies to ``day``."""
        return (self.effective_from is None or self.effective_from <= day) and (
            self.effective_to is None or day <= self.effective_to
        )


@dataclass(frozen=True)
class RowFact:
    """One received revision of one source record.

    Duplicate identical facts are collapsed. Different facts at the same
    revision are kept so the engine can report a conflict instead of silently
    picking whichever arrived last.
    """

    source: str
    day: str
    record_no: str
    revision: int
    primary: str | None = None
    fallback: str | None = None

    def __post_init__(self) -> None:
        """Normalize and validate the row identity."""
        if not self.source.strip():
            raise PrecheckError("row source must not be empty")
        if not str(self.record_no).strip():
            raise PrecheckError("row record_no must not be empty")
        _validate_day(self.day, "day")
        if self.revision < 1:
            raise PrecheckError("row revision must be at least 1")
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "day", str(self.day))
        object.__setattr__(self, "record_no", str(self.record_no))
        object.__setattr__(
            self, "primary", None if self.primary is None else str(self.primary)
        )
        object.__setattr__(
            self, "fallback", None if self.fallback is None else str(self.fallback)
        )

    @property
    def key(self) -> RowKey:
        """The identity shared by every revision of this source record."""
        return (self.source, self.day, self.record_no)

    @property
    def content(self) -> RowContent:
        """The values that distinguish one row revision's content."""
        return (self.primary, self.fallback)


@dataclass(frozen=True)
class Classification:
    """The current outcome for one source record."""

    source: str
    day: str
    record_no: str
    category: str
    bucket: str
    site: str
    domain: str
    suffix: str
    is_private: bool
    value_used: str | None
    primary: str | None
    fallback: str | None
    config_revision: int | None
    content_revision: int | None
    reason: str
    detail: str
    pending: bool
    conflict: tuple[RowContent, ...] = ()

    @property
    def key(self) -> RowKey:
        """The identity of the classified record."""
        return (self.source, self.day, self.record_no)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation, including its rule trace."""
        return {
            "source": self.source,
            "day": self.day,
            "record_no": self.record_no,
            "category": self.category,
            "bucket": self.bucket,
            "site": self.site,
            "domain": self.domain,
            "suffix": self.suffix,
            "is_private": self.is_private,
            "value_used": self.value_used,
            "primary": self.primary,
            "fallback": self.fallback,
            "config_revision": self.config_revision,
            "content_revision": self.content_revision,
            "reason": self.reason,
            "detail": self.detail,
            "pending": self.pending,
            "conflict": [list(content) for content in self.conflict],
        }


@dataclass(frozen=True)
class Update:
    """A replayable accounting change for a downstream consumer."""

    update_id: str
    op: str
    bucket: str
    delta: int
    source: str
    day: str
    record_no: str
    category: str
    site: str
    domain: str
    suffix: str
    is_private: bool
    value_used: str | None
    config_revision: int | None
    content_revision: int | None
    reason: str
    from_bucket: str | None
    to_bucket: str | None
    sequence: int

    @property
    def key(self) -> RowKey:
        """The record this update belongs to."""
        return (self.source, self.day, self.record_no)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        return {
            "update_id": self.update_id,
            "op": self.op,
            "bucket": self.bucket,
            "delta": self.delta,
            "source": self.source,
            "day": self.day,
            "record_no": self.record_no,
            "category": self.category,
            "site": self.site,
            "domain": self.domain,
            "suffix": self.suffix,
            "is_private": self.is_private,
            "value_used": self.value_used,
            "config_revision": self.config_revision,
            "content_revision": self.content_revision,
            "reason": self.reason,
            "from_bucket": self.from_bucket,
            "to_bucket": self.to_bucket,
            "sequence": self.sequence,
        }

    def to_json(self) -> str:
        """Serialize this update as one JSON line."""
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


class SiteLogPrecheck:
    """Classify site-log rows and emit replayable corrections.

    The engine is event-sourced. Adding a late row or a rule correction
    recomputes affected records from retained row facts. It never re-opens the
    original JSON Lines file.
    """

    def __init__(
        self,
        *,
        suffix_list_path: str | Path | None = None,
        internal_hosts: Iterable[str] = (),
        default_missing_values: Sequence[str] = ("-", ""),
        extractor: TLDExtract | None = None,
    ) -> None:
        """Create an empty engine.

        Args:
            suffix_list_path: Optional local Public Suffix List file. When
                omitted, the package's bundled snapshot is used. No network
                access is attempted.
            internal_hosts: Hostnames, or parent domains, that the caller has
                registered as internal. Matching is case-insensitive and
                respects dot boundaries.
            default_missing_values: Tokens treated as missing when no config
                revision covers a row's data day.
            extractor: Optional preconfigured ``TLDExtract``. The caller is
                responsible for keeping it local.
        """
        if extractor is None:
            if suffix_list_path is None:
                extractor = TLDExtract(
                    cache_dir=None,
                    suffix_list_urls=(),
                    fallback_to_snapshot=True,
                )
            else:
                suffix_uri = Path(suffix_list_path).expanduser().resolve().as_uri()
                extractor = TLDExtract(
                    cache_dir=None,
                    suffix_list_urls=(suffix_uri,),
                    fallback_to_snapshot=False,
                )

        self._extractor = extractor
        self._default_missing_values = tuple(
            str(value) for value in default_missing_values
        )
        self._internal_hosts = tuple(
            sorted(
                {
                    _normalize_internal_host(host)
                    for host in internal_hosts
                    if host.strip()
                }
            )
        )

        self._configs: dict[str, dict[int, RuleConfig]] = defaultdict(dict)
        self._rows: dict[RowKey, dict[int, dict[RowContent, RowFact]]] = {}
        self._source_keys: dict[str, set[RowKey]] = defaultdict(set)
        self._published: dict[RowKey, Classification] = {}
        self._history: dict[RowKey, list[dict[str, Any]]] = defaultdict(list)
        self._sequence: dict[RowKey, int] = defaultdict(lambda: -1)
        self._updates: list[Update] = []
        self._new_updates: list[Update] = []

    @property
    def updates(self) -> tuple[Update, ...]:
        """All updates emitted so far, oldest first."""
        return tuple(self._updates)

    def drain_updates(self) -> tuple[Update, ...]:
        """Return updates emitted since the previous drain, then clear them."""
        drained = tuple(self._new_updates)
        self._new_updates.clear()
        return drained

    def add_config(self, config: RuleConfig) -> None:
        """Add a rule revision, reclassifying its source if necessary.

        Re-adding an identical revision is a no-op. Reusing a revision number
        with different content is an error, because it would make the outcome
        depend on arrival order.
        """
        revisions = self._configs[config.source]
        existing = revisions.get(config.revision)
        if existing is not None:
            if existing != config:
                raise PrecheckError(
                    f"config source {config.source!r} revision {config.revision}"
                    " was already received with different content"
                )
            return

        revisions[config.revision] = config
        self._reclassify_source(config.source)

    def add_row(self, row: RowFact) -> bool:
        """Add one row fact.

        Returns ``True`` when the fact was new, and ``False`` when the exact
        same revision and content had already been received.
        """
        revisions = self._rows.setdefault(row.key, {})
        self._source_keys[row.source].add(row.key)
        contents = revisions.setdefault(row.revision, {})
        if row.content in contents:
            return False

        contents[row.content] = row
        self._reclassify_key(row.key)
        return True

    def ingest_event(self, event: Mapping[str, Any]) -> None:
        """Ingest one decoded config or row event."""
        kind = _event_kind(event)
        if kind == "config":
            self.add_config(_config_from_mapping(event))
        else:
            self.add_row(_row_from_mapping(event))

    def ingest_line(self, line: str) -> None:
        """Ingest one JSON Lines entry."""
        text = line.strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PrecheckError(f"invalid JSON line: {exc}") from exc
        if not isinstance(event, Mapping):
            raise PrecheckError("each JSON line must contain a JSON object")
        self.ingest_event(event)

    def ingest_jsonl(self, lines: Iterable[str]) -> None:
        """Ingest a sequence of JSON Lines entries."""
        for line_no, line in enumerate(lines, start=1):
            try:
                self.ingest_line(line)
            except PrecheckError as exc:
                raise PrecheckError(f"line {line_no}: {exc}") from exc

    def ingest_file(self, path: str | Path) -> None:
        """Read a UTF-8 JSON Lines file once and retain its row facts."""
        with open(path, encoding="utf-8") as event_file:
            self.ingest_jsonl(event_file)

    def counts(self) -> dict[str, int]:
        """Return current record counts for every accounting bucket."""
        counts = dict.fromkeys(BUCKETS, 0)
        for classification in self._published.values():
            counts[classification.bucket] += 1
        return counts

    def pending_review(self) -> int:
        """Return the number of rows awaiting review or verification."""
        counts = self.counts()
        return sum(counts[bucket] for bucket in PENDING_BUCKETS)

    def reconciliation(self) -> dict[str, Any]:
        """Return a reconciliation report for the current records."""
        counts = self.counts()
        records = len(self._published)
        total = sum(counts.values())
        return {
            "records": records,
            "counts": counts,
            "pending_review": self.pending_review(),
            "sum": total,
            "balanced": records == total,
        }

    def record(self, source: str, day: str, record_no: str) -> Classification | None:
        """Return the current classification for one record, if any."""
        return self._published.get((source, day, str(record_no)))

    def records(self) -> tuple[Classification, ...]:
        """Return all current classifications sorted by source/day/number."""
        return tuple(self._published[key] for key in sorted(self._published))

    def trace(self, source: str, day: str, record_no: str) -> dict[str, Any]:
        """Return the current record plus the revisions that changed it."""
        key = (source, day, str(record_no))
        current = self._published.get(key)
        return {
            "source": source,
            "day": day,
            "record_no": str(record_no),
            "current": None if current is None else current.as_dict(),
            "history": list(self._history.get(key, ())),
            "config": self._config_as_dict(self._effective_config(source, day)),
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize all retained configs, row facts, and emitted updates.

        A downstream process can rebuild the same record outcomes from this
        snapshot without the original log files.
        """
        configs = [
            {
                "source": config.source,
                "revision": config.revision,
                "missing_values": list(config.missing_values),
                "review_values": list(config.review_values),
                "effective_from": config.effective_from,
                "effective_to": config.effective_to,
                "note": config.note,
            }
            for source in sorted(self._configs)
            for config in (
                self._configs[source][revision]
                for revision in sorted(self._configs[source])
            )
        ]
        rows = [
            {
                "source": key[0],
                "day": key[1],
                "record_no": key[2],
                "revision": revision,
                "primary": fact.primary,
                "fallback": fact.fallback,
            }
            for key in sorted(self._rows)
            for revision in sorted(self._rows[key])
            for fact in (
                self._rows[key][revision][content]
                for content in sorted(
                    self._rows[key][revision],
                    key=lambda content: (
                        content[0] or "",
                        content[1] or "",
                    ),
                )
            )
        ]
        return {
            "version": 1,
            "settings": {
                "internal_hosts": list(self._internal_hosts),
                "default_missing_values": list(self._default_missing_values),
            },
            "configs": configs,
            "rows": rows,
            "history": {
                _key_id(key): list(entries) for key, entries in self._history.items()
            },
            "updates": [update.as_dict() for update in self._updates],
        }

    def save(self, path: str | Path) -> None:
        """Write a JSON snapshot that ``load`` can restore."""
        target = Path(path)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def load(self, state: Mapping[str, Any]) -> None:
        """Restore configs and row facts from a snapshot.

        The snapshot is treated as the canonical already-sent stream. Restoring
        does not emit new updates, and identical later input is absorbed
        without changing the stream.
        """
        version = state.get("version", 1)
        if version != 1:
            raise PrecheckError(f"unsupported snapshot version {version!r}")

        configs = [_config_from_mapping(item) for item in state.get("configs", ())]
        rows = [_row_from_mapping(item) for item in state.get("rows", ())]
        self._restore_settings(state.get("settings"))

        self._configs.clear()
        self._rows.clear()
        self._source_keys.clear()
        self._published.clear()
        self._history.clear()
        self._sequence.clear()

        for config in configs:
            existing = self._configs[config.source].get(config.revision)
            if existing is not None and existing != config:
                raise PrecheckError(
                    "snapshot has conflicting config source"
                    f" {config.source!r} revision {config.revision}"
                )
            self._configs[config.source][config.revision] = config

        for row in rows:
            self._rows.setdefault(row.key, {}).setdefault(row.revision, {})[
                row.content
            ] = row
            self._source_keys[row.source].add(row.key)

        self._restore_history(state.get("history"))
        self._restore_updates(state.get("updates"))

        for key in sorted(self._rows):
            self._published[key] = self._classify_key(key)

    def _restore_history(self, stored: object) -> None:
        for key in self._rows:
            self._sequence[key] = -1
        if not isinstance(stored, Mapping):
            return
        for key in self._rows:
            entries = stored.get(_key_id(key))
            if isinstance(entries, Sequence) and not isinstance(entries, str):
                self._history[key] = [
                    dict(entry) for entry in entries if isinstance(entry, Mapping)
                ]

    def _restore_updates(self, stored: object) -> None:
        self._new_updates.clear()
        if not isinstance(stored, Sequence) or isinstance(stored, str):
            return
        self._updates = [_update_from_mapping(item) for item in stored]
        for update in self._updates:
            self._sequence[update.key] = max(
                self._sequence[update.key], update.sequence
            )

    def load_file(self, path: str | Path) -> None:
        """Read a JSON snapshot and restore it."""
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrecheckError(f"cannot read snapshot {path}: {exc}") from exc
        if not isinstance(state, Mapping):
            raise PrecheckError("snapshot must contain a JSON object")
        self.load(state)

    def _restore_settings(self, settings: object) -> None:
        if not isinstance(settings, Mapping):
            return

        internal_hosts = settings.get("internal_hosts")
        if isinstance(internal_hosts, Sequence) and not isinstance(internal_hosts, str):
            self._internal_hosts = tuple(
                sorted(_normalize_internal_host(str(item)) for item in internal_hosts)
            )

        default_missing = settings.get("default_missing_values")
        if isinstance(default_missing, Sequence) and not isinstance(
            default_missing, str
        ):
            self._default_missing_values = tuple(str(item) for item in default_missing)

    def _reclassify_source(self, source: str) -> None:
        for key in sorted(self._source_keys.get(source, ())):
            self._reclassify_key(key)

    def _reclassify_key(self, key: RowKey) -> None:
        classification = self._classify_key(key)
        previous = self._published.get(key)
        self._published[key] = classification

        if previous is None:
            sequence = self._next_sequence(key)
            self._publish(
                op="classify",
                classification=classification,
                bucket=classification.bucket,
                delta=1,
                reason=classification.reason,
                from_bucket=None,
                to_bucket=classification.bucket,
                sequence=sequence,
            )
            self._history[key].append(
                _history_entry(
                    previous=None,
                    current=classification,
                    sequence=sequence,
                )
            )
            return

        if _same_accounting(previous, classification):
            if (
                previous.config_revision != classification.config_revision
                or previous.content_revision != classification.content_revision
                or previous.value_used != classification.value_used
            ):
                self._history[key].append(
                    _history_entry(
                        previous=previous,
                        current=classification,
                        sequence=self._next_sequence(key),
                    )
                )
            return

        sequence = self._next_sequence(key)
        self._publish(
            op="withdraw",
            classification=previous,
            bucket=previous.bucket,
            delta=-1,
            reason=f"superseded_by:{classification.reason}",
            from_bucket=previous.bucket,
            to_bucket=classification.bucket,
            sequence=sequence,
        )
        self._publish(
            op="classify",
            classification=classification,
            bucket=classification.bucket,
            delta=1,
            reason=classification.reason,
            from_bucket=previous.bucket,
            to_bucket=classification.bucket,
            sequence=sequence,
        )
        self._history[key].append(
            _history_entry(
                previous=previous,
                current=classification,
                sequence=sequence,
            )
        )

    def _next_sequence(self, key: RowKey) -> int:
        self._sequence[key] += 1
        return self._sequence[key]

    def _publish(
        self,
        *,
        op: str,
        classification: Classification,
        bucket: str,
        delta: int,
        reason: str,
        from_bucket: str | None,
        to_bucket: str | None,
        sequence: int,
    ) -> None:
        payload = {
            "source": classification.source,
            "day": classification.day,
            "record_no": classification.record_no,
            "op": op,
            "bucket": bucket,
            "delta": delta,
            "category": classification.category,
            "config_revision": classification.config_revision,
            "content_revision": classification.content_revision,
            "reason": reason,
            "from_bucket": from_bucket,
            "to_bucket": to_bucket,
            "sequence": sequence,
        }
        update_id = hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()[:24]
        update = Update(
            update_id=update_id,
            op=op,
            bucket=bucket,
            delta=delta,
            source=classification.source,
            day=classification.day,
            record_no=classification.record_no,
            category=classification.category,
            site=classification.site,
            domain=classification.domain,
            suffix=classification.suffix,
            is_private=classification.is_private,
            value_used=classification.value_used,
            config_revision=classification.config_revision,
            content_revision=classification.content_revision,
            reason=reason,
            from_bucket=from_bucket,
            to_bucket=to_bucket,
            sequence=sequence,
        )
        self._updates.append(update)
        self._new_updates.append(update)

    def _classify_key(self, key: RowKey) -> Classification:
        revisions = self._rows[key]
        highest_revision = max(revisions)
        contents = revisions[highest_revision]

        if len(contents) > 1:
            ordered = sorted(
                contents.values(),
                key=lambda fact: (fact.primary or "", fact.fallback or ""),
            )
            conflict_config = self._effective_config(key[0], key[1])
            return Classification(
                source=key[0],
                day=key[1],
                record_no=key[2],
                category=CONFLICT,
                bucket=PENDING,
                site="",
                domain="",
                suffix="",
                is_private=False,
                value_used=None,
                primary=None,
                fallback=None,
                config_revision=(
                    None if conflict_config is None else conflict_config.revision
                ),
                content_revision=highest_revision,
                reason="same_revision_conflict",
                detail=(
                    f"revision {highest_revision} was received with"
                    f" {len(contents)} different contents; counted once for review"
                ),
                pending=True,
                conflict=tuple(fact.content for fact in ordered),
            )

        fact = next(iter(contents.values()))
        config = self._effective_config(fact.source, fact.day)
        missing_values = (
            config.missing_values
            if config is not None
            else self._default_missing_values
        )
        review_values = config.review_values if config is not None else ()
        config_revision = config.revision if config is not None else None

        # A token declared as review text is meaningful content, even if an old
        # default also listed it as a missing marker.
        effective_missing = tuple(
            token
            for token in missing_values
            if token.strip() not in {value.strip() for value in review_values}
        )

        if _is_missing(fact.primary, effective_missing):
            if _is_missing(fact.fallback, effective_missing):
                return self._classification(
                    fact,
                    config_revision,
                    category=MISSING,
                    site="",
                    domain="",
                    suffix="",
                    is_private=False,
                    value_used=None,
                    reason="primary_missing_no_fallback",
                    detail="primary is missing and no usable fallback was provided",
                )
            value = fact.fallback
            value_used = FALLBACK
        else:
            value = fact.primary
            value_used = PRIMARY

        assert value is not None
        if _is_review_text(value, review_values):
            return self._classification(
                fact,
                config_revision,
                category=REVIEW_TEXT,
                site="",
                domain="",
                suffix="",
                is_private=False,
                value_used=value_used,
                reason="declared_review_text",
                detail=(f"{value!r} is declared as raw review text, not a site value"),
            )

        (
            category,
            site,
            domain,
            suffix,
            is_private,
            reason,
            detail,
        ) = self._classify_value(value)
        return self._classification(
            fact,
            config_revision,
            category=category,
            site=site,
            domain=domain,
            suffix=suffix,
            is_private=is_private,
            value_used=value_used,
            reason=reason,
            detail=detail,
        )

    def _classification(
        self,
        fact: RowFact,
        config_revision: int | None,
        *,
        category: str,
        site: str,
        domain: str,
        suffix: str,
        is_private: bool,
        value_used: str | None,
        reason: str,
        detail: str,
    ) -> Classification:
        bucket = PENDING if category in {REVIEW_TEXT, CONFLICT} else category
        return Classification(
            source=fact.source,
            day=fact.day,
            record_no=fact.record_no,
            category=category,
            bucket=bucket,
            site=site,
            domain=domain,
            suffix=suffix,
            is_private=is_private,
            value_used=value_used,
            primary=fact.primary,
            fallback=fact.fallback,
            config_revision=config_revision,
            content_revision=fact.revision,
            reason=reason,
            detail=detail,
            pending=bucket in PENDING_BUCKETS,
        )

    def _effective_config(self, source: str, day: str) -> RuleConfig | None:
        revisions = self._configs.get(source)
        if not revisions:
            return None
        candidates = [config for config in revisions.values() if config.covers(day)]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda config: (config.revision, config.effective_from or ""),
        )

    @staticmethod
    def _config_as_dict(config: RuleConfig | None) -> dict[str, Any] | None:
        if config is None:
            return None
        return {
            "source": config.source,
            "revision": config.revision,
            "missing_values": list(config.missing_values),
            "review_values": list(config.review_values),
            "effective_from": config.effective_from,
            "effective_to": config.effective_to,
        }

    def _classify_value(self, value: str) -> tuple[str, str, str, str, bool, str, str]:
        text = value.strip()
        host = _host_candidate(text)

        if self._is_internal(host):
            return (
                INTERNAL,
                "",
                "",
                "",
                False,
                "internal_host",
                f"{text!r} matches a registered internal host",
            )

        if _looks_like_ip(host):
            return (
                IP,
                "",
                "",
                "",
                False,
                "ip_address",
                f"{text!r} is an IP address",
            )

        extraction = self._extractor(text)
        if extraction.ipv4 or extraction.ipv6:
            return (
                IP,
                "",
                "",
                "",
                False,
                "ip_address",
                f"{text!r} is an IP address",
            )

        if (
            extraction.domain
            and extraction.suffix
            and extraction.fqdn
            and _valid_hostname(extraction.fqdn)
        ):
            return (
                ARCHIVED,
                extraction.fqdn,
                extraction.domain,
                extraction.suffix,
                extraction.is_private,
                "archived_site",
                f"{text!r} has registrable site {extraction.fqdn!r}",
            )

        if (
            extraction.suffix
            and not extraction.domain
            and host
            and _valid_hostname(host)
        ):
            return (
                SUFFIX_ONLY,
                "",
                "",
                extraction.suffix,
                extraction.is_private,
                "suffix_only",
                f"{text!r} contains a suffix but no registrable domain",
            )

        return (
            INVALID,
            "",
            "",
            "",
            False,
            "invalid_input",
            f"{text!r} does not contain a usable site and is not a declared"
            " missing value",
        )

    def _is_internal(self, host: str) -> bool:
        if not host:
            return False
        normalized = host.casefold().rstrip(".")
        return any(
            normalized == entry or normalized.endswith("." + entry)
            for entry in self._internal_hosts
        )


class UpdateLedger:
    """Apply an update stream with order-independent, replay-safe accounting.

    Updates carry a per-record ``sequence``. The ledger resolves each record to
    the classification emitted at its highest sequence, so replayed or
    out-of-order updates converge to the same totals instead of accumulating a
    duplicate delta.
    """

    def __init__(self) -> None:
        """Create an empty ledger."""
        self._seen: set[str] = set()
        self._by_key: dict[RowKey, dict[int, list[Update]]] = defaultdict(
            lambda: defaultdict(list)
        )

    @property
    def applied(self) -> int:
        """Return the number of unique updates applied."""
        return len(self._seen)

    def apply(self, update: Update) -> bool:
        """Apply one update; return ``False`` when it was already applied."""
        if update.update_id in self._seen:
            return False

        self._seen.add(update.update_id)
        self._by_key[update.key][update.sequence].append(update)
        return True

    def apply_all(self, updates: Iterable[Update]) -> int:
        """Apply several updates; return how many were new."""
        return sum(1 for update in updates if self.apply(update))

    def _resolved(self) -> dict[RowKey, Update]:
        resolved: dict[RowKey, Update] = {}
        for key, sequences in self._by_key.items():
            latest = max(sequences)
            entries = sequences[latest]
            classifications = [update for update in entries if update.op == "classify"]
            if classifications:
                resolved[key] = classifications[-1]
        return resolved

    def counts(self) -> dict[str, int]:
        """Return the accounting counts produced by applied updates."""
        counts = dict.fromkeys(BUCKETS, 0)
        for update in self._resolved().values():
            counts[update.bucket] += 1
        return counts

    def pending_review(self) -> int:
        """Return the pending-review total produced by applied updates."""
        counts = self.counts()
        return sum(counts[bucket] for bucket in PENDING_BUCKETS)

    def reconciliation(self) -> dict[str, Any]:
        """Return a reconciliation report for the applied update stream."""
        counts = self.counts()
        resolved = self._resolved()
        total = sum(counts.values())
        return {
            "counts": counts,
            "pending_review": self.pending_review(),
            "records": len(resolved),
            "sum": total,
            "balanced": total == len(resolved),
        }

    def record(self, source: str, day: str, record_no: str) -> Update | None:
        """Return the resolved update for one record, if the stream has one."""
        return self._resolved().get((source, day, str(record_no)))


def _config_from_mapping(event: Mapping[str, Any]) -> RuleConfig:
    source = _required_text(event, "source")
    revision = _required_int(event, "revision")
    missing_values = _optional_sequence(
        event,
        "missing_values",
        "missing_tokens",
        "null_tokens",
        default=("-", ""),
    )
    review_values = _optional_sequence(
        event,
        "review_values",
        "review_tokens",
        "raw_review_values",
        default=(),
    )
    effective_from = _optional_text(
        event, "effective_from", "from_day", "effective_day"
    )
    effective_to = _optional_text(event, "effective_to", "to_day")
    note = _optional_text(event, "note", "notes") or ""
    return RuleConfig(
        source=source,
        revision=revision,
        missing_values=tuple(missing_values),
        review_values=tuple(review_values),
        effective_from=effective_from,
        effective_to=effective_to,
        note=note,
    )


def _row_from_mapping(event: Mapping[str, Any]) -> RowFact:
    return RowFact(
        source=_required_text(event, "source"),
        day=_required_text(event, "day", "data_day"),
        record_no=_required_text(event, "record_no", "record_id", "number"),
        revision=_required_int(event, "revision", "row_revision"),
        primary=_optional_text(event, "primary", "primary_value", "primary_address"),
        fallback=_optional_text(
            event, "fallback", "fallback_value", "fallback_address"
        ),
    )


def _update_from_mapping(event: Mapping[str, Any]) -> Update:
    return Update(
        update_id=_required_text(event, "update_id"),
        op=_required_text(event, "op"),
        bucket=_required_text(event, "bucket"),
        delta=_required_int(event, "delta"),
        source=_required_text(event, "source"),
        day=_required_text(event, "day"),
        record_no=_required_text(event, "record_no"),
        category=_required_text(event, "category"),
        site=str(event.get("site") or ""),
        domain=str(event.get("domain") or ""),
        suffix=str(event.get("suffix") or ""),
        is_private=bool(event.get("is_private", False)),
        value_used=_optional_text(event, "value_used"),
        config_revision=_optional_int(event, "config_revision"),
        content_revision=_optional_int(event, "content_revision"),
        reason=_required_text(event, "reason"),
        from_bucket=_optional_text(event, "from_bucket"),
        to_bucket=_optional_text(event, "to_bucket"),
        sequence=_required_int(event, "sequence"),
    )


def _event_kind(event: Mapping[str, Any]) -> str:
    kind = event.get("type", event.get("kind"))
    if isinstance(kind, str):
        normalized = kind.strip().casefold()
        if normalized in {"config", "rule", "rules", "policy"}:
            return "config"
        if normalized in {"row", "record", "log", "site"}:
            return "row"
        raise PrecheckError(f"unknown event type {kind!r}")

    if "record_no" in event or "record_id" in event:
        return "row"
    if (
        "missing_values" in event
        or "missing_tokens" in event
        or "effective_from" in event
    ):
        return "config"
    raise PrecheckError("event does not declare a type and cannot be inferred")


def _required_text(event: Mapping[str, Any], *names: str) -> str:
    value = _first(event, names)
    if value is None:
        raise PrecheckError(f"missing required field {names[0]!r}")
    text = str(value).strip()
    if not text:
        raise PrecheckError(f"field {names[0]!r} must not be empty")
    return text


def _required_int(event: Mapping[str, Any], *names: str) -> int:
    value = _first(event, names)
    if value is None:
        raise PrecheckError(f"missing required field {names[0]!r}")
    return _as_int(value, names[0])


def _optional_int(event: Mapping[str, Any], *names: str) -> int | None:
    value = _first(event, names)
    if value is None:
        return None
    return _as_int(value, names[0])


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PrecheckError(f"field {name!r} must be an integer") from exc


def _optional_text(event: Mapping[str, Any], *names: str) -> str | None:
    value = _first(event, names)
    if value is None:
        return None
    return str(value)


def _optional_sequence(
    event: Mapping[str, Any],
    *names: str,
    default: Sequence[str],
) -> Sequence[str]:
    value = _first(event, names)
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        raise PrecheckError(f"field {names[0]!r} must be a list of strings")
    return tuple(str(item) for item in value)


def _first(event: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in event:
            return event[name]
    return None


def _validate_day(day: str | None, field_name: str) -> None:
    if day is None:
        return
    if not isinstance(day, str) or len(day) != 10 or day[4] != "-" or day[7] != "-":
        raise PrecheckError(f"{field_name} must be an ISO date string")
    try:
        datetime.date.fromisoformat(day)
    except ValueError as exc:
        raise PrecheckError(f"{field_name} must be an ISO date string") from exc


def _is_missing(value: str | None, missing_values: Sequence[str]) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    return stripped in {token.strip() for token in missing_values}


def _is_review_text(value: str | None, review_values: Sequence[str]) -> bool:
    if value is None:
        return False
    return value.strip() in {token.strip() for token in review_values}


def _host_candidate(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    normalized = text.replace("。", ".").replace("．", ".").replace("｡", ".")
    host = lenient_netloc(normalized).strip()
    if not host:
        host = normalized
    return host.casefold().rstrip(".。．｡")


def _looks_like_ip(host: str) -> bool:
    if not host:
        return False
    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def _valid_hostname(hostname: str) -> bool:
    candidate = hostname.rstrip(".。．｡")
    if not candidate or len(candidate) > 253:
        return False

    for label in candidate.split("."):
        if not label or len(label) > 63:
            return False
        if label[0] == "-" or label[-1] == "-":
            return False
        if not all(character.isalnum() or character == "-" for character in label):
            return False
    return True


def _normalize_internal_host(host: str) -> str:
    return host.strip().casefold().rstrip(".。．｡")


def _same_accounting(previous: Classification, current: Classification) -> bool:
    return (
        previous.bucket == current.bucket
        and previous.category == current.category
        and previous.site == current.site
        and previous.pending == current.pending
    )


def _history_entry(
    *,
    previous: Classification | None,
    current: Classification,
    sequence: int,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "from_bucket": None if previous is None else previous.bucket,
        "to_bucket": current.bucket,
        "from_category": None if previous is None else previous.category,
        "to_category": current.category,
        "config_revision": current.config_revision,
        "content_revision": current.content_revision,
        "reason": current.reason,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _key_id(key: RowKey) -> str:
    return json.dumps(list(key), ensure_ascii=False)


__all__ = [
    "ARCHIVED",
    "BUCKETS",
    "CATEGORIES",
    "CONFLICT",
    "Classification",
    "FALLBACK",
    "INTERNAL",
    "INVALID",
    "IP",
    "MISSING",
    "PENDING",
    "PENDING_BUCKETS",
    "PRIMARY",
    "PrecheckError",
    "REVIEW_TEXT",
    "RowFact",
    "RuleConfig",
    "SUFFIX_ONLY",
    "SiteLogPrecheck",
    "Update",
    "UpdateLedger",
]
