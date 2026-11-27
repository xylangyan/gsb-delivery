"""
Joint execution (合办执行) for reconciliation work orders.

This module adds a second execution style alongside the regular
`.Executor` flow: a single work order is driven through a series of
time slots in which a supplier issues *originals* (原件) bound to a
ledger (账册). Entries (入口) may be listed on an original when it is
issued, but can only claim (领用) it once their claim rules are
satisfied; signing outcomes may switch the ledger an entry is
effectively bound to, including across collections.

The regular pre/main/post task machinery is untouched: every real
fetch runs through a standard `.Executor`, so pre- and post-tasks
behave exactly as before. One original is fetched at most once, no
matter how many entries claim it.

Key concepts
------------

* `.Ledger` - a book ("账册"). Ledgers carry a name and version, but two
  registrations with the same name/version may still be distinct books,
  and different collections may register against the same book.
* `.Entry` - an entry point ("入口") on the work order. A call without
  an explicit collection runs against the entry's current collection
  configuration; a call with an explicit collection always uses that.
* `.Original` - an immutable roster issued at a time slot against one
  ledger. The roster cannot change after issuance; the original may be
  withdrawn later, but existing signatures keep their provenance.
* `.TimeSlot` - an issuance window defining which ledgers may be used,
  who may be listed, and how many originals the whole order may issue.
* `.ClaimRule` - gates when an entry may claim an original it is listed
  on. Rules may be scoped to one ledger, so the same entry can face
  different conditions on different books.
* `.WorkOrder` - the full spec: entries, ledgers, bindings, outcome
  switches, slots and withdrawals.
* `.JointExecutor` - plans the order (guaranteeing every required entry
  completes on every declared branch, minimizing the worst-case number
  of real fetches) and then executes the branch that actually occurs,
  recording a full `.AuditTrail`.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict, List, Mapping, Optional, Sequence, Tuple,
)

from .collection import Collection
from .config import Config
from .executor import Executor
from .tasks import Task

__all__ = [
    "AuditRecord",
    "AuditTrail",
    "BindingRule",
    "ClaimRule",
    "Entry",
    "GuaranteeViolation",
    "JointExecutor",
    "Ledger",
    "Original",
    "OutcomeRule",
    "ReconError",
    "SettledRule",
    "SlotRule",
    "TimeSlot",
    "WorkOrder",
]


class ReconError(Exception):
    """Base error for joint execution problems."""


class GuaranteeViolation(ReconError):
    """
    Raised when no arrangement can guarantee a required entry.

    The offending entry, the outcome chain that blocks it and the first
    unsatisfied rule are preserved on the exception for reporting.
    """

    def __init__(
        self,
        entry: "Entry",
        outcome_chain: Mapping[str, str],
        rule: "ClaimRule",
        reason: str,
    ) -> None:
        self.entry = entry
        self.outcome_chain = dict(outcome_chain)
        self.rule = rule
        self.reason = reason
        chain = ", ".join(
            "{}={}".format(name, result)
            for name, result in sorted(self.outcome_chain.items())
        ) or "<no outcomes>"
        super().__init__(
            "Required entry {!r} is blocked under [{}]: {} ({})".format(
                entry.name, chain, reason, rule.describe()
            )
        )


class Ledger:
    """
    A book (账册) that originals may be issued against.

    Identity is the explicit ``key`` handed in at registration, not the
    name/version pair: two books may share a name and version, and the
    same book may be registered from more than one collection.
    """

    def __init__(self, key: Any, name: str, version: str) -> None:
        self.key = key
        self.name = name
        self.version = version

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ledger) and other.key == self.key

    def __hash__(self) -> int:
        return hash(("Ledger", self.key))

    def __repr__(self) -> str:
        return "<Ledger {} {!r} v{}>".format(
            self.key, self.name, self.version
        )


class ClaimRule:
    """
    Base claim rule.

    A rule only constrains claims against originals of ``ledger`` when
    that scope is given; unscoped rules apply on every ledger. This lets
    one entry face different conditions on different books.
    """

    def __init__(self, ledger: Optional[Ledger] = None) -> None:
        self.ledger = ledger

    def applies_to(self, original: "Original") -> bool:
        return self.ledger is None or self.ledger == original.ledger

    def check(self, entry: "Entry", state: "_RuntimeState") -> Optional[str]:
        raise NotImplementedError

    def satisfied(
        self, entry: "Entry", state: "_RuntimeState", original: "Original"
    ) -> Tuple[bool, str]:
        if not self.applies_to(original):
            return True, ""
        reason = self.check(entry, state)
        return reason is None, reason or ""

    def describe(self) -> str:
        scope = ""
        if self.ledger is not None:
            scope = " on ledger {}".format(self.ledger.key)
        return self.__class__.__name__ + scope


class SlotRule(ClaimRule):
    """Claim only at (or after) a given time slot."""

    def __init__(
        self,
        slot: str,
        after: bool = False,
        ledger: Optional[Ledger] = None,
    ) -> None:
        super().__init__(ledger=ledger)
        self.slot = slot
        self.after = after

    def check(self, entry: "Entry", state: "_RuntimeState") -> Optional[str]:
        current = state.order.slot_index(state.current_slot)
        target = state.order.slot_index(self.slot)
        ok = current >= target if self.after else current == target
        if ok:
            return None
        return "slot must be {} {!r}".format(
            ">=" if self.after else "==", self.slot
        )

    def describe(self) -> str:
        return "slot {} {}{}".format(
            "at or after" if self.after else "exactly",
            self.slot,
            "" if self.ledger is None else " (ledger {})".format(
                self.ledger.key
            ),
        )


class OutcomeRule(ClaimRule):
    """Claim only once another entry has signed with a given outcome."""

    def __init__(
        self,
        entry: str,
        outcome: str,
        ledger: Optional[Ledger] = None,
    ) -> None:
        super().__init__(ledger=ledger)
        self.entry = entry
        self.outcome = outcome

    def check(self, entry: "Entry", state: "_RuntimeState") -> Optional[str]:
        actual = state.outcomes.get(self.entry)
        if actual == self.outcome:
            return None
        return "entry {!r} must outcome {!r} (got {!r})".format(
            self.entry, self.outcome, actual
        )

    def describe(self) -> str:
        return "outcome {}={}{}".format(
            self.entry,
            self.outcome,
            "" if self.ledger is None else " (ledger {})".format(
                self.ledger.key
            ),
        )


class SettledRule(ClaimRule):
    """Claim only once another entry has any signing conclusion."""

    def __init__(
        self,
        entry: str,
        ledger: Optional[Ledger] = None,
    ) -> None:
        super().__init__(ledger=ledger)
        self.entry = entry

    def check(self, entry: "Entry", state: "_RuntimeState") -> Optional[str]:
        if self.entry in state.outcomes:
            return None
        return "entry {!r} must have a signing conclusion".format(self.entry)

    def describe(self) -> str:
        return "settled {}{}".format(
            self.entry,
            "" if self.ledger is None else " (ledger {})".format(
                self.ledger.key
            ),
        )


class BindingRule(ClaimRule):
    """Claim only while an entry's effective ledger is ``ledger``."""

    def __init__(self, entry: str, ledger: Ledger) -> None:
        super().__init__(ledger=ledger)
        self.entry = entry

    @property
    def required_ledger(self) -> Ledger:
        assert self.ledger is not None
        return self.ledger

    def check(self, entry: "Entry", state: "_RuntimeState") -> Optional[str]:
        actual = state.bindings.get(self.entry)
        if actual == self.required_ledger:
            return None
        return "entry {!r} must be bound to {!r} (got {!r})".format(
            self.entry, self.required_ledger, actual
        )

    def describe(self) -> str:
        return "binding {}={}".format(self.entry, self.required_ledger.key)


