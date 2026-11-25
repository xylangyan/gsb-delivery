"""Material reconciliation for macro-expanded G-code.

An outsourced printer records its print stream as *snippets* plus repeat counts,
and snippets may invoke smaller snippets.  This module keeps that tree intact:
commands are expanded lazily and addressed by their execution ordinal, so a
query never has to materialise the whole (potentially enormous) stream.

The accounting model is physical rather than a simple sum of ``E`` values:

* every tool keeps its own extruder coordinate;
* ``M82`` / ``M83`` select absolute / relative interpretation of later ``E``
  words (the mode is modal, as on the machine);
* ``G92 E`` resets only the coordinate of the tool active at that command;
* negative extrusion displacement opens a retraction debt on the spool fed by
  the active tool;
* positive displacement first repays that spool's open retractions in
  first-opened-first-repaid order, and only the remainder is new material.

Retraction debt belongs to the *spool*, not to a tool.  A handover may reconnect
a tool to another spool between two operations; debt left on the old spool stays
there while the tool's own E coordinate continues unchanged.

Material quantities use :class:`decimal.Decimal`, preserving the decimal
precision written in the source commands.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import GcodeLine, parse_gcode_lines


__all__ = [
    "ABSOLUTE",
    "RELATIVE",
    "CommandCredential",
    "CommandId",
    "CommandPosition",
    "ExecutedCommand",
    "Handover",
    "Ledger",
    "MaterialLedger",
    "NewUsage",
    "Payment",
    "PrintProgram",
    "Program",
    "Repeat",
    "Repayment",
    "Retraction",
    "RetractionId",
    "Snippet",
    "SourceRef",
    "UsageWindow",
    "WindowReport",
    "analyze",
    "as_decimal",
    "decimal_text",
]

ABSOLUTE = "absolute"
RELATIVE = "relative"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


def as_decimal(value: int | float | str | Decimal) -> Decimal:
    """Convert a G-code numeric value to an exact :class:`Decimal`.

    Floats are routed through ``str`` so ``0.1`` does not acquire binary
    floating point dust.
    """

    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise ValueError(f"not a decimal value: {value!r}") from exc
    raise TypeError(f"cannot convert {type(value).__name__} to Decimal")


def _ceil_div(value: int, divisor: int) -> int:
    return -((-value) // divisor)




def decimal_text(value: Decimal) -> str:
    """Render a Decimal in plain notation, preserving its written scale."""

    return format(value, "f")


@dataclass(frozen=True, order=True)
class SourceRef:
    """Location of a command in its original, unexpanded snippet."""

    snippet: str
    line_index: int
    command_index: int = 0

    def __str__(self) -> str:
        return f"{self.snippet}:{self.line_index}:{self.command_index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "snippet": self.snippet,
            "line_index": self.line_index,
            "command_index": self.command_index,
        }


@dataclass(frozen=True)
class CommandId:
    """Credential for one concrete execution of one original command.

    ``ordinal`` is the position in the fully expanded command stream.  Because
    it is part of the value, two executions of the same source line never
    compare equal even though their :class:`SourceRef` is identical.
    ``iteration`` records the repeat indices that produced this execution and is
    useful when a credential is displayed to a human.
    """

    ordinal: int
    source: SourceRef | None = None
    iteration: tuple[tuple[str, int], ...] = ()
    text: str = ""

    @property
    def position(self) -> int:
        return self.ordinal

    @property
    def credential(self) -> "CommandId":
        return self

    def __str__(self) -> str:
        source = str(self.source) if self.source is not None else "<synthetic>"
        return f"{source}@{self.ordinal}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "source": self.source.to_dict() if self.source is not None else None,
            "iteration": [list(part) for part in self.iteration],
            "text": self.text,
        }


CommandCredential = CommandId
CommandPosition = CommandId


@dataclass(frozen=True)
class Handover:
    """Attach ``tool`` to ``spool`` at this point in the stream."""

    tool: int
    spool: str


# ---------------------------------------------------------------------------
# Internal program nodes
# ---------------------------------------------------------------------------


@dataclass
class _CommandNode:
    line: GcodeLine
    source_line: int


@dataclass
class _RepeatNode:
    target: "Snippet | str"
    count: int


@dataclass(frozen=True)
class _HandoverNode:
    tool: int
    spool: str


class Snippet:
    """An original snippet: literal G-code and/or references to smaller snippets.

    Body items may be:

    * a G-code string (one or more lines, possibly with several commands each);
    * ``Repeat(snippet, count)``;
    * ``Handover(tool, spool)``;
    * another :class:`Snippet` (executed once where it appears).
    """

    def __init__(self, body: Sequence[Any] | str, name: str | None = None):
        if isinstance(body, str):
            body = [body]
        self.body: list[Any] = list(body)
        self.name = name
        self._compiled: list[Any] | None = None
        self._count_cache: dict[int, int] = {}
        self._counting: set[int] = set()

    @classmethod
    def from_gcode(cls, text: str, name: str | None = None) -> "Snippet":
        return cls([text], name=name)

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"Snippet(name={self.name!r}, body=<{len(self.body)} part(s)>"

    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = object.__hash__

    def _compile(self) -> list[Any]:
        if self._compiled is None:
            compiled: list[Any] = []
            line_offset = 0
            for item in self.body:
                if isinstance(item, str):
                    parsed = list(parse_gcode_lines(item))
                    max_line = -1
                    for line in parsed:
                        max_line = max(max_line, line.line_index)
                        compiled.append(
                            _CommandNode(line=line, source_line=line_offset + line.line_index)
                        )
                    line_offset += max_line + 1
                elif isinstance(item, Repeat):
                    compiled.append(_RepeatNode(item.snippet, int(item.count)))
                elif isinstance(item, Handover):
                    compiled.append(_HandoverNode(int(item.tool), str(item.spool)))
                elif isinstance(item, Snippet):
                    compiled.append(_RepeatNode(item, 1))
                else:
                    raise TypeError(f"unsupported snippet body item: {item!r}")
            self._compiled = compiled
        return self._compiled

    def command_count(self, program: "PrintProgram") -> int:
        key = id(program)
        cached = self._count_cache.get(key)
        if cached is not None:
            return cached
        if key in self._counting:
            raise ValueError("recursive snippet reference detected")
        self._counting.add(key)
        try:
            total = 0
            for node in self._compile():
                if isinstance(node, _CommandNode):
                    total += 1
                elif isinstance(node, _RepeatNode):
                    total += node.count * program.resolve_snippet(node.target).command_count(
                        program
                    )
        finally:
            self._counting.discard(key)
        self._count_cache[key] = total
        return total


@dataclass
class Repeat:
    """Execute ``snippet`` ``count`` times."""

    snippet: "Snippet | str"
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("repeat count cannot be negative")


class PrintProgram:
    """A lazy, nested print stream."""

    def __init__(
        self,
        root: Snippet | Repeat | str,
        snippets: Mapping[str, Snippet] | None = None,
        *,
        name: str = "root",
    ):
        self.snippets: dict[str, Snippet] = dict(snippets or {})
        if isinstance(root, str):
            root_snippet = Snippet.from_gcode(root, name=name)
        elif isinstance(root, Repeat):
            root_snippet = Snippet([root], name=name)
        else:
            root_snippet = root
        if root_snippet.name is None:
            root_snippet.name = name
        self.root = root_snippet
        self.snippets.setdefault(self.root.name or name, root_snippet)
        self.name = name

    # -- construction helpers -------------------------------------------

    @classmethod
    def from_gcode(cls, text: str, name: str = "root") -> "PrintProgram":
        return cls(Snippet.from_gcode(text, name=name), name=name)

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any] | str | Path) -> "PrintProgram":
        """Build a program from a JSON file or an already-parsed mapping.

        Body items are G-code strings or objects::

            {"repeat": {"snippet": "pass", "count": 10}}
            {"handover": {"tool": 1, "spool": "B"}}

        A snippet may also be written as ``{"body": [...]}``.  The root may be a
        snippet name, a ``{"snippet": ..., "count": ...}`` object, or a body.
        """

        if isinstance(spec, (str, Path)):
            spec = json.loads(Path(spec).read_text())
        if not isinstance(spec, Mapping):
            raise TypeError("program specification must be a mapping")

        snippets: dict[str, Snippet] = {}
        for snippet_name, body in (spec.get("snippets", {}) or {}).items():
            if isinstance(body, Mapping) and "body" in body:
                body = body["body"]
            snippets[snippet_name] = Snippet(_body_items(body), name=snippet_name)

        root_spec = spec.get("root")
        if root_spec is None:
            if not snippets:
                raise ValueError("program specification needs a root or snippets")
            root: Snippet | Repeat | str = snippets[next(iter(snippets))]
        elif isinstance(root_spec, str):
            root = snippets[root_spec] if root_spec in snippets else root_spec
        elif isinstance(root_spec, Mapping):
            target = root_spec.get("snippet") or root_spec.get("name")
            if target is None:
                raise ValueError("root object needs a 'snippet' name")
            count = int(root_spec.get("count", root_spec.get("repeat", 1)))
            root = Repeat(snippets[target] if target in snippets else target, count)
        elif isinstance(root_spec, Sequence):
            root = Snippet(_body_items(root_spec), name="root")
        else:
            root = root_spec  # type: ignore[assignment]

        return cls(root, snippets=snippets, name=spec.get("name", "root"))

    def resolve_snippet(self, target: Snippet | str) -> Snippet:
        if isinstance(target, Snippet):
            return target
        try:
            return self.snippets[target]
        except KeyError as exc:
            raise KeyError(f"unknown snippet {target!r}") from exc

    # -- expansion ------------------------------------------------------

    def command_count(self) -> int:
        return self.root.command_count(self)

    def __len__(self) -> int:
        return self.command_count()

    def iter_actions(
        self,
    ) -> Iterator[tuple[str, Any]]:
        """Yield ``("command", (node, snippet_name, prefix))`` / ``("handover", handover)``.

        Expansion is depth-first and fully lazy: a repeat block is expanded one
        iteration at a time, and nothing is materialised up front.
        """

        yield from self._iter_snippet(
            self.root, self.root.name or self.name, ()
        )

    def command_at(self, ordinal: int) -> tuple[_CommandNode, str, tuple[tuple[str, int], ...]]:
        """Locate one command structurally, without expanding the whole stream.

        Repeat blocks are skipped arithmetically using their command counts, so
        lookup cost is proportional to nesting depth rather than stream length.
        """

        if ordinal < 0 or ordinal >= self.command_count():
            raise IndexError(
                f"command ordinal {ordinal} outside stream 0..{self.command_count() - 1}"
            )
        return self._locate(
            self.root, self.root.name or self.name, (), ordinal
        )

    def _locate(
        self,
        snippet: Snippet,
        snippet_name: str,
        prefix: tuple[tuple[str, int], ...],
        ordinal: int,
    ) -> tuple[_CommandNode, str, tuple[tuple[str, int], ...]]:
        for node in snippet._compile():
            if isinstance(node, _CommandNode):
                if ordinal == 0:
                    return node, snippet_name, prefix
                ordinal -= 1
            elif isinstance(node, _RepeatNode):
                child = self.resolve_snippet(node.target)
                per = child.command_count(self)
                if per == 0:
                    continue
                total = per * node.count
                if ordinal >= total:
                    ordinal -= total
                    continue
                index, offset = divmod(ordinal, per)
                child_name = child.name or "<anonymous>"
                return self._locate(
                    child, child_name, prefix + ((child_name, index),), offset
                )
        raise IndexError("ordinal out of range")  # pragma: no cover - guarded

    def command_id_at(self, ordinal: int) -> CommandId:
        """Build a credential for one ordinal without executing the stream."""

        node, snippet_name, prefix = self.command_at(ordinal)
        line = node.line
        return CommandId(
            ordinal=ordinal,
            source=SourceRef(snippet_name, node.source_line, line.command_index),
            iteration=prefix,
            text=line.gcode_str,
        )

    def _iter_snippet(
        self,
        snippet: Snippet,
        snippet_name: str,
        prefix: tuple[tuple[str, int], ...],
    ) -> Iterator[tuple[str, Any]]:
        for node in snippet._compile():
            if isinstance(node, _CommandNode):
                yield "command", (node, snippet_name, prefix)
            elif isinstance(node, _HandoverNode):
                yield "handover", node
            else:
                child = self.resolve_snippet(node.target)
                child_name = child.name or "<anonymous>"
                for index in range(node.count):
                    yield from self._iter_snippet(
                        child, child_name, prefix + ((child_name, index),)
                    )

    def iter_commands(
        self, start: int = 0, stop: int | None = None
    ) -> Iterator[ExecutedCommand]:
        """Iterate expanded commands in execution order.

        ``start`` is inclusive and ``stop`` exclusive.  Commands outside the
        requested range are still visited by the lazy cursor (a stream cannot
        be entered mid-state), but only commands in range are yielded.
        """

        if start < 0:
            raise ValueError("start cannot be negative")
        total = self.command_count()
        if stop is None:
            stop = total
        stop = min(stop, total)
        ordinal = 0
        for kind, payload in self.iter_actions():
            if kind != "command":
                continue
            if ordinal >= stop:
                return
            if ordinal >= start:
                node, snippet_name, prefix = payload
                yield ExecutedCommand(
                    id=CommandId(
                        ordinal=ordinal,
                        source=SourceRef(
                            snippet_name, node.source_line, node.line.command_index
                        ),
                        iteration=prefix,
                        text=node.line.gcode_str,
                    ),
                    line=node.line,
                )
            ordinal += 1


Program = PrintProgram


def _body_items(body: Any) -> list[Any]:
    if isinstance(body, str):
        return [body]
    if not isinstance(body, Sequence):
        raise TypeError("snippet body must be a string or a list of items")
    items: list[Any] = []
    for item in body:
        if isinstance(item, Mapping):
            if "repeat" in item:
                spec = item["repeat"]
                if isinstance(spec, str):
                    spec = {"snippet": spec, "count": 1}
                name = spec.get("snippet") or spec.get("name") or spec.get("target")
                count = int(spec.get("count", spec.get("times", 1)))
                items.append(Repeat(name, count))
            elif "handover" in item:
                spec = item["handover"]
                items.append(Handover(int(spec["tool"]), str(spec["spool"])))
            elif "gcode" in item:
                items.append(str(item["gcode"]))
            elif "body" in item:
                items.append(Snippet(_body_items(item["body"]), name=item.get("name")))
            else:
                raise ValueError(f"unrecognised snippet body item: {item!r}")
        else:
            items.append(item)
    return items


# ---------------------------------------------------------------------------
# Executed command view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutedCommand:
    """One command produced by expanding a :class:`PrintProgram`."""

    id: CommandId
    line: GcodeLine


# ---------------------------------------------------------------------------
# Ledger events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetractionId:
    spool: str
    index: int

    def __str__(self) -> str:
        return f"{self.spool}:{self.index}"

    def to_dict(self) -> dict[str, Any]:
        return {"spool": self.spool, "index": self.index}


@dataclass(frozen=True)
class Payment:
    """One repayment of one retraction by one positive-extrusion command."""

    retraction: RetractionId
    amount: Decimal
    credential: CommandId
    part_index: int = 0

    @property
    def ordinal(self) -> int:
        return self.credential.ordinal

    def to_dict(self) -> dict[str, Any]:
        return {
            "retraction": self.retraction.to_dict(),
            "amount": decimal_text(self.amount),
            "credential": self.credential.to_dict(),
            "part_index": self.part_index,
        }


Repayment = Payment


@dataclass
class Retraction:
    id: RetractionId
    spool: str
    amount: Decimal
    origin: CommandId
    remaining: Decimal

    @property
    def ordinal(self) -> int:
        return self.origin.ordinal

    @property
    def repaid(self) -> Decimal:
        return self.amount - self.remaining

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id.to_dict(),
            "spool": self.spool,
            "amount": decimal_text(self.amount),
            "remaining": decimal_text(self.remaining),
            "origin": self.origin.to_dict(),
        }


@dataclass(frozen=True)
class NewUsage:
    spool: str
    amount: Decimal
    credential: CommandId
    part_index: int = 0

    @property
    def ordinal(self) -> int:
        return self.credential.ordinal

    def to_dict(self) -> dict[str, Any]:
        return {
            "spool": self.spool,
            "amount": decimal_text(self.amount),
            "credential": self.credential.to_dict(),
            "part_index": self.part_index,
        }


@dataclass
class WindowReport:
    """Result of a window query.

    ``start`` and ``end`` are inclusive command ordinals, unless the query set
    ``inclusive_end=False`` (then ``end`` is the first ordinal after the window).
    """

    start: int
    end: int
    usage: dict[str, Decimal]
    first_new_usage: CommandId | None
    first_new_usage_by_spool: dict[str, CommandId]
    inclusive_end: bool = True
    events: tuple[NewUsage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "inclusive_end": self.inclusive_end,
            "usage": {
                spool: decimal_text(amount) for spool, amount in sorted(self.usage.items())
            },
            "first_new_usage": (
                self.first_new_usage.to_dict() if self.first_new_usage is not None else None
            ),
            "first_new_usage_by_spool": {
                spool: credential.to_dict()
                for spool, credential in sorted(self.first_new_usage_by_spool.items())
            },
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True)
class UsageWindow:
    start: int = 0
    end: int | None = None
    inclusive_end: bool = True


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class _Frame:
    """One active snippet expansion in the resumable walker stack."""

    items: list[Any]
    cursor: int
    snippet_name: str
    prefix: tuple[tuple[str, int], ...]
    indices: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Span:
    """A run of identical repeat iterations collapsed into one record.

    ``events`` are ``(offset, spool, amount)``: the material is released by the
    command at ``offset`` within each iteration.  Because every skipped iteration
    starts from the same accounting state, its events recur at
    ``base + offset + k * step``.
    """

    step: int
    count: int
    base: int
    events: tuple[tuple[int, str, Decimal], ...]
    # Self-contained retraction units produced per cycle.  Each tuple is
    # (open_offset, spool, amount, ((repay_offset, repay_amount), ...)).
    retractions: tuple[
        tuple[int, str, Decimal, tuple[tuple[int, Decimal], ...]], ...
    ] = ()
    # Retraction index assigned by the ledger to the first cycle's first unit,
    # per spool, so identities can be reconstructed lazily.
    retraction_base: tuple[tuple[str, int], ...] = ()

    def retraction_count(self, spool: str) -> int:
        return sum(1 for _, unit_spool, _, _ in self.retractions if unit_spool == spool)

    def base_index(self, spool: str) -> int:
        return dict(self.retraction_base).get(spool, 0)

    def retraction(self, ledger: "MaterialLedger", spool: str, index: int) -> Retraction | None:
        base = self.base_index(spool)
        per_cycle = self.retraction_count(spool)
        if per_cycle <= 0 or index < base:
            return None
        relative = index - base
        cycle, local = divmod(relative, per_cycle)
        if cycle >= self.count:
            return None
        selected = None
        for offset, unit_spool, amount, payments in self.retractions:
            if unit_spool != spool:
                continue
            if local == 0:
                selected = (offset, amount, payments)
                break
            local -= 1
        if selected is None:
            return None
        offset, amount, payments = selected
        cycle_base = self.base + cycle * self.step
        repaid = sum((payment for _, payment in payments), Decimal(0))
        return Retraction(
            id=RetractionId(spool, index),
            spool=spool,
            amount=amount,
            origin=ledger.program.command_id_at(cycle_base + offset),
            remaining=amount - repaid,
        )

    def retraction_payments(
        self, ledger: "MaterialLedger", spool: str, index: int
    ) -> list[Payment]:
        base = self.base_index(spool)
        per_cycle = self.retraction_count(spool)
        if per_cycle <= 0 or index < base:
            return []
        relative = index - base
        cycle, local = divmod(relative, per_cycle)
        if cycle >= self.count:
            return []
        position = local
        for offset, unit_spool, _, payments in self.retractions:
            if unit_spool != spool:
                continue
            if position == 0:
                cycle_base = self.base + cycle * self.step
                return [
                    Payment(
                        retraction=RetractionId(spool, index),
                        amount=amount,
                        credential=ledger.program.command_id_at(cycle_base + repay_offset),
                    )
                    for repay_offset, amount in payments
                ]
            position -= 1
        return []

    def _released(self, offset: int, start: int, end: int) -> int:
        first = self.base + offset
        low = max(0, _ceil_div(start - first, self.step))
        if low >= self.count:
            return 0
        high = min((end - first) // self.step, self.count - 1)
        return max(0, high - low + 1)

    def amount_between(self, start: int, end: int, spool: str | None = None) -> Decimal:
        return sum(
            (
                amount * self._released(offset, start, end)
                for offset, event_spool, amount in self.events
                if spool is None or event_spool == spool
            ),
            Decimal(0),
        )

    def amounts_by_spool(self, start: int, end: int) -> dict[str, Decimal]:
        result: dict[str, Decimal] = defaultdict(Decimal)
        for offset, spool, amount in self.events:
            result[spool] += amount * self._released(offset, start, end)
        return dict(result)

    def first_ordinal_between(
        self, start: int, end: int, spool: str | None = None
    ) -> int | None:
        best: int | None = None
        for offset, event_spool, amount in self.events:
            if spool is not None and event_spool != spool:
                continue
            if self._released(offset, start, end) == 0:
                continue
            first = self.base + offset
            low = max(0, _ceil_div(start - first, self.step))
            ordinal = first + low * self.step
            if best is None or ordinal < best:
                best = ordinal
        return best

    def credential(self, ledger: "MaterialLedger", ordinal: int) -> CommandId:
        cached = ledger._credentials.get(ordinal)
        return cached if cached is not None else ledger.program.command_id_at(ordinal)




class MaterialLedger:
    """Execute a print program and reconcile material spool by spool.

    Construction is lazy.  Queries advance a resumable walker only as far as the
    highest needed ordinal and record material events; no part of the stream is
    flattened up front.  Long periodic repeat runs can be evaluated
    arithmetically instead of being walked command by command.

    Windows are inclusive command-ordinal ranges by default (``inclusive_end``
    can make the end exclusive).  A window starting after a retraction was
    opened still sees the debt, so only repayments inside the window count.
    """

    def __init__(
        self,
        program: PrintProgram | Snippet | str,
        *,
        initial_spools: Mapping[int | str, str] | None = None,
        initial_tool: int = 0,
        mode: str = ABSOLUTE,
        initial_e: Mapping[int | str, int | float | str | Decimal] | None = None,
        timed_handovers: Mapping[int | CommandId, Iterable[Handover | tuple[int, str]]]
        | None = None,
        spools: Iterable[str] | None = None,
    ):
        if isinstance(program, str):
            program = PrintProgram.from_gcode(program)
        elif isinstance(program, Snippet):
            program = PrintProgram(program)
        if mode not in (ABSOLUTE, RELATIVE):
            raise ValueError("mode must be 'absolute' or 'relative'")
        self.program = program
        self.initial_tool = int(initial_tool)
        self.initial_mode = mode
        self.initial_spools: dict[int, str] = {
            int(tool): str(spool) for tool, spool in (initial_spools or {}).items()
        }
        self.initial_e: dict[int, Decimal] = {
            int(tool): as_decimal(value) for tool, value in (initial_e or {}).items()
        }
        self.timed_handovers: dict[int, list[Handover]] = defaultdict(list)
        for position, handovers in (timed_handovers or {}).items():
            if isinstance(position, CommandId):
                position = position.ordinal
            for handover in handovers:
                if isinstance(handover, Handover):
                    self.timed_handovers[int(position)].append(handover)
                else:
                    tool, spool = handover
                    self.timed_handovers[int(position)].append(Handover(int(tool), str(spool)))

        self._total = program.command_count()
        self._known_spools: set[str] = set(spools or ())
        self._known_spools.update(self.initial_spools.values())
        for handovers in self.timed_handovers.values():
            self._known_spools.update(handover.spool for handover in handovers)

        self._next_ordinal = 0
        self._simulated_through = -1
        self._walking = False
        self._stack: list[_Frame] | None = None
        self._spans: list[_Span] = []

        self._tool = self.initial_tool
        self._mode = self.initial_mode
        self._e: dict[int, Decimal] = defaultdict(Decimal)
        self._e.update(self.initial_e)
        self._spools: dict[int, str] = dict(self.initial_spools)
        self._debt: dict[str, deque[Retraction]] = defaultdict(deque)
        self._released: dict[str, Decimal] = defaultdict(Decimal)
        self._retraction_counters: dict[str, int] = defaultdict(int)
        self._sensitive_commands = 0
        self._applied_timed: set[int] = set()

        self._credentials: dict[int, CommandId] = {}
        self._new_events: list[NewUsage] = []
        self._payments: list[Payment] = []
        self._retractions: list[Retraction] = []
        self._retractions_by_id: dict[RetractionId, Retraction] = {}
        self._retraction_by_origin: dict[int, Retraction] = {}
        self._payments_by_retraction: dict[RetractionId, list[Payment]] = defaultdict(list)
        self._segment_retractions: list[Retraction] = []
        self._segment_payments: list[Payment] = []

    # -- size / provenance ----------------------------------------------

    @property
    def command_count(self) -> int:
        return self._total

    def __len__(self) -> int:
        return self._total

    def credential(self, position: int | CommandId | ExecutedCommand) -> CommandId:
        """Return the full credential for an executed command position."""

        ordinal = self._ordinal(position)
        if ordinal < 0 or ordinal >= self._total:
            raise IndexError(
                f"command ordinal {ordinal} outside stream 0..{self._total - 1}"
            )
        if ordinal in self._credentials:
            return self._credentials[ordinal]
        node, snippet_name, prefix = self.program.command_at(ordinal)
        line = node.line
        return CommandId(
            ordinal=ordinal,
            source=SourceRef(snippet_name, node.source_line, line.command_index),
            iteration=prefix,
            text=line.gcode_str,
        )

    # -- window queries --------------------------------------------------

    def usage(
        self,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        spool: str | None = None,
        inclusive_end: bool = True,
    ) -> dict[str, Decimal]:
        """New material per spool in the command window."""

        start_i, end_i = self._window(start, end, inclusive_end=inclusive_end)
        self._simulate_to(end_i)
        result: dict[str, Decimal] = defaultdict(Decimal)
        for event in self._new_events:
            if event.ordinal < start_i or event.ordinal > end_i:
                continue
            if spool is not None and event.spool != spool:
                continue
            result[event.spool] += event.amount
        self._add_spans(start_i, end_i, result, spool=spool)
        return dict(result)

    def usage_events(
        self,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        spool: str | None = None,
        inclusive_end: bool = True,
    ) -> list[NewUsage]:
        """New-usage events in execution order, each carrying its credential."""

        start_i, end_i = self._window(start, end, inclusive_end=inclusive_end)
        self._simulate_to(end_i)
        events = [
            event
            for event in self._new_events
            if start_i <= event.ordinal <= end_i
            and (spool is None or event.spool == spool)
        ]
        for span in self._spans:
            for event_spool, amount in span.amounts_by_spool(start_i, end_i).items():
                if spool is not None and event_spool != spool:
                    continue
                if not amount:
                    continue
                ordinal = span.first_ordinal_between(start_i, end_i, spool=event_spool)
                events.append(
                    NewUsage(
                        spool=event_spool,
                        amount=amount,
                        credential=span.credential(self, ordinal),
                    )
                )
        events.sort(key=lambda event: event.ordinal)
        return events

    def first_new_usage_by_spool(
        self,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        inclusive_end: bool = True,
    ) -> dict[str, CommandId]:
        start_i, end_i = self._window(start, end, inclusive_end=inclusive_end)
        self._simulate_to(end_i)
        first: dict[str, CommandId] = {}
        for event in self._new_events:
            if event.ordinal < start_i:
                continue
            if event.ordinal > end_i:
                break
            first.setdefault(event.spool, event.credential)
        for span in self._spans:
            for event_spool in {item[1] for item in span.events}:
                ordinal = self._span_first(span, start_i, end_i, spool=event_spool)
                if ordinal is None:
                    continue
                credential = span.credential(self, ordinal)
                existing = first.get(event_spool)
                if existing is None or credential.ordinal < existing.ordinal:
                    first[event_spool] = credential
        return first

    def first_new_usage(
        self,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        spool: str | None = None,
        inclusive_end: bool = True,
    ) -> CommandId | None:
        """Credential of the first command that produced new material."""

        start_i, end_i = self._window(start, end, inclusive_end=inclusive_end)
        self._simulate_to(end_i)
        best_event: CommandId | None = None
        for event in self._new_events:
            if event.ordinal < start_i:
                continue
            if event.ordinal > end_i:
                break
            if spool is None or event.spool == spool:
                best_event = event.credential
                break
        span = self._first_span(start_i, end_i, spool=spool)
        if best_event is None:
            return span
        if span is None:
            return best_event
        return best_event if best_event.ordinal <= span.ordinal else span

    def _add_spans(
        self,
        start_i: int,
        end_i: int,
        result: dict[str, Decimal],
        *,
        spool: str | None = None,
    ) -> None:
        for span in self._spans:
            for event_spool, amount in span.amounts_by_spool(start_i, end_i).items():
                if spool is not None and event_spool != spool:
                    continue
                if amount:
                    result[event_spool] += amount

    def _first_span(
        self,
        start_i: int,
        end_i: int,
        *,
        spool: str | None = None,
    ) -> CommandId | None:
        best: tuple[int, _Span] | None = None
        for span in self._spans:
            ordinal = span.first_ordinal_between(start_i, end_i, spool=spool)
            if ordinal is None:
                continue
            if best is None or ordinal < best[0]:
                best = (ordinal, span)
        return best[1].credential(self, best[0]) if best is not None else None

    def window(
        self,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        inclusive_end: bool = True,
    ) -> WindowReport:
        start_i, end_i = self._window(start, end, inclusive_end=inclusive_end)
        self._simulate_to(end_i)
        usage: dict[str, Decimal] = defaultdict(Decimal)
        first: dict[str, CommandId] = {}
        events: list[NewUsage] = []
        span_events: list[NewUsage] = []
        for event in self._new_events:
            if event.ordinal < start_i:
                continue
            if event.ordinal > end_i:
                break
            events.append(event)
            usage[event.spool] += event.amount
            first.setdefault(event.spool, event.credential)
        for span in self._spans:
            for event_spool, amount in span.amounts_by_spool(start_i, end_i).items():
                if not amount:
                    continue
                ordinal = span.first_ordinal_between(start_i, end_i, spool=event_spool)
                credential = span.credential(self, ordinal)
                usage[event_spool] += amount
                existing = first.get(event_spool)
                if existing is None or credential.ordinal < existing.ordinal:
                    first[event_spool] = credential
                span_events.append(
                    NewUsage(spool=event_spool, amount=amount, credential=credential)
                )
        merged = sorted(events + span_events, key=lambda event: event.ordinal)
        overall = merged[0].credential if merged else None
        return WindowReport(
            start=start_i,
            end=end_i,
            usage=dict(usage),
            first_new_usage=overall,
            first_new_usage_by_spool=first,
            inclusive_end=inclusive_end,
            events=tuple(merged),
        )

    def usage_window(
        self,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        inclusive_end: bool = True,
    ) -> WindowReport:
        return self.window(start, end, inclusive_end=inclusive_end)

    def report(
        self,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        inclusive_end: bool = True,
    ) -> dict[str, Any]:
        """Window usage plus optional repayment details in one JSON-ready dict."""

        window = self.window(start, end, inclusive_end=inclusive_end)
        return window.to_dict()

    # -- retraction queries ---------------------------------------------

    def retractions(self, spool: str | None = None) -> list[Retraction]:
        """All retractions opened in the executed prefix (optionally by spool)."""

        self._simulate_to(self._total - 1)
        if spool is None:
            return list(self._retractions)
        return [retraction for retraction in self._retractions if retraction.spool == spool]

    def retraction(
        self, query: Retraction | RetractionId | str | int | CommandId
    ) -> Retraction:
        self._simulate_to(self._total - 1)
        return self._resolve_retraction(query)

    def retraction_at(self, position: int | CommandId) -> Retraction | None:
        """Retraction opened by the command at ``position``, if any."""

        ordinal = self._ordinal(position)
        self._simulate_to(ordinal)
        return self._retraction_by_origin.get(ordinal)

    def outstanding(
        self, spool: str | None = None, at: int | CommandId | None = None
    ) -> dict[str, Decimal]:
        """Unrepaid retraction debt per spool, optionally at a command position."""

        if at is None:
            self._simulate_to(self._total - 1)
        else:
            self._simulate_to(self._ordinal(at))
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for retraction in self._retractions:
            if retraction.remaining == 0:
                continue
            if spool is not None and retraction.spool != spool:
                continue
            totals[retraction.spool] += retraction.remaining
        return dict(totals)

    def pending(
        self, spool: str, at: int | CommandId | None = None
    ) -> list[Retraction]:
        """Open retractions for ``spool`` in first-opened-first-repaid order."""

        if at is None:
            self._simulate_to(self._total - 1)
        else:
            self._simulate_to(self._ordinal(at))
        return [retraction for retraction in self._debt[spool] if retraction.remaining > 0]

    def state(self, at: int | CommandId | None = None) -> dict[str, Any]:
        """Snapshot of tool coordinates, spool assignment and open debt."""

        if at is None:
            self._simulate_to(self._total - 1)
        else:
            self._simulate_to(self._ordinal(at))
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for retraction in self._retractions:
            if retraction.remaining != 0:
                totals[retraction.spool] += retraction.remaining
        return {
            "tool": self._tool,
            "mode": self._mode,
            "e": {tool: decimal_text(value) for tool, value in sorted(self._e.items())},
            "spools": {str(tool): spool for tool, spool in sorted(self._spools.items())},
            "outstanding": {
                spool: decimal_text(amount)
                for spool, amount in sorted(totals.items())
            },
        }

    def repaid(
        self,
        retraction: Retraction | RetractionId | str | int | CommandId,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        inclusive_end: bool = True,
    ) -> Decimal:
        """Amount of ``retraction`` actually repaid inside the window."""

        start_i, end_i = self._window(start, end, inclusive_end=inclusive_end)
        self._simulate_to(end_i)
        target = self._resolve_retraction(retraction)
        payments = list(self._payments_by_retraction[target.id])
        if not payments:
            for span in self._spans:
                payments = span.retraction_payments(self, target.spool, target.id.index)
                if payments:
                    break
        total = Decimal(0)
        for payment in payments:
            if payment.ordinal < start_i:
                continue
            if payment.ordinal > end_i:
                break
            total += payment.amount
        return total

    def first_repayment(
        self,
        retraction: Retraction | RetractionId | str | int | CommandId,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        inclusive_end: bool = True,
    ) -> CommandId | None:
        """Credential of the first repayment of ``retraction`` in the window."""

        start_i, end_i = self._window(start, end, inclusive_end=inclusive_end)
        self._simulate_to(end_i)
        target = self._resolve_retraction(retraction)
        payments = list(self._payments_by_retraction[target.id])
        if not payments:
            for span in self._spans:
                payments = span.retraction_payments(self, target.spool, target.id.index)
                if payments:
                    break
        for payment in payments:
            if payment.ordinal < start_i:
                continue
            if payment.ordinal > end_i:
                break
            return payment.credential
        return None

    def repayment(
        self,
        retraction: Retraction | RetractionId | str | int | CommandId,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        inclusive_end: bool = True,
    ) -> tuple[Decimal, CommandId | None]:
        """Convenience wrapper returning ``(repaid_amount, first_credential)``."""

        return (
            self.repaid(retraction, start, end, inclusive_end=inclusive_end),
            self.first_repayment(retraction, start, end, inclusive_end=inclusive_end),
        )

    def retraction_report(
        self,
        retraction: Retraction | RetractionId | str | int | CommandId,
        start: int | CommandId = 0,
        end: int | CommandId | None = None,
        *,
        inclusive_end: bool = True,
    ) -> dict[str, Any]:
        target_id = self._retraction_id(retraction)
        # Simulate the window first: that also builds compressed span records, so
        # a retraction living inside a skipped cycle can then be resolved lazily
        # instead of walking the whole stream.
        start_i, end_i = self._window(start, end, inclusive_end=inclusive_end)
        self._simulate_to(end_i)
        if not self._retraction_known(target_id):
            # The retraction may live beyond the window; walking the stream builds
            # the compressed spans that let us resolve it without a full flatten.
            self._simulate_to(self._total - 1)
        target = self._resolve_retraction(target_id)
        amount = self.repaid(target, start_i, end_i, inclusive_end=True)
        first = self.first_repayment(target, start_i, end_i, inclusive_end=True)
        return {
            "retraction": target.id.to_dict(),
            "amount": decimal_text(amount),
            "first_repayment": first.to_dict() if first is not None else None,
        }

    # -- simulation ------------------------------------------------------

    def _simulate_to(self, ordinal: int) -> None:
        """Advance the walker through ``ordinal`` inclusive.

        The walker keeps an explicit stack of "frames", so it can pause after any
        command and resume exactly where it stopped.  Within a repeat it may skip
        a range of iterations arithmetically when they are known to be neutral
        (see :meth:`_try_fast_forward`).
        """

        if ordinal < 0:
            raise IndexError("command ordinal cannot be negative")
        if ordinal >= self._total:
            raise IndexError(
                f"command ordinal {ordinal} outside stream 0..{self._total - 1}"
            )
        if self._simulated_through >= ordinal:
            return
        if self._walking:
            raise RuntimeError("re-entrant ledger simulation")

        if self._stack is None:
            self._stack = [self._make_frame(self.program.root, self.program.root.name or self.program.name, ())]
        self._walking = True
        try:
            while self._simulated_through < ordinal:
                if not self._stack:
                    raise IndexError("program ended before requested ordinal")
                frame = self._stack[-1]
                if frame.cursor >= len(frame.items):
                    self._stack.pop()
                    continue
                node = frame.items[frame.cursor]
                if isinstance(node, _CommandNode):
                    frame.cursor += 1
                    self._advance_node(node, frame.snippet_name, frame.prefix)
                elif isinstance(node, _HandoverNode):
                    frame.cursor += 1
                    self._apply_handover(node.tool, node.spool)
                else:
                    child = self.program.resolve_snippet(node.target)
                    per = child.command_count(self.program)
                    if per == 0:
                        frame.cursor += 1
                        continue
                    if self._try_fast_forward(frame, node, child, per, ordinal):
                        continue
                    index = frame.indices.get(frame.cursor, 0)
                    if index >= node.count:
                        frame.cursor += 1
                        continue
                    frame.indices[frame.cursor] = index + 1
                    child_name = child.name or "<anonymous>"
                    self._stack.append(
                        self._make_frame(
                            child, child_name, frame.prefix + ((child_name, index),)
                        )
                    )
        finally:
            self._walking = False

    def _advance_node(
        self,
        node: _CommandNode,
        snippet_name: str,
        prefix: tuple[tuple[str, int], ...],
    ) -> None:
        line = node.line
        ordinal = self._next_ordinal
        self._apply_timed(ordinal)
        self._next_ordinal += 1
        self._simulated_through = ordinal

        command = line.command
        if command == ("M", 82):
            self._mode = ABSOLUTE
            self._sensitive_commands += 1
        elif command == ("M", 83):
            self._mode = RELATIVE
        elif command == ("G", 92):
            self._sensitive_commands += 1
            if "E" in line.params:
                self._e[self._tool] = self._raw_decimal(line, "E")
        elif command[0] == "T" and command[1] is not None:
            self._tool = int(command[1])
        elif self._is_extrusion(line):
            credential = CommandId(
                ordinal=ordinal,
                source=SourceRef(snippet_name, node.source_line, line.command_index),
                iteration=prefix,
                text=line.gcode_str,
            )
            self._credentials[ordinal] = credential
            self._apply_extrusion(line, credential)

    @staticmethod
    def _is_extrusion(line: GcodeLine) -> bool:
        return (
            line.command[0] == "G"
            and line.command[1] in (0, 1, 2, 3)
            and "E" in line.params
        )

    def _make_frame(
        self,
        snippet: Snippet,
        snippet_name: str,
        prefix: tuple[tuple[str, int], ...],
    ) -> "_Frame":
        return _Frame(
            items=snippet._compile(),
            cursor=0,
            snippet_name=snippet_name,
            prefix=prefix,
        )

    def _try_fast_forward(
        self,
        frame: "_Frame",
        node: _RepeatNode,
        child: Snippet,
        per: int,
        target: int,
    ) -> bool:
        """Skip a stable run of repeat iterations in one arithmetic step.

        Two consecutive iterations are simulated on a sandbox copy of the ledger.
        If both release the same material at the same offsets and end in the same
        machine state, every further such iteration repeats that pattern, so the
        safe ones can be recorded as a span instead of being visited.
        """

        start_index = frame.indices.get(frame.cursor, 0)
        remaining_iterations = node.count - start_index
        if remaining_iterations <= 2 or per < 1:
            return False
        block_start = self._next_ordinal
        skip = (target - block_start) // per
        skip = min(skip, remaining_iterations - 1)
        if skip <= 0:
            return False
        skip_end = block_start + per * skip
        if any(block_start <= position < skip_end for position in self.timed_handovers):
            return False

        plan = self._periodic_plan(child, per)
        if plan is None:
            return False
        events, end_state, released_delta, units, e_delta = plan
        if not events and not units:
            return False

        live_released = dict(self._released)
        base_indices: dict[str, int] = {}
        for _, spool, _, _ in units:
            base_indices.setdefault(spool, self._retraction_counters[spool])
        self._record_span(
            step=per,
            count=skip,
            base=block_start,
            events=events,
            retractions=units,
            retraction_base=tuple(sorted(base_indices.items())),
        )
        live_e = dict(self._e)
        live_counters = dict(self._retraction_counters)
        live_debt = {spool: deque(queue) for spool, queue in self._debt.items()}
        live_spools = dict(self._spools)
        live_mode = self._mode
        live_tool = self._tool
        e_delta = plan[4]
        self._restore_state(end_state)
        self._spools = live_spools
        self._mode = live_mode
        self._tool = live_tool
        # Restore the open-debt queues to the live position; for a self-contained
        # cycle they are empty on both sides, so the probe's empty queues suffice.
        self._debt = defaultdict(deque)
        for spool, queue in live_debt.items():
            self._debt[spool] = deque(queue)
        self._e = defaultdict(Decimal, live_e)
        for tool, delta in e_delta.items():
            self._e[tool] = live_e.get(tool, Decimal(0)) + delta * skip
        self._retraction_counters = defaultdict(int, live_counters)
        for spool in {unit_spool for _, unit_spool, _, _ in units}:
            per_cycle = sum(1 for _, unit_spool, _, _ in units if unit_spool == spool)
            self._retraction_counters[spool] += per_cycle * skip
        self._released = defaultdict(Decimal)
        for spool, value in live_released.items():
            self._released[spool] = value
        for spool, delta in released_delta.items():
            self._released[spool] += delta * skip
        self._next_ordinal += per * skip
        self._simulated_through = self._next_ordinal - 1
        frame.indices[frame.cursor] = start_index + skip
        return True

    def _periodic_plan(
        self, child: Snippet, per: int
    ) -> tuple[tuple[tuple[int, str, Decimal], ...], dict[str, Any], dict[str, Decimal]] | None:
        """Simulate two iterations and return ``(events, end_state, delta)``.

        The first probe runs on a clone with full accounting; the second runs on a
        clone whose event lists were emptied first, so its releases can be read
        directly and compared offset-by-offset with the first.
        """

        first_before = self._next_ordinal
        first_probe = self._fresh_probe()
        first_probe._walk_iteration(child, per)
        first_state = first_probe._period_snapshot()
        first_events = tuple(
            (event.ordinal - first_before, event.spool, event.amount)
            for event in first_probe._new_events
        )

        second_before = first_probe._next_ordinal
        second_probe = first_probe._fresh_probe()
        second_probe._walk_iteration(child, per)
        second_state = second_probe._period_snapshot()
        second_events = tuple(
            (event.ordinal - second_before, event.spool, event.amount)
            for event in second_probe._new_events
        )

        if first_events != second_events:
            return None

        self_contained = not first_state["debt"] and not second_state["debt"]
        if not self_contained:
            if not self._states_periodic(first_state, second_state):
                return None
        delta: dict[str, Decimal] = {}
        for spool, value in second_state["released"].items():
            before = first_state["released"].get(spool, Decimal(0))
            amount = value - before
            if amount < 0:
                return None
            delta[spool] = amount
        units = self._retraction_units(second_probe, second_before, per)
        e_delta = {
            tool: second_state["e"].get(tool, Decimal(0))
            - first_state["e"].get(tool, Decimal(0))
            for tool in set(second_state["e"]) | set(first_state["e"])
        }
        return second_events, second_state, delta, units, e_delta

    def _retraction_units(
        self, probe: "MaterialLedger", iteration_start: int, per: int
    ) -> tuple[tuple[int, str, Decimal, tuple[tuple[int, Decimal], ...]], ...]:
        """Describe retractions opened in one self-contained iteration."""

        suffix = tuple(
            retraction
            for retraction in probe._segment_retractions
            if retraction.origin.ordinal >= iteration_start
        )
        payments: dict[RetractionId, list[tuple[int, Decimal]]] = defaultdict(list)
        for payment in probe._segment_payments:
            payments[payment.retraction].append(
                (payment.ordinal - iteration_start, payment.amount)
            )
        return tuple(
            (
                retraction.origin.ordinal - iteration_start,
                retraction.spool,
                retraction.amount,
                tuple(payments.get(retraction.id, ())),
            )
            for retraction in suffix
        )

    def _fresh_probe(self) -> "MaterialLedger":
        probe = self._clone()
        probe._new_events = []
        probe._payments = []
        probe._retractions = []
        probe._retractions_by_id = {}
        probe._retraction_by_origin = {}
        probe._payments_by_retraction = defaultdict(list)
        probe._segment_retractions = []
        probe._segment_payments = []
        return probe

    def _states_periodic(self, before: dict[str, Any], after: dict[str, Any]) -> bool:
        """True when one more iteration cannot change the accounting state."""

        if before["mode"] != after["mode"] or before["tool"] != after["tool"]:
            return False
        if before["spools"] != after["spools"]:
            return False
        if before["debt"] != after["debt"]:
            return False
        if before["counters"] != after["counters"]:
            return False
        return before["e"] == after["e"]

    def _walk_iteration(self, child: Snippet, per: int) -> None:
        """Advance the (sandbox) ledger through exactly one iteration of ``child``."""

        target = self._next_ordinal + per - 1
        if self._stack is None:
            self._stack = []
        self._stack.append(self._make_frame(child, child.name or "<anonymous>", ()))
        self._walking = True
        try:
            while self._simulated_through < target:
                if not self._stack:
                    raise IndexError("iteration ended early")
                frame = self._stack[-1]
                if frame.cursor >= len(frame.items):
                    self._stack.pop()
                    continue
                item = frame.items[frame.cursor]
                if isinstance(item, _CommandNode):
                    frame.cursor += 1
                    self._advance_node(item, frame.snippet_name, frame.prefix)
                elif isinstance(item, _HandoverNode):
                    frame.cursor += 1
                    self._apply_handover(item.tool, item.spool)
                else:
                    nested = self.program.resolve_snippet(item.target)
                    index = frame.indices.get(frame.cursor, 0)
                    if index >= item.count:
                        frame.cursor += 1
                        continue
                    frame.indices[frame.cursor] = index + 1
                    self._stack.append(
                        self._make_frame(
                            nested,
                            nested.name or "<anonymous>",
                            frame.prefix + ((nested.name or "<anonymous>", index),),
                        )
                    )
        finally:
            self._walking = False

    def _period_snapshot(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "tool": self._tool,
            "e": dict(self._e),
            "spools": dict(self._spools),
            "debt": {
                spool: tuple(item.remaining for item in queue)
                for spool, queue in self._debt.items()
                if any(item.remaining for item in queue)
            },
            "counters": dict(self._retraction_counters),
            "released": dict(self._released),
            "sensitive": self._sensitive_commands,
        }

    def _restore_state(self, snapshot: dict[str, Any]) -> None:
        self._mode = snapshot["mode"]
        self._tool = snapshot["tool"]
        self._e = defaultdict(Decimal, snapshot["e"])
        self._spools = dict(snapshot["spools"])
        self._debt = defaultdict(deque)
        for spool, remaining in snapshot["debt"].items():
            self._debt[spool] = deque(
                Retraction(
                    id=RetractionId(spool, index),
                    spool=spool,
                    amount=amount,
                    origin=CommandId(ordinal=-1),
                    remaining=amount,
                )
                for index, amount in enumerate(remaining)
            )
        self._retraction_counters = defaultdict(int, snapshot["counters"])
        self._released = defaultdict(Decimal, snapshot["released"])
        self._sensitive_commands = snapshot.get("sensitive", 0)

    def _clone(self) -> "MaterialLedger":
        clone = copy.copy(self)
        clone._e = defaultdict(Decimal, self._e)
        clone._spools = dict(self._spools)
        clone._debt = defaultdict(deque)
        retraction_copies: dict[RetractionId, Retraction] = {}
        for spool, queue in self._debt.items():
            copied = deque()
            for retraction in queue:
                duplicate = Retraction(
                    id=retraction.id,
                    spool=retraction.spool,
                    amount=retraction.amount,
                    origin=retraction.origin,
                    remaining=retraction.remaining,
                )
                retraction_copies[duplicate.id] = duplicate
                copied.append(duplicate)
            clone._debt[spool] = copied
        clone._retraction_counters = defaultdict(int, self._retraction_counters)
        clone._released = defaultdict(Decimal, self._released)
        clone._new_events = list(self._new_events)
        clone._payments = list(self._payments)
        clone._retractions = [
            retraction_copies.get(item.id, item) for item in self._retractions
        ]
        clone._retractions_by_id = dict(retraction_copies)
        clone._retraction_by_origin = dict(self._retraction_by_origin)
        clone._payments_by_retraction = defaultdict(list)
        for key, value in self._payments_by_retraction.items():
            clone._payments_by_retraction[key] = list(value)
        clone._segment_retractions = []
        clone._segment_payments = []
        clone._credentials = dict(self._credentials)
        clone._stack = None
        clone._spans = list(self._spans)
        clone._applied_timed = set(self._applied_timed)
        return clone

    def _record_span(
        self,
        *,
        step: int,
        count: int,
        base: int,
        events: tuple[tuple[int, str, Decimal], ...],
        retractions: tuple[
            tuple[int, str, Decimal, tuple[tuple[int, Decimal], ...]], ...
        ] = (),
        retraction_base: tuple[tuple[str, int], ...] = (),
    ) -> None:
        self._spans.append(
            _Span(
                step=step,
                count=count,
                base=base,
                events=events,
                retractions=retractions,
                retraction_base=retraction_base,
            )
        )

    def _apply_timed(self, ordinal: int) -> None:
        if ordinal in self._applied_timed:
            return
        self._applied_timed.add(ordinal)
        for handover in self.timed_handovers.get(ordinal, ()):
            self._apply_handover(handover.tool, handover.spool)

    def _apply_handover(self, tool: int, spool: str) -> None:
        self._known_spools.add(spool)
        self._spools[int(tool)] = str(spool)

    def _raw_decimal(self, line: GcodeLine, param: str) -> Decimal:
        text = line.param_text.get(param)
        if text is None or text == "":
            return as_decimal(line.params[param])  # type: ignore[arg-type]
        return Decimal(text)

    def _apply_extrusion(self, line: GcodeLine, credential: CommandId) -> None:
        value = self._raw_decimal(line, "E")
        tool = self._tool
        if self._mode == ABSOLUTE:
            delta = value - self._e[tool]
            self._e[tool] = value
        else:
            delta = value
            self._e[tool] = self._e[tool] + value

        if delta == 0:
            return
        spool = self._spools.get(tool)
        if spool is None:
            raise ValueError(
                f"tool {tool} is not connected to a spool at command {credential}"
            )
        if delta < 0:
            self._open_retraction(spool, -delta, credential)
        else:
            self._consume_positive(spool, delta, credential)

    def _open_retraction(
        self, spool: str, amount: Decimal, credential: CommandId
    ) -> Retraction:
        index = self._retraction_counters[spool]
        self._retraction_counters[spool] = index + 1
        retraction = Retraction(
            id=RetractionId(spool, index),
            spool=spool,
            amount=amount,
            origin=credential,
            remaining=amount,
        )
        self._debt[spool].append(retraction)
        self._retractions.append(retraction)
        self._retractions_by_id[retraction.id] = retraction
        self._retraction_by_origin[credential.ordinal] = retraction
        self._segment_retractions.append(retraction)
        return retraction

    def _consume_positive(
        self,
        spool: str,
        amount: Decimal,
        credential: CommandId,
    ) -> None:
        """Repay debt in material order, then record remaining new material.

        ``released`` is the cumulative material released by this tool up to and
        including this command; ``amount`` is what this command adds.  A single
        command can therefore finish an older retraction *and* start new material.
        Both parts are recorded against the same credential, in material order.
        """

        start = self._released[spool]
        released = start + amount
        queue = self._debt[spool]
        part_index = 0
        consumed = Decimal(0)
        while queue and consumed < amount:
            retraction = queue[0]
            boundary = start + consumed + retraction.remaining
            if boundary <= released:
                payment = retraction.remaining
                retraction.remaining = Decimal(0)
                queue.popleft()
            else:
                payment = released - (start + consumed)
                retraction.remaining -= payment
            if payment > 0:
                record = Payment(
                    retraction=retraction.id,
                    amount=payment,
                    credential=credential,
                    part_index=part_index,
                )
                self._payments.append(record)
                self._payments_by_retraction[retraction.id].append(record)
                self._segment_payments.append(record)
                consumed += payment
                part_index += 1
            elif payment <= 0:
                break

        self._released[spool] = released
        if consumed < amount:
            event = NewUsage(
                spool=spool,
                amount=amount - consumed,
                credential=credential,
                part_index=part_index,
            )
            self._new_events.append(event)

    # -- helpers ---------------------------------------------------------

    def _ordinal(self, value: int | CommandId | ExecutedCommand) -> int:
        if isinstance(value, bool):
            raise TypeError("a command ordinal must be an int, not bool")
        if isinstance(value, int):
            return value
        if isinstance(value, CommandId):
            return value.ordinal
        if isinstance(value, ExecutedCommand):
            return value.id.ordinal
        ordinal = getattr(value, "ordinal", None)
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            return ordinal
        raise TypeError(f"cannot interpret {value!r} as a command position")

    def _window(
        self,
        start: int | CommandId,
        end: int | CommandId | None,
        *,
        inclusive_end: bool,
    ) -> tuple[int, int]:
        start_i = self._ordinal(start)
        end_i = self._total - 1 if end is None else self._ordinal(end)
        if end_i < start_i:
            raise ValueError("window end precedes start")
        if start_i < 0 or end_i >= self._total:
            raise IndexError(
                f"window [{start_i}, {end_i}] outside stream 0..{self._total - 1}"
            )
        if not inclusive_end:
            end_i -= 1
        return start_i, end_i

    def _retraction_known(self, target: RetractionId) -> bool:
        if target in self._retractions_by_id:
            return True
        for span in self._spans:
            if span.retraction(self, target.spool, target.index) is not None:
                return True
        return False

    def _retraction_id(self, query: Retraction | RetractionId | str | int | CommandId) -> RetractionId:
        if isinstance(query, Retraction):
            return query.id
        if isinstance(query, RetractionId):
            return query
        if isinstance(query, str):
            spool, _, index_text = query.rpartition(":")
            if not spool or not index_text:
                raise ValueError(f"cannot parse retraction identifier {query!r}")
            return RetractionId(spool, int(index_text))
        if isinstance(query, (int, CommandId, ExecutedCommand)):
            ordinal = self._ordinal(query)
            self._simulate_to(ordinal)
            try:
                return self._retraction_by_origin[ordinal].id
            except KeyError as exc:
                raise KeyError(f"no retraction was opened by command {ordinal}") from exc
        raise TypeError(f"cannot interpret {query!r} as a retraction")

    def _resolve_retraction(
        self, query: Retraction | RetractionId | str | int | CommandId
    ) -> Retraction:
        if isinstance(query, Retraction):
            return query
        if isinstance(query, RetractionId):
            if query in self._retractions_by_id:
                return self._retractions_by_id[query]
            for span in self._spans:
                retraction = span.retraction(self, query.spool, query.index)
                if retraction is not None:
                    return retraction
            self._seek_retraction(query)
            return self._retractions_by_id[query]
        if isinstance(query, str):
            spool, _, index_text = query.rpartition(":")
            if not spool or not index_text:
                raise ValueError(f"cannot parse retraction identifier {query!r}")
            return self._resolve_retraction(RetractionId(spool, int(index_text)))
        if isinstance(query, (int, CommandId, ExecutedCommand)):
            ordinal = self._ordinal(query)
            self._simulate_to(ordinal)
            try:
                return self._retraction_by_origin[ordinal]
            except KeyError as exc:
                raise KeyError(f"no retraction was opened by command {ordinal}") from exc
        raise TypeError(f"cannot interpret {query!r} as a retraction")

    def _seek_retraction(self, target: RetractionId) -> None:
        """Advance the cursor until ``target`` has been opened (if it exists)."""

        while target not in self._retractions_by_id:
            if self._simulated_through + 1 >= self._total:
                raise KeyError(f"unknown retraction {target}")
            self._simulate_to(self._simulated_through + 1)


Ledger = MaterialLedger


def analyze(
    program: PrintProgram | Snippet | str,
    *,
    initial_spools: Mapping[int | str, str] | None = None,
    initial_tool: int = 0,
    mode: str = ABSOLUTE,
    initial_e: Mapping[int | str, int | float | str | Decimal] | None = None,
    timed_handovers: Mapping[int | CommandId, Iterable[Handover | tuple[int, str]]]
    | None = None,
) -> MaterialLedger:
    """Shortcut for building a :class:`MaterialLedger`."""

    return MaterialLedger(
        program,
        initial_spools=initial_spools,
        initial_tool=initial_tool,
        mode=mode,
        initial_e=initial_e,
        timed_handovers=timed_handovers,
    )


# ---------------------------------------------------------------------------
# JSON command line interface
# ---------------------------------------------------------------------------


def _run_queries(ledger: MaterialLedger, queries: Sequence[Mapping[str, Any]]) -> list[Any]:
    results: list[Any] = []
    for query in queries:
        kind = query.get("type", "usage")
        start = query.get("start", 0)
        end = query.get("end")
        inclusive = bool(query.get("inclusive_end", True))
        if kind == "usage":
            results.append(ledger.window(start, end, inclusive_end=inclusive).to_dict())
        elif kind == "first_new":
            credential = ledger.first_new_usage(
                start, end, spool=query.get("spool"), inclusive_end=inclusive
            )
            results.append(
                {"first_new_usage": credential.to_dict() if credential else None}
            )
        elif kind in ("retraction", "repayment"):
            target = query.get("retraction")
            if target is None:
                raise ValueError("retraction query needs a 'retraction' identifier")
            results.append(
                ledger.retraction_report(target, start, end, inclusive_end=inclusive)
            )
        elif kind == "credential":
            results.append(ledger.credential(start).to_dict())
        else:
            raise ValueError(f"unknown query type {kind!r}")
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gcodeparser.reconcile",
        description="Reconcile material usage from a nested G-code program",
    )
    parser.add_argument("program", help="program JSON specification")
    parser.add_argument(
        "--queries", help="JSON file with window queries; defaults to stdin"
    )
    parser.add_argument(
        "-o", "--output", default="-", help="output JSON path (default: stdout)"
    )
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.program).read_text())
    program = PrintProgram.from_spec(spec)
    initial = spec.get("initial", {}) or {}
    ledger = MaterialLedger(
        program,
        initial_spools=initial.get("spools"),
        initial_tool=int(initial.get("tool", 0)),
        mode=initial.get("mode", ABSOLUTE),
        initial_e=initial.get("e"),
        timed_handovers=initial.get("timed_handovers"),
    )

    query_data = json.loads(Path(args.queries).read_text()) if args.queries else json.load(sys.stdin)
    queries = query_data.get("queries", []) if isinstance(query_data, dict) else query_data
    result = _run_queries(ledger, queries)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
