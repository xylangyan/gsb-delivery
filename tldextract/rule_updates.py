"""Compatibility-aware shared cache for independently published rule lists.

A suffix rule source can be split into two parts that two pipelines publish
independently: the public (ICANN) rules and the private (hosted) rules. Either
part can arrive first, and an older response can finish after a newer one has
already been published.

Every response is validated and stored as an immutable candidate named by its
content hash. The published pair lives in ``manifest.json`` and is replaced with
a single atomic rename. Readers follow only the objects referenced by the
manifest, so an interrupted, failed, or rejected write cannot expose a partial
or incompatible pair.

A private candidate may declare the minimum public revision it needs, for
example ``// REQUIRES-PUBLIC: >=P2``. A complete candidate with no compatible
public revision yet stays *pending pairing*; it is not invalid data and it does
not replace the last compatible pair.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from filelock import FileLock

from .cache import DiskCache

LOG = logging.getLogger(__name__)

_PRIVATE_BEGIN = "===BEGIN PRIVATE DOMAINS==="
_PRIVATE_END = "===END PRIVATE DOMAINS==="
_PUBLIC_END = "===END ICANN DOMAINS==="

_METADATA_RE = re.compile(
    r"^\s*//\s*(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*?)\s*$"
)
_DATE_RE = re.compile(
    r"(?P<year>\d{4})[-_](?P<month>\d{2})[-_](?P<day>\d{2})"
    r"(?:[T _-](?P<hour>\d{2})[-_:](?P<minute>\d{2})[-_:](?P<second>\d{2}))?"
)
_NUMBER_RE = re.compile(r"(?P<number>\d+)\s*$")
_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<operator>>=|<=|==|>|<|=)?\s*(?P<version>[^\s]+)\s*$"
)

_MANIFEST_FORMAT = 1
_OBJECT_FORMAT = 1
DEFAULT_NAMESPACE = "default"

_VERSION_KEYS = ("VERSION", "UPSTREAM-VERSION", "UPSTREAM", "REVISION")
_COMPONENT_KEYS = ("COMPONENT", "PART", "KIND")
_REQUIRES_PUBLIC_KEYS = (
    "REQUIRES-PUBLIC",
    "REQUIRES-PUBLIC-VERSION",
    "MIN-PUBLIC",
    "COMPATIBLE-PUBLIC",
)


class InvalidRuleListError(ValueError):
    """A submitted response is not a usable rule list."""


class Component(str, Enum):
    """The two independently published halves of a suffix rule source."""

    PUBLIC = "public"
    PRIVATE = "private"


class UpdateStatus(str, Enum):
    """Outcome of staging or publishing a rule candidate."""

    STAGED = "staged"
    ACTIVE = "active"
    ALREADY_ACTIVE = "already_active"
    ALREADY_USED = "already_used"
    PENDING_PAIRING = "pending_pairing"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    SAVE_FAILED = "save_failed"


@dataclass(frozen=True)
class Compatibility:
    """A private list's requirement on the public list."""

    version: str
    sequence: int
    operator: str = ">="

    def accepts(self, public_sequence: int | None) -> bool:
        """Return whether the given public sequence satisfies this requirement."""
        if public_sequence is None:
            return False
        if self.operator == ">=":
            return public_sequence >= self.sequence
        if self.operator == ">":
            return public_sequence > self.sequence
        if self.operator in ("=", "=="):
            return public_sequence == self.sequence
        if self.operator == "<=":
            return public_sequence <= self.sequence
        if self.operator == "<":
            return public_sequence < self.sequence
        return False

    def to_json(self) -> dict[str, Any]:
        """Serialize the requirement."""
        return {
            "version": self.version,
            "sequence": self.sequence,
            "operator": self.operator,
        }

    @classmethod
    def from_json(cls, value: object) -> Compatibility | None:
        """Deserialize a requirement, returning ``None`` if there is none."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("compatibility requirement must be an object")
        raw = cast(dict[str, Any], value)
        version = raw.get("version")
        sequence = raw.get("sequence")
        operator = raw.get("operator", ">=")
        if not isinstance(version, str) or not version:
            raise ValueError("compatibility version is invalid")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("compatibility sequence is invalid")
        if not isinstance(operator, str):
            raise ValueError("compatibility operator is invalid")
        return cls(version=version, sequence=sequence, operator=operator)


@dataclass(frozen=True)
class RuleCandidate:
    """An immutable, validated rule list candidate."""

    component: Component
    version: str
    sequence: int
    rules: tuple[str, ...]
    compatibility: Compatibility | None = None
    content_sha256: str = ""

    @property
    def is_public(self) -> bool:
        """Whether this candidate supplies public rules."""
        return self.component is Component.PUBLIC

    def is_compatible_with(self, public_sequence: int | None) -> bool:
        """Whether this candidate may be paired with the given public rules."""
        if self.is_public or self.compatibility is None:
            return True
        return self.compatibility.accepts(public_sequence)

    def to_json(self) -> dict[str, Any]:
        """Serialize the candidate for storage."""
        return {
            "format": _OBJECT_FORMAT,
            "component": self.component.value,
            "version": self.version,
            "sequence": self.sequence,
            "rules": list(self.rules),
            "compatibility": (
                self.compatibility.to_json() if self.compatibility else None
            ),
        }

    def to_bytes(self) -> bytes:
        """Serialize the candidate deterministically."""
        return json.dumps(
            self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes, expected_sha256: str) -> RuleCandidate:
        """Deserialize a candidate and verify its content hash."""
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("candidate content hash does not match its name")
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("candidate must be a JSON object")
        data = cast(dict[str, Any], raw)
        if data.get("format") != _OBJECT_FORMAT:
            raise ValueError("unsupported candidate format")
        component = data.get("component")
        version = data.get("version")
        sequence = data.get("sequence")
        rules = data.get("rules")
        if component not in (Component.PUBLIC.value, Component.PRIVATE.value):
            raise ValueError("candidate component is invalid")
        if not isinstance(version, str) or not version:
            raise ValueError("candidate version is invalid")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("candidate sequence is invalid")
        if not isinstance(rules, list) or not all(
            isinstance(rule, str) for rule in rules
        ):
            raise ValueError("candidate rules are invalid")
        compatibility = Compatibility.from_json(data.get("compatibility"))
        if compatibility is not None and component != Component.PRIVATE.value:
            raise ValueError("public rules cannot require a public version")
        return cls(
            component=Component(component),
            version=version,
            sequence=sequence,
            rules=tuple(cast(list[str], rules)),
            compatibility=compatibility,
            content_sha256=expected_sha256,
        )


@dataclass(frozen=True)
class ActiveRules:
    """The published, mutually compatible rule pair."""

    public_version: str
    private_version: str | None
    public_tlds: list[str]
    private_tlds: list[str]
    generation: int
    manifest_sha256: str

    def suffix_lists(
        self, include_psl_private_domains: bool = False
    ) -> tuple[list[str], list[str]]:
        """Return the rules appropriate for the requested lookup mode."""
        if include_psl_private_domains:
            return list(self.public_tlds), list(self.private_tlds)
        return list(self.public_tlds), []


@dataclass(frozen=True)
class UpdateReceipt:
    """Result of submitting or staging a rule candidate."""

    status: UpdateStatus
    component: Component | None = None
    version: str | None = None
    sequence: int | None = None
    content_sha256: str | None = None
    detail: str = ""
    active_public: str | None = None
    active_private: str | None = None
    generation: int = 0
    changed: bool = False
    pending: tuple[tuple[str, str], ...] = ()

    @property
    def accepted(self) -> bool:
        """Whether the candidate was stored as a valid rule list."""
        return self.status not in (
            UpdateStatus.REJECTED,
            UpdateStatus.SAVE_FAILED,
            UpdateStatus.CONFLICT,
        )


@dataclass(frozen=True)
class PublishReceipt:
    """Result of reconciling all stored candidates."""

    changed: bool
    generation: int
    active_public: str | None
    active_private: str | None
    activated: tuple[tuple[str, str], ...]
    pending: tuple[tuple[str, str], ...]
    detail: str = ""


@dataclass(frozen=True)
class _ManifestEntry:
    content_sha256: str
    version: str
    sequence: int
    compatibility: Compatibility | None

    def to_json(self) -> dict[str, Any]:
        return {
            "sha256": self.content_sha256,
            "version": self.version,
            "sequence": self.sequence,
            "compatibility": (
                self.compatibility.to_json() if self.compatibility else None
            ),
        }

    @classmethod
    def from_candidate(cls, candidate: RuleCandidate) -> _ManifestEntry:
        return cls(
            content_sha256=candidate.content_sha256,
            version=candidate.version,
            sequence=candidate.sequence,
            compatibility=candidate.compatibility,
        )

    @classmethod
    def from_json(cls, value: object) -> _ManifestEntry:
        if not isinstance(value, dict):
            raise ValueError("manifest entry must be an object")
        raw = cast(dict[str, Any], value)
        sha = raw.get("sha256")
        version = raw.get("version")
        sequence = raw.get("sequence")
        if not isinstance(sha, str) or not sha:
            raise ValueError("manifest entry hash is invalid")
        if not isinstance(version, str) or not version:
            raise ValueError("manifest entry version is invalid")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("manifest entry sequence is invalid")
        return cls(
            content_sha256=sha,
            version=version,
            sequence=sequence,
            compatibility=Compatibility.from_json(raw.get("compatibility")),
        )


@dataclass(frozen=True)
class _Manifest:
    generation: int
    public: _ManifestEntry
    private: _ManifestEntry | None
    used: dict[str, tuple[_ManifestEntry, ...]]
    sha256: str = ""

    def to_bytes(self) -> bytes:
        used = {
            name: [entry.to_json() for entry in entries]
            for name, entries in sorted(self.used.items())
        }
        data = {
            "format": _MANIFEST_FORMAT,
            "generation": self.generation,
            "public": self.public.to_json(),
            "private": self.private.to_json() if self.private else None,
            "used": used,
        }
        return json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


@dataclass(frozen=True)
class _ParsedDocument:
    metadata: dict[str, str]
    public_rules: tuple[str, ...]
    private_rules: tuple[str, ...]
    has_private_section: bool


def _parse_document(text: str) -> _ParsedDocument:
    state = _ParserState()
    for line_number, original_line in enumerate(text.splitlines(), 1):
        line = original_line.strip()
        if line_number == 1:
            line = line.lstrip("﻿")
        if not line:
            continue
        if line.startswith("//"):
            state.consume_comment(line)
            continue
        state.consume_rule(line, line_number, original_line)

    if state.private_begin and not state.private_end:
        raise InvalidRuleListError("private section is not terminated")
    return _ParsedDocument(
        metadata=state.metadata,
        public_rules=tuple(_dedupe(state.public_rules)),
        private_rules=tuple(_dedupe(state.private_rules)),
        has_private_section=state.private_begin,
    )


class _ParserState:
    """Mutable parse state for one suffix rule document."""

    def __init__(self) -> None:
        self.metadata: dict[str, str] = {}
        self.public_rules: list[str] = []
        self.private_rules: list[str] = []
        self.section = "public"
        self.private_begin = False
        self.private_end = False

    def consume_comment(self, line: str) -> None:
        """Record section markers and metadata from a comment line."""
        upper = line.upper()
        if _PRIVATE_BEGIN in upper:
            if self.private_begin:
                raise InvalidRuleListError("duplicate private section marker")
            self.private_begin = True
            self.section = "private"
        elif _PRIVATE_END in upper:
            if not self.private_begin or self.private_end:
                raise InvalidRuleListError("private section end without a begin")
            self.private_end = True
            self.section = "after_private"
        elif _PUBLIC_END in upper:
            self.section = "after_public"
        else:
            match = _METADATA_RE.match(line)
            if match:
                self.metadata[match.group("key").upper()] = match.group("value")

    def consume_rule(self, line: str, line_number: int, original: str) -> None:
        """Record a rule line in the current section."""
        if self.section in ("after_public", "after_private"):
            raise InvalidRuleListError(
                f"rule outside a section on line {line_number}: {original!r}"
            )
        rule = _validate_rule(line, line_number)
        if self.section == "private":
            self.private_rules.append(rule)
        else:
            self.public_rules.append(rule)


def _validate_rule(line: str, line_number: int) -> str:
    body = line[1:] if line.startswith("!") else line
    labels = body.split(".")
    if not labels or any(not label for label in labels):
        raise InvalidRuleListError(f"invalid rule on line {line_number}: {line!r}")
    for index, label in enumerate(labels):
        if label == "*":
            if index != 0:
                raise InvalidRuleListError(
                    f"wildcard must be leftmost on line {line_number}: {line!r}"
                )
            continue
        if not _valid_label(label):
            raise InvalidRuleListError(f"invalid rule on line {line_number}: {line!r}")
    return line


def _valid_label(label: str) -> bool:
    if label.startswith("-") or label.endswith("-"):
        return False
    for char in label:
        if char == "-":
            continue
        if unicodedata.category(char)[0] not in ("L", "N", "M"):
            return False
    return True


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _sequence_from_version(version: str) -> int:
    date_match = _DATE_RE.search(version)
    if date_match is not None:
        parts = [
            date_match.group("year"),
            date_match.group("month"),
            date_match.group("day"),
            date_match.group("hour") or "00",
            date_match.group("minute") or "00",
            date_match.group("second") or "00",
        ]
        return int("".join(parts))
    number_match = _NUMBER_RE.search(version)
    if number_match is not None:
        return int(number_match.group("number"))
    raise InvalidRuleListError(
        f"version {version!r} is not orderable; pass an explicit sequence"
    )


def _parse_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidRuleListError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise InvalidRuleListError(f"{field} must be an integer") from exc
    raise InvalidRuleListError(f"{field} must be an integer")


def _metadata_value(metadata: dict[str, str], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


def _parse_requirement(
    value: str | None, *, sequence: int | None
) -> Compatibility | None:
    if value is None:
        return None
    match = _REQUIREMENT_RE.match(value)
    if match is None:
        raise InvalidRuleListError(f"invalid public requirement: {value!r}")
    operator = match.group("operator") or ">="
    version = match.group("version")
    requirement_sequence = (
        sequence if sequence is not None else _sequence_from_version(version)
    )
    return Compatibility(
        version=version, sequence=requirement_sequence, operator=operator
    )


def _candidate_from_text(
    component: Component,
    text: str,
    *,
    version: str | None,
    sequence: int | None,
    requires_public: str | None,
    requires_public_sequence: int | None,
) -> RuleCandidate:
    if not text or not text.strip():
        raise InvalidRuleListError("response is empty")

    document = _parse_document(text)
    _check_declared_component(component, document)

    version, sequence = _resolve_identity(document, version=version, sequence=sequence)

    if component is Component.PUBLIC:
        if requires_public is not None or requires_public_sequence is not None:
            raise InvalidRuleListError(
                "public rules cannot require a specific public version"
            )
        rules = list(document.public_rules)
        if not rules:
            raise InvalidRuleListError("response contains no public rules")
        compatibility = None
    else:
        requires_public, requires_public_sequence = _resolve_requirement_metadata(
            document, requires_public, requires_public_sequence
        )
        rules = (
            list(document.private_rules)
            if document.has_private_section
            else list(document.public_rules)
        )
        if not rules:
            raise InvalidRuleListError("response contains no private rules")
        compatibility = _parse_requirement(
            requires_public, sequence=requires_public_sequence
        )

    raw_candidate = RuleCandidate(
        component=component,
        version=version,
        sequence=sequence,
        rules=tuple(rules),
        compatibility=compatibility,
    )
    payload = raw_candidate.to_bytes()
    return RuleCandidate(
        component=raw_candidate.component,
        version=raw_candidate.version,
        sequence=raw_candidate.sequence,
        rules=raw_candidate.rules,
        compatibility=raw_candidate.compatibility,
        content_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _check_declared_component(component: Component, document: _ParsedDocument) -> None:
    declared_component = _metadata_value(document.metadata, _COMPONENT_KEYS)
    if declared_component is None:
        return
    declared = declared_component.strip().lower()
    if declared not in (Component.PUBLIC.value, Component.PRIVATE.value):
        raise InvalidRuleListError(
            f"invalid component declaration: {declared_component!r}"
        )
    if declared != component.value:
        raise InvalidRuleListError(
            f"response declares component {declared!r}, not {component.value!r}"
        )


def _resolve_identity(
    document: _ParsedDocument,
    *,
    version: str | None,
    sequence: int | None,
) -> tuple[str, int]:
    if version is None:
        version = _metadata_value(document.metadata, _VERSION_KEYS)
    if sequence is None:
        sequence_value = document.metadata.get("SEQUENCE")
        if sequence_value is not None:
            sequence = _parse_integer(sequence_value, field="sequence")
    if version is None:
        if sequence is None:
            raise InvalidRuleListError("missing upstream version identifier")
        version = f"sequence-{sequence}"
    version = version.strip()
    if not version:
        raise InvalidRuleListError("missing upstream version identifier")
    if sequence is None:
        sequence = _sequence_from_version(version)
    return version, sequence


def _resolve_requirement_metadata(
    document: _ParsedDocument,
    requires_public: str | None,
    requires_public_sequence: int | None,
) -> tuple[str | None, int | None]:
    if requires_public is None:
        requires_public = _metadata_value(document.metadata, _REQUIRES_PUBLIC_KEYS)
    if requires_public_sequence is None:
        requirement_value = document.metadata.get("REQUIRES-PUBLIC-SEQUENCE")
        if requirement_value is not None:
            requires_public_sequence = _parse_integer(
                requirement_value, field="requires_public_sequence"
            )
    return requires_public, requires_public_sequence


def candidate_from_text(
    component: Component | str,
    text: str,
    *,
    version: str | None = None,
    sequence: int | None = None,
    requires_public: str | None = None,
    requires_public_sequence: int | None = None,
) -> RuleCandidate:
    """Validate and parse a response without storing it.

    Metadata comments such as ``// VERSION: P2``, ``// COMPONENT: public`` and
    ``// REQUIRES-PUBLIC: >=P2`` supply values not passed explicitly.
    """
    return _candidate_from_text(
        Component(component),
        text,
        version=version,
        sequence=sequence,
        requires_public=requires_public,
        requires_public_sequence=requires_public_sequence,
    )


def _safe_namespace(namespace: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", namespace).strip(".-")
    return cleaned or DEFAULT_NAMESPACE


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write bytes atomically, syncing the file and its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _same_pair(
    manifest: _Manifest,
    public: _ManifestEntry,
    private: _ManifestEntry | None,
) -> bool:
    """Whether a manifest already references exactly the given pair."""
    return public.content_sha256 == manifest.public.content_sha256 and (
        private.content_sha256 if private else None
    ) == (manifest.private.content_sha256 if manifest.private else None)


def _newest_unique(candidates: Iterable[RuleCandidate]) -> list[RuleCandidate]:
    """Return candidates newest first, one per distinct effective rule set."""
    by_content: dict[str, RuleCandidate] = {}
    for candidate in candidates:
        existing = by_content.get(candidate.content_sha256)
        if existing is None or candidate.sequence > existing.sequence:
            by_content[candidate.content_sha256] = candidate
    return sorted(
        by_content.values(),
        key=lambda candidate: (candidate.sequence, candidate.version),
        reverse=True,
    )


class SharedRuleCache:
    """A shared cache that publishes only mutually compatible rule pairs.

    Candidates are immutable objects named by content hash. The published state
    is ``manifest.json``, replaced with one atomic rename. Readers use only the
    objects the manifest references, falling back to the previous manifest if
    the current one is not fully readable.
    """

    def __init__(
        self,
        cache: DiskCache | str | None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        lock_timeout: int = 20,
    ) -> None:
        """Wrap a :class:`~tldextract.cache.DiskCache` or a cache directory."""
        self._disk_cache = cache if isinstance(cache, DiskCache) else DiskCache(cache)
        self.enabled = self._disk_cache.enabled
        self.lock_timeout = lock_timeout
        self.namespace = _safe_namespace(namespace)
        self._root = (
            Path(self._disk_cache.cache_dir, "shared-suffix-rules", self.namespace)
            if self.enabled
            else None
        )

    @property
    def root(self) -> Path | None:
        """The directory backing this store, if caching is enabled."""
        return self._root

    @property
    def _objects_dir(self) -> Path:
        assert self._root is not None
        return self._root / "objects"

    @property
    def _receipts_dir(self) -> Path:
        assert self._root is not None
        return self._root / "receipts"

    @property
    def _manifest_path(self) -> Path:
        assert self._root is not None
        return self._root / "manifest.json"

    @property
    def _backup_path(self) -> Path:
        assert self._root is not None
        return self._root / "manifest.backup.json"

    @property
    def _lock_path(self) -> Path:
        assert self._root is not None
        return self._root / ".publish.lock"

    def stage_public(
        self,
        text: str,
        *,
        version: str | None = None,
        sequence: int | None = None,
    ) -> UpdateReceipt:
        """Validate and store a public list without publishing it."""
        return self._submit(
            Component.PUBLIC,
            text,
            version=version,
            sequence=sequence,
            requires_public=None,
            requires_public_sequence=None,
            publish=False,
        )

    def stage_private(
        self,
        text: str,
        *,
        version: str | None = None,
        sequence: int | None = None,
        requires_public: str | None = None,
        requires_public_sequence: int | None = None,
    ) -> UpdateReceipt:
        """Validate and store a private list without publishing it."""
        return self._submit(
            Component.PRIVATE,
            text,
            version=version,
            sequence=sequence,
            requires_public=requires_public,
            requires_public_sequence=requires_public_sequence,
            publish=False,
        )

    def submit_public(
        self,
        text: str,
        *,
        version: str | None = None,
        sequence: int | None = None,
    ) -> UpdateReceipt:
        """Store a public list and reconcile the published pair."""
        return self._submit(
            Component.PUBLIC,
            text,
            version=version,
            sequence=sequence,
            requires_public=None,
            requires_public_sequence=None,
            publish=True,
        )

    def submit_private(
        self,
        text: str,
        *,
        version: str | None = None,
        sequence: int | None = None,
        requires_public: str | None = None,
        requires_public_sequence: int | None = None,
    ) -> UpdateReceipt:
        """Store a private list and reconcile the published pair."""
        return self._submit(
            Component.PRIVATE,
            text,
            version=version,
            sequence=sequence,
            requires_public=requires_public,
            requires_public_sequence=requires_public_sequence,
            publish=True,
        )

    def clear(self) -> None:
        """Remove all published rules and stored candidates for this namespace."""
        if self._root is None:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path), timeout=self.lock_timeout):
            paths = [
                self._manifest_path,
                self._backup_path,
                *sorted(self._objects_dir.glob("*.json")),
                *sorted(self._receipts_dir.glob("*.json")),
            ]
            for path in paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    LOG.debug("unable to remove %s: %s", path, exc)

    def _submit(
        self,
        component: Component,
        text: str,
        *,
        version: str | None,
        sequence: int | None,
        requires_public: str | None,
        requires_public_sequence: int | None,
        publish: bool,
    ) -> UpdateReceipt:
        if self._root is None:
            return UpdateReceipt(
                status=UpdateStatus.SAVE_FAILED,
                component=component,
                version=version,
                detail="shared rule cache is disabled because caching is disabled",
            )

        try:
            candidate = _candidate_from_text(
                component,
                text,
                version=version,
                sequence=sequence,
                requires_public=requires_public,
                requires_public_sequence=requires_public_sequence,
            )
        except (InvalidRuleListError, ValueError) as exc:
            receipt = UpdateReceipt(
                status=UpdateStatus.REJECTED,
                component=component,
                version=version,
                sequence=sequence,
                detail=str(exc),
            )
            self._record_receipt(receipt)
            return receipt

        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with FileLock(str(self._lock_path), timeout=self.lock_timeout):
                return self._submit_locked(candidate, publish=publish)
        except OSError as exc:
            receipt = self._save_failed(candidate, self._safe_manifest(), exc)
            self._record_receipt(receipt)
            return receipt

    def _submit_locked(
        self, candidate: RuleCandidate, *, publish: bool
    ) -> UpdateReceipt:
        manifest = self._load_manifest()
        conflict = self._find_conflict(candidate)
        if conflict is not None:
            receipt = UpdateReceipt(
                status=UpdateStatus.CONFLICT,
                component=candidate.component,
                version=candidate.version,
                sequence=candidate.sequence,
                content_sha256=candidate.content_sha256,
                detail=conflict,
                active_public=manifest.public.version if manifest else None,
                active_private=(
                    manifest.private.version if manifest and manifest.private else None
                ),
                generation=manifest.generation if manifest else 0,
            )
            self._record_receipt(receipt)
            return receipt

        try:
            self._store_candidate(candidate)
        except OSError as exc:
            receipt = self._save_failed(candidate, manifest, exc)
            self._record_receipt(receipt)
            return receipt

        if not publish:
            receipt = UpdateReceipt(
                status=UpdateStatus.STAGED,
                component=candidate.component,
                version=candidate.version,
                sequence=candidate.sequence,
                content_sha256=candidate.content_sha256,
                detail="candidate stored; publication not requested",
                active_public=manifest.public.version if manifest else None,
                active_private=(
                    manifest.private.version if manifest and manifest.private else None
                ),
                generation=manifest.generation if manifest else 0,
            )
            self._record_receipt(receipt)
            return receipt

        return self._publish_candidate_locked(candidate, manifest)

    def _save_failed(
        self,
        candidate: RuleCandidate,
        manifest: _Manifest | None,
        exc: OSError,
    ) -> UpdateReceipt:
        return UpdateReceipt(
            status=UpdateStatus.SAVE_FAILED,
            component=candidate.component,
            version=candidate.version,
            sequence=candidate.sequence,
            content_sha256=candidate.content_sha256,
            detail=f"unable to store or publish candidate: {exc}",
            active_public=manifest.public.version if manifest else None,
            active_private=(
                manifest.private.version if manifest and manifest.private else None
            ),
            generation=manifest.generation if manifest else 0,
        )

    def _safe_manifest(self) -> _Manifest | None:
        try:
            return self._load_manifest()
        except OSError:
            return None

    def _find_conflict(self, candidate: RuleCandidate) -> str | None:
        for existing in self._load_candidates():
            if (
                existing.component is candidate.component
                and existing.sequence == candidate.sequence
                and existing.content_sha256 != candidate.content_sha256
            ):
                return (
                    f"version sequence {candidate.sequence} is already stored "
                    "with different content"
                )
        return None

    def _store_candidate(self, candidate: RuleCandidate) -> None:
        path = self._objects_dir / f"{candidate.content_sha256}.json"
        if path.is_file():
            return
        _atomic_write(path, candidate.to_bytes())

    def _load_candidate(self, content_sha256: str) -> RuleCandidate | None:
        path = self._objects_dir / f"{content_sha256}.json"
        try:
            return RuleCandidate.from_bytes(path.read_bytes(), content_sha256)
        except (OSError, ValueError):
            return None

    def _load_candidates(self) -> list[RuleCandidate]:
        if not self._objects_dir.is_dir():
            return []
        candidates: list[RuleCandidate] = []
        for path in sorted(self._objects_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            candidate = self._load_candidate(path.stem)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _entry_valid(self, entry: _ManifestEntry, expected: Component) -> bool:
        candidate = self._load_candidate(entry.content_sha256)
        return (
            candidate is not None
            and candidate.component is expected
            and candidate.version == entry.version
            and candidate.sequence == entry.sequence
            and candidate.compatibility == entry.compatibility
        )

    def _load_manifest_file(self, path: Path) -> _Manifest | None:
        try:
            payload = path.read_bytes()
            raw = json.loads(payload.decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        data = cast(dict[str, Any], raw)
        if data.get("format") != _MANIFEST_FORMAT:
            return None
        generation = data.get("generation")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            return None
        entries = self._manifest_entries(data)
        if entries is None:
            return None
        public, private = entries
        return _Manifest(
            generation=generation,
            public=public,
            private=private,
            used=self._manifest_used(data),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def _manifest_entries(
        self, data: dict[str, Any]
    ) -> tuple[_ManifestEntry, _ManifestEntry | None] | None:
        try:
            public = _ManifestEntry.from_json(data["public"])
            private = (
                _ManifestEntry.from_json(data["private"])
                if data.get("private") is not None
                else None
            )
        except (KeyError, ValueError):
            return None
        if not self._entry_valid(public, Component.PUBLIC):
            return None
        if private is None:
            return public, None
        if not self._entry_valid(private, Component.PRIVATE):
            return None
        if private.compatibility is not None and not (
            private.compatibility.accepts(public.sequence)
        ):
            return None
        return public, private

    @staticmethod
    def _manifest_used(data: dict[str, Any]) -> dict[str, tuple[_ManifestEntry, ...]]:
        used: dict[str, tuple[_ManifestEntry, ...]] = {}
        raw_used = data.get("used", {})
        if not isinstance(raw_used, dict):
            return used
        for name, entries in cast(dict[str, Any], raw_used).items():
            if name not in (Component.PUBLIC.value, Component.PRIVATE.value):
                continue
            if not isinstance(entries, list):
                used[name] = ()
                continue
            try:
                used[name] = tuple(_ManifestEntry.from_json(entry) for entry in entries)
            except ValueError:
                used[name] = ()
        return used

    def _load_manifest(self) -> _Manifest | None:
        manifest = self._load_manifest_file(self._manifest_path)
        if manifest is not None:
            return manifest
        return self._load_manifest_file(self._backup_path)

    def active(self) -> ActiveRules | None:
        """Return the published compatible pair, if one exists."""
        if self._root is None:
            return None
        manifest = self._load_manifest()
        if manifest is None:
            return None
        public = self._load_candidate(manifest.public.content_sha256)
        private = (
            self._load_candidate(manifest.private.content_sha256)
            if manifest.private is not None
            else None
        )
        if public is None:
            return None
        if manifest.private is not None and private is None:
            return None
        return ActiveRules(
            public_version=manifest.public.version,
            private_version=manifest.private.version if manifest.private else None,
            public_tlds=list(public.rules),
            private_tlds=list(private.rules) if private is not None else [],
            generation=manifest.generation,
            manifest_sha256=manifest.sha256,
        )

    def _publish_candidate_locked(
        self, candidate: RuleCandidate, manifest: _Manifest | None
    ) -> UpdateReceipt:
        new_manifest, changed, pending = self._reconcile(manifest)
        published = manifest
        if changed and new_manifest is not None:
            try:
                self._commit_manifest(manifest, new_manifest)
            except OSError as exc:
                receipt = self._save_failed(candidate, manifest, exc)
                self._record_receipt(receipt)
                return receipt
            published = new_manifest

        status = self._candidate_status(candidate, published, changed)
        receipt = UpdateReceipt(
            status=status,
            component=candidate.component,
            version=candidate.version,
            sequence=candidate.sequence,
            content_sha256=candidate.content_sha256,
            detail=self._status_detail(status),
            active_public=published.public.version if published else None,
            active_private=(
                published.private.version
                if published is not None and published.private is not None
                else None
            ),
            generation=published.generation if published is not None else 0,
            changed=changed,
            pending=pending,
        )
        self._record_receipt(receipt)
        return receipt

    def publish(self) -> PublishReceipt:
        """Reconcile stored candidates and publish the best compatible pair."""
        if self._root is None:
            return PublishReceipt(
                changed=False,
                generation=0,
                active_public=None,
                active_private=None,
                activated=(),
                pending=(),
                detail="shared rule cache is disabled",
            )
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with FileLock(str(self._lock_path), timeout=self.lock_timeout):
                manifest = self._load_manifest()
                new_manifest, changed, pending = self._reconcile(manifest)
                if changed and new_manifest is not None:
                    try:
                        self._commit_manifest(manifest, new_manifest)
                    except OSError as exc:
                        return PublishReceipt(
                            changed=False,
                            generation=manifest.generation if manifest else 0,
                            active_public=manifest.public.version if manifest else None,
                            active_private=(
                                manifest.private.version
                                if manifest and manifest.private
                                else None
                            ),
                            activated=(),
                            pending=pending,
                            detail=f"unable to publish manifest: {exc}",
                        )
                    manifest = new_manifest
                activated: list[tuple[str, str]] = []
                if changed and manifest is not None:
                    activated.append((Component.PUBLIC.value, manifest.public.version))
                    if manifest.private is not None:
                        activated.append(
                            (Component.PRIVATE.value, manifest.private.version)
                        )
                return PublishReceipt(
                    changed=changed,
                    generation=manifest.generation if manifest else 0,
                    active_public=manifest.public.version if manifest else None,
                    active_private=(
                        manifest.private.version
                        if manifest and manifest.private
                        else None
                    ),
                    activated=tuple(activated),
                    pending=pending,
                    detail="published" if changed else "no compatible change",
                )
        except OSError as exc:
            return PublishReceipt(
                changed=False,
                generation=0,
                active_public=None,
                active_private=None,
                activated=(),
                pending=(),
                detail=f"unable to access shared rule cache: {exc}",
            )

    def _reconcile(
        self, manifest: _Manifest | None
    ) -> tuple[_Manifest | None, bool, tuple[tuple[str, str], ...]]:
        candidates = self._load_candidates()
        if manifest is not None:
            candidates.extend(self._active_candidates(manifest))

        public_candidates = _newest_unique(
            candidate for candidate in candidates if candidate.is_public
        )
        private_candidates = _newest_unique(
            candidate for candidate in candidates if not candidate.is_public
        )
        if not public_candidates:
            return manifest, False, ()

        current_public, current_private = self._current_candidates(manifest)
        chosen_public, paired_private = self._select_pair(
            public_candidates,
            private_candidates,
            current_public,
            current_private,
        )
        pending = self._pending_private(
            private_candidates, paired_private, chosen_public
        )

        new_public = _ManifestEntry.from_candidate(chosen_public)
        new_private = (
            _ManifestEntry.from_candidate(paired_private)
            if paired_private is not None
            else None
        )
        if manifest is not None and _same_pair(manifest, new_public, new_private):
            return manifest, False, pending

        used = (
            {name: list(entries) for name, entries in manifest.used.items()}
            if manifest is not None
            else {}
        )
        for name, entry in (
            (Component.PUBLIC.value, new_public),
            (Component.PRIVATE.value, new_private),
        ):
            if entry is None:
                continue
            entries = used.setdefault(name, [])
            if not any(
                existing.content_sha256 == entry.content_sha256
                and existing.version == entry.version
                for existing in entries
            ):
                entries.append(entry)
        return (
            _Manifest(
                generation=(manifest.generation + 1) if manifest else 1,
                public=new_public,
                private=new_private,
                used={name: tuple(entries) for name, entries in used.items()},
            ),
            True,
            pending,
        )

    def _current_candidates(
        self, manifest: _Manifest | None
    ) -> tuple[RuleCandidate | None, RuleCandidate | None]:
        if manifest is None:
            return None, None
        public = self._load_candidate(manifest.public.content_sha256)
        private = (
            self._load_candidate(manifest.private.content_sha256)
            if manifest.private is not None
            else None
        )
        return public, private

    @staticmethod
    def _select_pair(
        public_candidates: list[RuleCandidate],
        private_candidates: list[RuleCandidate],
        current_public: RuleCandidate | None,
        current_private: RuleCandidate | None,
    ) -> tuple[RuleCandidate, RuleCandidate | None]:
        chosen_public = public_candidates[0]
        if (
            current_public is not None
            and chosen_public.sequence < current_public.sequence
        ):
            chosen_public = current_public

        paired_private: RuleCandidate | None = None
        for private in private_candidates:
            if private.is_compatible_with(chosen_public.sequence):
                paired_private = private
                break

        if current_private is not None and current_private.is_compatible_with(
            chosen_public.sequence
        ):
            if (
                paired_private is None
                or paired_private.sequence <= current_private.sequence
            ):
                paired_private = current_private

        # An existing compatible pair is not replaced by an unpaired public view
        # just because the newest public list cannot be paired yet.
        if (
            paired_private is None
            and private_candidates
            and current_public is not None
            and current_private is not None
            and current_private.is_compatible_with(current_public.sequence)
        ):
            return current_public, current_private
        return chosen_public, paired_private

    @staticmethod
    def _pending_private(
        private_candidates: list[RuleCandidate],
        paired_private: RuleCandidate | None,
        chosen_public: RuleCandidate,
    ) -> tuple[tuple[str, str], ...]:
        pending: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for private in private_candidates:
            if private.content_sha256 == (
                paired_private.content_sha256 if paired_private else None
            ):
                continue
            if (
                paired_private is not None
                and private.sequence <= paired_private.sequence
            ):
                continue
            if private.is_compatible_with(chosen_public.sequence):
                continue
            key = (private.component.value, private.version)
            if key not in seen:
                seen.add(key)
                pending.append(key)
        return tuple(pending)

    def _active_candidates(self, manifest: _Manifest) -> list[RuleCandidate]:
        result: list[RuleCandidate] = []
        for entry in (manifest.public, manifest.private):
            if entry is None:
                continue
            candidate = self._load_candidate(entry.content_sha256)
            if candidate is not None and not any(
                existing.content_sha256 == candidate.content_sha256
                for existing in result
            ):
                result.append(candidate)
        return result

    def _commit_manifest(self, previous: _Manifest | None, new: _Manifest) -> None:
        if previous is not None:
            try:
                _atomic_write(self._backup_path, previous.to_bytes())
            except OSError:
                pass
        _atomic_write(self._manifest_path, new.to_bytes())

    def _candidate_status(
        self, candidate: RuleCandidate, manifest: _Manifest | None, changed: bool
    ) -> UpdateStatus:
        if manifest is None:
            return UpdateStatus.PENDING_PAIRING
        if candidate.is_public:
            if manifest.public.content_sha256 == candidate.content_sha256:
                return UpdateStatus.ACTIVE if changed else UpdateStatus.ALREADY_ACTIVE
            if self._was_used(candidate, manifest):
                return UpdateStatus.ALREADY_USED
            if candidate.sequence < manifest.public.sequence:
                return UpdateStatus.SUPERSEDED
            return UpdateStatus.PENDING_PAIRING

        if (
            manifest.private is not None
            and manifest.private.content_sha256 == candidate.content_sha256
        ):
            return UpdateStatus.ACTIVE if changed else UpdateStatus.ALREADY_ACTIVE
        if self._was_used(candidate, manifest):
            return UpdateStatus.ALREADY_USED
        if (
            manifest.private is not None
            and candidate.sequence <= manifest.private.sequence
        ):
            return UpdateStatus.SUPERSEDED
        return UpdateStatus.PENDING_PAIRING

    @staticmethod
    def _was_used(candidate: RuleCandidate, manifest: _Manifest) -> bool:
        return any(
            existing.content_sha256 == candidate.content_sha256
            and existing.version == candidate.version
            for existing in manifest.used.get(candidate.component.value, ())
        )

    @staticmethod
    def _status_detail(status: UpdateStatus) -> str:
        return {
            UpdateStatus.ACTIVE: "candidate is part of the newly published pair",
            UpdateStatus.ALREADY_ACTIVE: "candidate is already published",
            UpdateStatus.ALREADY_USED: "candidate was used by an earlier pair",
            UpdateStatus.SUPERSEDED: "a newer version is already published",
            UpdateStatus.PENDING_PAIRING: (
                "candidate is valid but has no compatible public list yet"
            ),
            UpdateStatus.CONFLICT: (
                "the same version sequence is stored with different content"
            ),
        }.get(status, "candidate is stored but not published")

    def _record_receipt(self, receipt: UpdateReceipt) -> None:
        if self._root is None:
            return
        data = {
            "status": receipt.status.value,
            "component": receipt.component.value if receipt.component else None,
            "version": receipt.version,
            "sequence": receipt.sequence,
            "content_sha256": receipt.content_sha256,
            "detail": receipt.detail,
            "active_public": receipt.active_public,
            "active_private": receipt.active_private,
            "generation": receipt.generation,
            "changed": receipt.changed,
            "pending": [list(item) for item in receipt.pending],
            "accepted": receipt.accepted,
        }
        prefix = receipt.component.value if receipt.component else "store"
        name = f"{prefix}-{receipt.version or 'unknown'}-{uuid.uuid4().hex}.json"
        try:
            _atomic_write(
                self._receipts_dir / name,
                (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
        except OSError:
            LOG.debug("unable to persist rule update receipt", exc_info=True)

    def receipts(self) -> list[dict[str, Any]]:
        """Read persisted receipts in lexical order."""
        if self._root is None or not self._receipts_dir.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for path in sorted(self._receipts_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict):
                result.append(cast(dict[str, Any], raw))
        return result


def read_active_suffix_lists(cache: DiskCache) -> tuple[list[str], list[str]] | None:
    """Return the active rule pair from a cache, if one is published."""
    active = SharedRuleCache(cache).active()
    if active is None:
        return None
    return active.suffix_lists(include_psl_private_domains=True)


__all__ = [
    "ActiveRules",
    "Component",
    "DEFAULT_NAMESPACE",
    "InvalidRuleListError",
    "PublishReceipt",
    "RuleCandidate",
    "SharedRuleCache",
    "UpdateReceipt",
    "UpdateStatus",
    "candidate_from_text",
    "read_active_suffix_lists",
]