class Entry:
    """
    An entry point (入口) of a work order.

    :param name: Unique entry name.
    :param collection: The collection configuration the entry belongs to;
        calls without an explicit collection use this configuration (or
        the configuration a switch moved the entry onto) at execution.
    :param task: The fetch/accounting `.Task` run when the entry claims.
    :param kwargs: Keyword arguments for the task call.
    :param explicit_collection: If set, the call always runs against this
        collection, regardless of the entry's then-current configuration.
    :param outcomes: All signing outcomes this entry may produce.
    :param default_outcome: Outcome used when the task result cannot be
        mapped; defaults to the sole declared outcome.
    :param required: Whether the entry must complete on every branch.
    :param deadline: Optional slot name by which signing must finish.
    :param rules: Claim rules gating retrieval of a listed original.
    """

    def __init__(
        self,
        name: str,
        collection: str,
        task: Task,
        kwargs: Optional[Dict[str, Any]] = None,
        explicit_collection: Optional[str] = None,
        outcomes: Sequence[str] = ("signed",),
        default_outcome: Optional[str] = None,
        required: bool = True,
        deadline: Optional[str] = None,
        rules: Optional[Sequence[ClaimRule]] = None,
    ) -> None:
        self.name = name
        self.collection = collection
        self.task = task
        self.kwargs = dict(kwargs or {})
        self.explicit_collection = explicit_collection
        self.outcomes = tuple(outcomes)
        if default_outcome is not None:
            self.default_outcome = default_outcome
        elif len(self.outcomes) == 1:
            self.default_outcome = self.outcomes[0]
        else:
            self.default_outcome = None
        self.required = required
        self.deadline = deadline
        self.rules = tuple(rules or ())

    def fetch_key(self, original: "Original") -> Tuple[Any, ...]:
        # One real fetch per (original, effective collection). Implicit
        # calls share the original's single fetch; explicit collection
        # calls stay distinct per named collection.
        scope = (
            ("explicit", self.explicit_collection)
            if self.explicit_collection is not None
            else ("implicit",)
        )
        return (id(original),) + scope

    def effective_collection(self, state: "_RuntimeState") -> str:
        if self.explicit_collection is not None:
            return self.explicit_collection
        return state.collection_overrides.get(self.name, self.collection)

    def __repr__(self) -> str:
        return "<Entry {!r} collection={!r}>".format(
            self.name, self.collection
        )


