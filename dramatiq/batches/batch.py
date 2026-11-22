# This file is a part of Dramatiq.
#
# Copyright (C) 2017,2018 CLEARTYPE SRL <bogdan@cleartype.io>
#
# Dramatiq is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version.
#
# Dramatiq is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public License
# for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

from typing import Any, Optional

from .backend import BatchStateBackend
from .errors import (
    BatchAlreadyExists,
    BatchNotFound,
    BatchOpen,
    BatchSettled,
    BatchVersionConflict,
    DuplicateObligation,
    ObligationClosed,
    ObligationNotFound,
)

#: The default TTL for batch state, in milliseconds (one week).
DEFAULT_BATCH_TTL = 7 * 24 * 60 * 60 * 1000

#: Obligation lifecycle states.
PENDING = "pending"
COMPLETED = "completed"
CANCELLED = "cancelled"
TERMINATED = "terminated"

TERMINAL_STATES = frozenset({COMPLETED, CANCELLED, TERMINATED})


def _check_version(state: dict[str, Any], expected_version: Optional[int]) -> None:
    if expected_version is not None and state["version"] != expected_version:
        raise BatchVersionConflict(
            f"expected batch version {expected_version} but the batch is at version {state['version']}"
        )


def _obligation(state: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        return state["obligations"][key]
    except KeyError:
        raise ObligationNotFound(f"batch {state['batch_id']!r} has no obligation {key!r}") from None


def _apply_open(batch_id: str, obligations: list[dict[str, Any]], callbacks: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "batch_id": batch_id,
        "version": 1,
        "status": "open",
        "obligations": {},
        "order": [],
        "settlement_callbacks": list(callbacks),
        "settlement": None,
    }
    for obligation in obligations:
        _accept(state, obligation["key"], obligation["site"], detail=obligation.get("detail"))
    return state


def _accept(state: dict[str, Any], key: str, site: str, *, detail: Optional[dict[str, Any]]) -> dict[str, Any]:
    existing = state["obligations"].get(key)
    if existing is not None:
        # A retried registration (e.g. one whose acknowledgement was
        # lost in a broker restart) carries the same key.  It must
        # never materialize into a second obligation.
        if existing["site"] != site:
            raise DuplicateObligation(f"obligation key {key!r} is already registered to site {existing['site']!r}")
        return existing

    record = {
        "key": key,
        "site": site,
        "status": PENDING,
        "range": None,
        "evidence": None,
        "reason": None,
        "late": [],
        "accepted_version": state["version"],
        "resolved_version": None,
        "detail": detail,
    }
    state["obligations"][key] = record
    state["order"].append(key)
    state["version"] += 1
    return record


def _accept_obligation(state, key, site, *, detail=None, expected_version=None):
    if state["status"] == "settled":
        raise BatchSettled(f"batch {state['batch_id']!r} has already been settled")
    _check_version(state, expected_version)
    return _accept(state, key, site, detail=detail)


def _resolve(state, key, status, *, range=None, evidence=None, reason=None):
    record = _obligation(state, key)
    if record["status"] == status:
        return record
    if record["status"] in TERMINAL_STATES:
        raise ObligationClosed(f"obligation {key!r} is already {record['status']}")
    record["status"] = status
    record["range"] = range
    record["evidence"] = evidence
    record["reason"] = reason
    record["resolved_version"] = state["version"] + 1
    state["version"] += 1
    return record


def _require_open(state):
    # A settled batch is frozen: even idempotent-looking mutations are
    # rejected so its settlement document can never change underneath
    # the callbacks it already emitted.
    if state["status"] == "settled":
        raise BatchSettled(f"batch {state['batch_id']!r} has already been settled")


def _complete_obligation(state, key, *, range=None, evidence=None, expected_version=None):
    _require_open(state)
    _check_version(state, expected_version)

    record = _obligation(state, key)
    if record["status"] == COMPLETED:
        return record
    if record["status"] in TERMINAL_STATES:
        # The obligation was cancelled or terminated before this result
        # arrived.  Keep the late material as evidence, but do not turn
        # the old promise into new work.
        return _record_late(state, key, {"range": range, "evidence": evidence})
    return _resolve(state, key, COMPLETED, range=range, evidence=evidence)


def _cancel_obligation(state, key, *, effective_version, reason=None):
    _require_open(state)
    _check_version(state, effective_version)
    return _resolve(state, key, CANCELLED, reason=reason)


def _terminate_obligation(state, key, *, effective_version, range=None, reason=None):
    _require_open(state)
    _check_version(state, effective_version)
    return _resolve(state, key, TERMINATED, range=range, reason=reason)


def _record_late(state, key, material):
    record = _obligation(state, key)
    record["late"].append(material)
    state["version"] += 1
    return record


def _record_late_obligation(state, key, material, *, expected_version=None):
    _require_open(state)
    _check_version(state, expected_version)
    return _record_late(state, key, material)


def _submit_page(state, key, *, range, evidence, discovered):
    record = _obligation(state, key)
    newly_accepted: list[dict[str, Any]] = []

    if record["status"] in (CANCELLED, TERMINATED):
        # The obligation was stopped before this submission arrived.
        # Keep the entire submission (including anything it claims to
        # have discovered) as late material, but never turn the old
        # promise into new obligations.
        material = {"range": range, "evidence": evidence}
        if discovered:
            material["discovered"] = list(discovered)
        _record_late(state, key, material)
        return newly_accepted

    if record["status"] == PENDING:
        _resolve(state, key, COMPLETED, range=range, evidence=evidence)

    for obligation in discovered or []:
        existed = obligation["key"] in state["obligations"]
        _accept(state, obligation["key"], obligation["site"], detail=obligation.get("detail"))
        if not existed:
            newly_accepted.append(obligation)
    return newly_accepted


def _add_settlement_callback(state, message, *, expected_version=None):
    if state["status"] == "settled":
        raise BatchSettled(f"batch {state['batch_id']!r} has already been settled")
    _check_version(state, expected_version)
    state["settlement_callbacks"].append(message)
    state["version"] += 1
    return state


def _settlement_ranges(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    ranges: dict[str, list[dict[str, Any]]] = {}
    for key in state["order"]:
        record = state["obligations"][key]
        ranges.setdefault(record["site"], []).append(
            {
                "key": key,
                "status": record["status"],
                "range": record["range"],
                "late": list(record["late"]),
                "reason": record["reason"],
            }
        )
    return ranges


def _settle(state, *, expected_version=None):
    if state["status"] == "settled":
        return state["settlement"]
    _check_version(state, expected_version)

    pending = [key for key in state["order"] if state["obligations"][key]["status"] == PENDING]
    if pending:
        raise BatchOpen(f"batch {state['batch_id']!r} still has unresolved obligations: {', '.join(pending)}")

    state["version"] += 1
    state["status"] = "settled"
    state["settlement"] = {
        "version": state["version"],
        "ranges": _settlement_ranges(state),
    }
    return state["settlement"]


def _try_settle(state):
    if state["status"] == "settled":
        return None
    pending = [key for key in state["order"] if state["obligations"][key]["status"] == PENDING]
    if pending:
        return None

    state["version"] += 1
    state["status"] = "settled"
    state["settlement"] = {
        "version": state["version"],
        "ranges": _settlement_ranges(state),
    }
    return state["settlement"]


class Batch:
    """A settleable batch of per-site page obligations.

    A batch tracks every page obligation that has been *accepted* into
    it.  Branches that a page might discover later do not block
    settlement until they are registered from a page submission; an
    obligation only leaves the ``pending`` state with explicit
    completion, cancellation or termination evidence.

    State lives in a :class:`BatchStateBackend` and every mutation is
    performed behind a compare-and-swap on the batch's monotonic
    ``version``.  Administrative commands pin the version they were
    issued against with ``expected_version`` / ``effective_version`` so
    a stale command can never clobber a newer registration.

    Parameters:
      batch_id(str): The unique id of the batch.
      backend(BatchStateBackend): The backend storing batch state.
      ttl(int): The TTL applied to every write, in milliseconds.
    """

    def __init__(self, batch_id: str, *, backend: BatchStateBackend, ttl: int = DEFAULT_BATCH_TTL):
        self.batch_id = batch_id
        self.backend = backend
        self.ttl = ttl
        self._state: Optional[dict[str, Any]] = None

    @classmethod
    def open(
        cls,
        batch_id: str,
        *,
        backend: BatchStateBackend,
        obligations: Optional[list[dict[str, Any]]] = None,
        settlement_callbacks: Optional[list[dict[str, Any]]] = None,
        ttl: int = DEFAULT_BATCH_TTL,
    ) -> "Batch":
        """Create a new batch with the initially registered obligations."""
        state = _apply_open(batch_id, obligations or [], settlement_callbacks or [])
        if not backend.create(batch_id, state, ttl):
            raise BatchAlreadyExists(f"batch {batch_id!r} already exists")
        batch = cls(batch_id, backend=backend, ttl=ttl)
        batch._state = state
        return batch

    @property
    def state(self) -> dict[str, Any]:
        """The cached state document; call :meth:`reload` to refresh it."""
        if self._state is None:
            self.reload()
        return self._state  # type: ignore[return-value]

    def reload(self) -> dict[str, Any]:
        """Re-read the batch state from the backend."""
        state = self.backend.read(self.batch_id)
        if state is None:
            raise BatchNotFound(f"unknown batch {self.batch_id!r}")
        self._state = state
        return state

    @property
    def version(self) -> int:
        return self.state["version"]

    @property
    def is_settled(self) -> bool:
        return self.state["status"] == "settled"

    @property
    def can_settle(self) -> bool:
        """Whether every accepted obligation has terminal evidence."""
        return all(record["status"] in TERMINAL_STATES for record in self.state["obligations"].values())

    @property
    def pending_obligations(self) -> list[dict[str, Any]]:
        return [
            self.state["obligations"][key]
            for key in self.state["order"]
            if self.state["obligations"][key]["status"] == PENDING
        ]

    def get_obligation(self, key: str) -> dict[str, Any]:
        return _obligation(self.state, key)

    def _mutate(self, fn) -> dict[str, Any]:
        def transaction(document):
            if document is None:
                raise BatchNotFound(f"unknown batch {self.batch_id!r}")
            # Transition helpers mutate the document in place; their
            # return value is the affected record, not the document.
            fn(document)
            return document

        self._state = self.backend.mutate(self.batch_id, transaction, ttl=self.ttl)
        return self._state

    def accept_obligation(
        self,
        key: str,
        site: str,
        *,
        detail: Optional[dict[str, Any]] = None,
        expected_version: Optional[int] = None,
    ) -> dict[str, Any]:
        """Register a page obligation discovered from a page submission.

        Re-accepting the same ``key`` for the same site is an idempotent
        no-op, so a registration whose acknowledgement was lost can be
        retried safely.  Reusing a key for another site raises
        :class:`DuplicateObligation`.
        """
        self._mutate(
            lambda document: _accept_obligation(document, key, site, detail=detail, expected_version=expected_version)
        )
        return self.get_obligation(key)

    def complete_obligation(
        self,
        key: str,
        *,
        range: Optional[dict[str, Any]] = None,
        evidence: Optional[dict[str, Any]] = None,
        expected_version: Optional[int] = None,
    ) -> dict[str, Any]:
        """Mark an obligation complete with the page range it covered.

        If the obligation was already cancelled or terminated, the
        material is stored as late evidence and the obligation's
        terminal status is left untouched.
        """
        self._mutate(
            lambda document: _complete_obligation(
                document, key, range=range, evidence=evidence, expected_version=expected_version
            )
        )
        return self.get_obligation(key)

    def submit_page(
        self,
        key: str,
        *,
        range: Optional[dict[str, Any]] = None,
        evidence: Optional[dict[str, Any]] = None,
        discovered: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        """Atomically complete a page and accept the branches it discovered.

        Unknown branches only become obligations at page submission:
        each item in ``discovered`` is an ``{"key", "site", "detail"}``
        dict and is accepted idempotently.  The obligations newly
        accepted by this submission are returned so the caller can
        enqueue their work messages; already-known keys are not
        returned.
        """
        newly_accepted: list[dict[str, Any]] = []

        def transaction(document):
            newly_accepted.clear()
            newly_accepted.extend(
                _submit_page(document, key, range=range, evidence=evidence, discovered=discovered)
            )
            return document

        self._mutate(transaction)
        return newly_accepted

    def cancel_obligation(self, key: str, *, effective_version: int, reason: Optional[str] = None) -> dict[str, Any]:
        """Cancel an obligation against an explicit effective batch version.

        A result that arrives for a cancelled obligation is kept as late
        material instead of reviving the obligation.
        """
        self._mutate(
            lambda document: _cancel_obligation(document, key, effective_version=effective_version, reason=reason)
        )
        return self.get_obligation(key)

    def terminate_obligation(
        self,
        key: str,
        *,
        effective_version: int,
        range: Optional[dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        """Terminate further crawling for an obligation and record its
        final range.  Requires the batch version the stop command was
        issued against.
        """
        self._mutate(
            lambda document: _terminate_obligation(
                document, key, effective_version=effective_version, range=range, reason=reason
            )
        )
        return self.get_obligation(key)

    def record_late(
        self,
        key: str,
        material: dict[str, Any],
        *,
        expected_version: Optional[int] = None,
    ) -> dict[str, Any]:
        """Attach late material to an obligation without changing its
        terminal status and without creating new work.
        """
        self._mutate(
            lambda document: _record_late_obligation(document, key, material, expected_version=expected_version)
        )
        return self.get_obligation(key)

    def add_settlement_callback(self, message, *, expected_version: Optional[int] = None) -> None:
        """Register a message to enqueue once, when the batch settles.

        Fixed-group callbacks are unrelated and keep working without
        being registered here.
        """
        self._mutate(
            lambda document: _add_settlement_callback(
                document, message.asdict(), expected_version=expected_version
            )
        )

    def settle(self, *, expected_version: Optional[int] = None) -> dict[str, Any]:
        """Settle the batch and return the settlement document.

        Raises :class:`BatchOpen` while any accepted obligation is still
        pending.  Settling twice returns the existing settlement.
        """
        state = self._mutate(lambda document: _settle(document, expected_version=expected_version))
        return state["settlement"]

    def try_settle(self) -> Optional[dict[str, Any]]:
        """Settle the batch when every obligation has terminal evidence.

        Returns:
          The settlement document when this call performed the
          settlement, or ``None`` while obligations are still pending
          or when the batch was already settled.  Checking for ``None``
          keeps settlement callbacks firing exactly once.
        """
        was_settled = self.state["status"] == "settled"
        state = self._mutate(_try_settle)
        if was_settled or state["status"] != "settled":
            return None
        return state["settlement"]