class Original:
    """
    An original (原件) issued at a time slot against one ledger.

    The roster is fixed at issuance and can never change afterwards. The
    original may later be withdrawn; existing signatures keep their
    provenance but new claims are refused.
    """

    def __init__(
        self,
        serial: str,
        slot: "TimeSlot",
        ledger: Ledger,
        roster: Sequence[Entry],
    ) -> None:
        self.serial = serial
        self.slot = slot
        self.ledger = ledger
        self.roster: Tuple[Entry, ...] = tuple(roster)
        self.withdrawn = False

    @property
    def active(self) -> bool:
        return not self.withdrawn

    def withdraw(self) -> None:
        self.withdrawn = True

    def __contains__(self, entry: Entry) -> bool:
        return entry in self.roster

    def identity(self) -> Tuple[Any, ...]:
        # Serial-free identity, shared by planning originals and the real
        # originals rebuilt from the chosen policy.
        return (
            self.slot.name,
            self.ledger.key,
            tuple(entry.name for entry in self.roster),
            self.withdrawn,
        )

    def __repr__(self) -> str:
        names = ", ".join(entry.name for entry in self.roster)
        state = "withdrawn" if self.withdrawn else "active"
        return "<Original {} slot={} ledger={} [{}] ({})>".format(
            self.serial, self.slot.name, self.ledger.key, names, state
        )


class TimeSlot:
    """
    An issuance window (时段).

    :param name: Unique slot name, ordered by insertion on the work order.
    :param issuable: Mapping of ledger to the entries allowed to be
        listed on an original of that ledger in this slot.
    :param max_originals: Maximum number of originals the whole work
        order may issue during this slot.
    """

    def __init__(
        self,
        name: str,
        issuable: Mapping[Ledger, Sequence[Entry]],
        max_originals: int = 1,
    ) -> None:
        self.name = name
        self.issuable: Dict[Ledger, Tuple[Entry, ...]] = {
            ledger: tuple(entries) for ledger, entries in issuable.items()
        }
        self.max_originals = max_originals

    def listable(self, ledger: Ledger) -> Tuple[Entry, ...]:
        return self.issuable.get(ledger, ())

    def __repr__(self) -> str:
        return "<TimeSlot {!r}>".format(self.name)


@dataclass
class AuditRecord:
    """The verified record of one event in an entry's journey."""

    entry: str
    collection: str
    explicit_collection: bool
    ledger: str
    ledger_version: str
    original_serial: Optional[str]
    issued_slot: Optional[str]
    claimed_slot: Optional[str]
    status: str
    outcome: Optional[str]
    fetch_seq: Optional[int]

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class AuditTrail:
    """Queryable record of everything that happened during a run."""

    def __init__(self) -> None:
        self.records: List[AuditRecord] = []
        self.originals: Dict[str, Original] = {}

    def add(self, record: AuditRecord) -> None:
        self.records.append(record)

    def for_entry(self, name: str) -> List[AuditRecord]:
        return [record for record in self.records if record.entry == name]

    def for_original(self, serial: str) -> List[AuditRecord]:
        return [
            record
            for record in self.records
            if record.original_serial == serial
        ]

    def original(self, serial: str) -> Original:
        return self.originals[serial]

    def provenance(self, entry_name: str) -> AuditRecord:
        """Return the final signing record for an entry."""
        signed = [
            record
            for record in self.for_entry(entry_name)
            if record.status == "signed"
        ]
        if not signed:
            raise ReconError(
                "Entry {!r} has no signed record".format(entry_name)
            )
        return signed[-1]


class WorkOrder:
    """The full specification of a reconciliation work order."""

    def __init__(self, name: str = "work-order") -> None:
        self.name = name
        self.entries: Dict[str, Entry] = {}
        self.slots: List[TimeSlot] = []
        self._ledger_keys: Dict[Any, Ledger] = {}
        self._registrations: Dict[Tuple[str, str, str], Ledger] = {}
        self.bindings: Dict[str, Ledger] = {}
        self.switches: List[
            Tuple[str, str, str, Ledger, Optional[str]]
        ] = []
        self.withdrawals: List[Tuple[str, Ledger]] = []
        self._serial_counter = 0

    # -- entries ---------------------------------------------------------

    def add_entry(self, entry: Entry, ledger: Ledger) -> Entry:
        if entry.name in self.entries:
            raise ReconError("Duplicate entry {!r}".format(entry.name))
        self.entries[entry.name] = entry
        self.bindings[entry.name] = ledger
        return entry

    # -- ledgers ---------------------------------------------------------

    def register_ledger(
        self,
        collection: str,
        name: str,
        version: str,
        shared: Optional[Ledger] = None,
    ) -> Ledger:
        """
        Register a ledger for a collection.

        Same name/version may be registered twice to obtain two distinct
        books. Pass ``shared`` to register another collection against an
        already-registered book.
        """
        registration_key = (collection, name, version)
        if registration_key in self._registrations:
            return self._registrations[registration_key]
        if shared is not None:
            ledger = shared
        else:
            key = (name, version, len(self._ledger_keys))
            ledger = Ledger(key=key, name=name, version=version)
            self._ledger_keys[key] = ledger
        self._registrations[registration_key] = ledger
        return ledger

    def ledger_for(self, entry_name: str) -> Ledger:
        return self.bindings[entry_name]

    def add_switch(
        self,
        trigger_entry: str,
        outcome: str,
        target_entry: str,
        ledger: Ledger,
        collection: Optional[str] = None,
    ) -> None:
        """
        Declare that ``trigger_entry`` producing ``outcome`` rebinds
        ``target_entry`` onto ``ledger`` (optionally moving its implicit
        collection configuration onto ``collection``).
        """
        for name in (trigger_entry, target_entry):
            if name not in self.entries:
                raise ReconError("Unknown entry {!r}".format(name))
        self.switches.append(
            (trigger_entry, outcome, target_entry, ledger, collection)
        )

    # -- slots -----------------------------------------------------------

    def add_slot(self, slot: TimeSlot) -> TimeSlot:
        if any(existing.name == slot.name for existing in self.slots):
            raise ReconError("Duplicate slot {!r}".format(slot.name))
        self.slots.append(slot)
        return slot

    def slot_index(self, name: str) -> int:
        for index, slot in enumerate(self.slots):
            if slot.name == name:
                return index
        raise ReconError("Unknown slot {!r}".format(name))

    def withdraw_before(self, slot_name: str, ledger: Ledger) -> None:
        """Schedule withdrawal of active originals of a ledger before slot."""
        self.withdrawals.append((slot_name, ledger))

    # -- internals -------------------------------------------------------

    def next_serial(self) -> str:
        self._serial_counter += 1
        return "ORIG-{:04d}".format(self._serial_counter)


@dataclass
class _RuntimeState:
    """Mutable planning/execution state for one branch."""

    order: WorkOrder
    bindings: Dict[str, Ledger]
    collection_overrides: Dict[str, str] = field(default_factory=dict)
    outcomes: Dict[str, str] = field(default_factory=dict)
    originals: List[Original] = field(default_factory=list)
    signed: Dict[str, Original] = field(default_factory=dict)
    current_slot: Optional[str] = None
    fetch_order: Dict[Tuple[Any, ...], int] = field(default_factory=dict)
    fetch_results: Dict[Tuple[Any, ...], Any] = field(default_factory=dict)
    records: List[AuditRecord] = field(default_factory=list)

    @classmethod
    def initial(cls, order: WorkOrder) -> "_RuntimeState":
        return cls(order=order, bindings=dict(order.bindings))

    def clone(self) -> "_RuntimeState":
        return _RuntimeState(
            order=self.order,
            bindings=dict(self.bindings),
            collection_overrides=dict(self.collection_overrides),
            outcomes=dict(self.outcomes),
            originals=list(self.originals),
            signed=dict(self.signed),
            current_slot=self.current_slot,
            fetch_order=dict(self.fetch_order),
            fetch_results=dict(self.fetch_results),
            records=list(self.records),
        )

    def signature(self) -> Tuple[Any, ...]:
        return (
            self.current_slot,
            tuple(sorted(self.outcomes.items())),
            tuple(
                sorted(
                    (name, ledger.key)
                    for name, ledger in self.bindings.items()
                )
            ),
            tuple(sorted(original.identity() for original in self.originals)),
            tuple(sorted(self.signed)),
        )

    def active_original(self, entry: Entry) -> Optional[Original]:
        for original in reversed(self.originals):
            if entry in original and original.active:
                return original
        return None

    def blocked_reason(
        self, entry: Entry, original: Original
    ) -> Optional[Tuple[ClaimRule, str]]:
        for rule in entry.rules:
            ok, reason = rule.satisfied(entry, self, original)
            if not ok:
                return rule, reason
        if self.bindings[entry.name] != original.ledger:
            rule = BindingRule(entry.name, original.ledger)
            return rule, "effective ledger does not match the original"
        return None

    def apply_outcome(self, entry: Entry, outcome: str) -> None:
        self.outcomes[entry.name] = outcome
        for trigger, result, target, ledger, collection in (
            self.order.switches
        ):
            if trigger == entry.name and result == outcome:
                self.bindings[target] = ledger
                if collection is not None:
                    self.collection_overrides[target] = collection

    def apply_withdrawals(self, slot: TimeSlot) -> List[Original]:
        withdrawn = []
        for slot_name, ledger in self.order.withdrawals:
            if slot_name != slot.name:
                continue
            for original in self.originals:
                if original.ledger == ledger and original.active:
                    original.withdraw()
                    withdrawn.append(original)
        return withdrawn


class _JointEngine:
    """
    Deterministic minimax engine shared by planning and execution.

    At every slot each eligible ledger may issue one original carrying
    every entry currently allowed on it (listing is free and never harms
    later claims), subject to the slot's original cap. The engine walks
    every legal issuance and every declared outcome branch: arrangements
    that strand a required entry are rejected, and among the feasible
    ones the arrangement with the smallest worst-case fetch count wins,
    with a stable tie-break.

    Planning (`.verify`) and live execution (`.choose`) run the same
    memoized search, so the choice made on the branch that actually
    occurs is exactly the one the guarantee was established for.
    """

    def __init__(self, order: WorkOrder) -> None:
        self.order = order
        self.memo: Dict[
            Tuple[int, Tuple[Any, ...]],
            Tuple[int, List[Original]],
        ] = {}

    # -- public API ------------------------------------------------------

    def verify(self) -> int:
        """Walk every branch; raise on a dead end.

        Returns the smallest achievable worst-case fetch count.
        """
        state = _RuntimeState.initial(self.order)
        worst, _chosen = self._evaluate(state, 0)
        return worst

    def choose(
        self, state: _RuntimeState, slot_index: int
    ) -> List[Original]:
        """Return the optimal issuance for the state at ``slot_index``."""
        _worst, chosen = self._evaluate(state, slot_index)
        return chosen

    # -- search ----------------------------------------------------------

    def _evaluate(
        self, state: _RuntimeState, slot_index: int
    ) -> Tuple[int, List[Original]]:
        if slot_index >= len(self.order.slots):
            self._check_leaf(state)
            return len(state.fetch_order), []
        slot = self.order.slots[slot_index]
        state.current_slot = slot.name
        state.apply_withdrawals(slot)
        key = (slot_index, state.signature())
        if key in self.memo:
            return self.memo[key]
        choices = self._issuance_choices(state, slot)
        failures: List[GuaranteeViolation] = []
        scored: List[Tuple[int, List[Original]]] = []
        for originals in choices:
            candidate = state.clone()
            candidate.originals.extend(originals)
            try:
                worst, _next = self._settle(candidate, slot_index)
            except GuaranteeViolation as error:
                failures.append(error)
                continue
            scored.append((worst, originals))
        if not scored:
            if failures:
                # Prefer the failure that progressed furthest: a concrete
                # claim-rule block over a mere "no original", then the
                # branch with the most settled outcomes.
                failures.sort(
                    key=lambda error: (
                        isinstance(error.rule, _NoOriginalRule),
                        -len(error.outcome_chain),
                    )
                )
                raise failures[0]
            raise GuaranteeViolation(
                entry=next(iter(self.order.entries.values())),
                outcome_chain=state.outcomes,
                rule=_NoOriginalRule(),
                reason="no legal issuance in slot {!r}".format(slot.name),
            )
        scored.sort(
            key=lambda item: (item[0], self._rank(item[1]))
        )
        result = scored[0]
        self.memo[key] = result
        return result

    def _settle(
        self, state: _RuntimeState, slot_index: int
    ) -> Tuple[int, List[Original]]:
        # Claim everything currently unlockable. Deterministic entries
        # settle in place; an uncertain entry's outcome branches here, so
        # later slots in each branch see only information known after the
        # signing actually occurred.
        while True:
            self._claim_ready(state)
            pending = [
                entry
                for entry in self.order.entries.values()
                if entry.name in state.signed
                and entry.name not in state.outcomes
                and len(entry.outcomes) > 1
            ]
            if not pending:
                return self._evaluate(state, slot_index + 1)
            entry = pending[0]
            worst = 0
            for outcome in entry.outcomes:
                branch = state.clone()
                branch.apply_outcome(entry, outcome)
                sub_worst, _ = self._settle(branch, slot_index)
                worst = max(worst, sub_worst)
            return worst, []

    # -- choices & claims ------------------------------------------------

    def _issuance_choices(
        self, state: _RuntimeState, slot: TimeSlot
    ) -> List[List[Original]]:
        ledgers = [
            ledger
            for ledger in slot.issuable
            if any(
                entry.name not in state.signed
                for entry in slot.listable(ledger)
            )
        ]
        choices: List[List[Original]] = [[]]
        for size in range(1, min(slot.max_originals, len(ledgers)) + 1):
            for combo in itertools.combinations(ledgers, size):
                originals = []
                for ledger in combo:
                    roster = [
                        entry
                        for entry in slot.listable(ledger)
                        if entry.name not in state.signed
                    ]
                    originals.append(
                        Original(
                            serial="PLAN",
                            slot=slot,
                            ledger=ledger,
                            roster=roster,
                        )
                    )
                choices.append(originals)
        choices.sort(key=self._rank)
        return choices

    def _claim_ready(self, state: _RuntimeState) -> None:
        progress = True
        while progress:
            progress = False
            for entry in self.order.entries.values():
                if entry.name in state.signed:
                    continue
                original = state.active_original(entry)
                if original is None:
                    continue
                if state.blocked_reason(entry, original) is not None:
                    continue
                state.signed[entry.name] = original
                state.fetch_order.setdefault(
                    entry.fetch_key(original), len(state.fetch_order) + 1
                )
                # Deterministic outcomes (and their switches) apply
                # immediately; uncertain ones are branched on later.
                if len(entry.outcomes) == 1:
                    state.apply_outcome(entry, entry.outcomes[0])
                progress = True

    # -- guarantee -------------------------------------------------------

    def _check_leaf(self, state: _RuntimeState) -> None:
        for entry in self.order.entries.values():
            if not entry.required or entry.name in state.signed:
                continue
            if entry.deadline is not None:
                raise GuaranteeViolation(
                    entry=entry,
                    outcome_chain=state.outcomes,
                    rule=SlotRule(entry.deadline, after=True),
                    reason="deadline passed without signing",
                )
            original = state.active_original(entry)
            if original is None:
                raise GuaranteeViolation(
                    entry=entry,
                    outcome_chain=state.outcomes,
                    rule=_NoOriginalRule(),
                    reason="no active original lists this entry",
                )
            blocked = state.blocked_reason(entry, original)
            assert blocked is not None
            rule, reason = blocked
            raise GuaranteeViolation(
                entry=entry,
                outcome_chain=state.outcomes,
                rule=rule,
                reason=reason,
            )

    # -- ranking ---------------------------------------------------------

    def _rank(self, originals: List[Original]) -> Tuple[Any, ...]:
        return tuple(
            (
                original.ledger.key,
                tuple(entry.name for entry in original.roster),
            )
            for original in originals
        )


class _NoOriginalRule(ClaimRule):
    def check(self, entry: "Entry", state: "_RuntimeState") -> Optional[str]:
        return "no listable original"

    def describe(self) -> str:
        return "no original available"


class JointExecutor:
    """
    Executor for the joint (合办) execution style.

    Wraps a regular `.Executor` so each unique real fetch still runs the
    ordinary pre/main/post task chain. Construction runs the full
    minimax verification: it raises `.GuaranteeViolation` (naming the
    outcome chain, required entry and blocking rule) if any declared
    branch can strand a required entry, and otherwise remembers the
    smallest achievable worst-case fetch count. The live run then makes
    the same deterministic optimal choice at each slot on the branch
    that actually occurs.
    """

    def __init__(
        self,
        order: WorkOrder,
        collection: Collection,
        config: Optional[Config] = None,
        executor: Optional[Executor] = None,
        outcome_of: Optional[Callable[[Any], Optional[str]]] = None,
    ) -> None:
        self.order = order
        self.collection = collection
        self.config = config
        self.executor = executor or Executor(
            collection=collection, config=config
        )
        self.outcome_of = outcome_of or (lambda result: None)
        self.trail = AuditTrail()
        self._engine = _JointEngine(order)
        self.worst_case_fetches = self._engine.verify()

    def run(self) -> AuditTrail:
        state = _RuntimeState.initial(self.order)
        for slot_index, slot in enumerate(self.order.slots):
            state.current_slot = slot.name
            self._apply_withdrawals(state, slot)
            self._issue(state, slot, slot_index)
            self._claim_and_sign(state, slot)
        self._assert_complete(state)
        return self.trail

    # -- issuance --------------------------------------------------------

    def _issue(
        self,
        state: _RuntimeState,
        slot: TimeSlot,
        slot_index: int,
    ) -> List[Original]:
        planned = self._engine.choose(state, slot_index)
        if not planned:
            return []
        originals = []
        for plan_original in planned:
            roster = [
                self.order.entries[entry.name]
                for entry in plan_original.roster
            ]
            original = Original(
                serial=self.order.next_serial(),
                slot=slot,
                ledger=plan_original.ledger,
                roster=roster,
            )
            state.originals.append(original)
            self.trail.originals[original.serial] = original
            for entry in roster:
                self._record(
                    state,
                    entry,
                    original,
                    status="listed",
                    outcome=None,
                    fetch_seq=None,
                    claimed_slot=None,
                )
            originals.append(original)
        return originals

    def _apply_withdrawals(
        self, state: _RuntimeState, slot: TimeSlot
    ) -> None:
        state.apply_withdrawals(slot)

    # -- claiming & signing ----------------------------------------------

    def _claim_and_sign(
        self, state: _RuntimeState, slot: TimeSlot
    ) -> None:
        progress = True
        reported: set = set()
        while progress:
            progress = False
            for entry in self.order.entries.values():
                if entry.name in state.signed:
                    continue
                original = state.active_original(entry)
                if original is None:
                    continue
                blocked = state.blocked_reason(entry, original)
                if blocked is not None:
                    key = (
                        entry.name,
                        original.serial,
                        slot.name,
                        blocked[0].describe(),
                    )
                    if key not in reported:
                        reported.add(key)
                        self._record(
                            state,
                            entry,
                            original,
                            status="blocked",
                            outcome=None,
                            fetch_seq=None,
                            claimed_slot=None,
                        )
                    continue
                outcome, fetch_seq = self._sign(state, entry, original)
                state.signed[entry.name] = original
                state.apply_outcome(entry, outcome)
                self._record(
                    state,
                    entry,
                    original,
                    status="signed",
                    outcome=outcome,
                    fetch_seq=fetch_seq,
                    claimed_slot=slot.name,
                )
                progress = True

    def _sign(
        self, state: _RuntimeState, entry: Entry, original: Original
    ) -> Tuple[str, int]:
        key = entry.fetch_key(original)
        if key not in state.fetch_order:
            state.fetch_order[key] = len(state.fetch_order) + 1
            # Real fetch: the ordinary Executor runs pre/main/post tasks.
            results = self.executor.execute(
                (entry.task.name, dict(entry.kwargs))
            )
            state.fetch_results[key] = results.get(entry.task)
        fetch_seq = state.fetch_order[key]
        result = state.fetch_results.get(key)
        outcome = self.outcome_of(result)
        if outcome is None:
            outcome = entry.default_outcome
        if outcome not in entry.outcomes:
            raise ReconError(
                "Entry {!r} produced undeclared outcome {!r}".format(
                    entry.name, outcome
                )
            )
        return outcome, fetch_seq

    # -- records ---------------------------------------------------------

    def _record(
        self,
        state: _RuntimeState,
        entry: Entry,
        original: Original,
        status: str,
        outcome: Optional[str],
        fetch_seq: Optional[int],
        claimed_slot: Optional[str],
    ) -> None:
        record = AuditRecord(
            entry=entry.name,
            collection=entry.effective_collection(state),
            explicit_collection=entry.explicit_collection is not None,
            ledger=original.ledger.name,
            ledger_version=original.ledger.version,
            original_serial=original.serial,
            issued_slot=original.slot.name,
            claimed_slot=claimed_slot,
            status=status,
            outcome=outcome,
            fetch_seq=fetch_seq,
        )
        state.records.append(record)
        self.trail.add(record)

    def _assert_complete(self, state: _RuntimeState) -> None:
        missing = [
            name
            for name, entry in self.order.entries.items()
            if entry.required and name not in state.signed
        ]
        if missing:
            raise GuaranteeViolation(
                entry=self.order.entries[missing[0]],
                outcome_chain=state.outcomes,
                rule=_NoOriginalRule(),
                reason="unsigned at end of run: {}".format(missing),
            )
