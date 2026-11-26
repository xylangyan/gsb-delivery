"""Reconstruct archived commands from paper slips and archived receipts.

The original command lines are lost. What remains for every batch slip is:

* the command's docopt-ng usage description (per-slip settings such as
  ``options_first`` and option defaults included),
* an archived receipt: the ordered field values that were booked,
* the raw argument tokens that the paper form allowed, grouped into a small
  number of candidate commands.

Candidates are fed through the *original* docopt-ng machinery one at a time,
exactly as the operator would have typed them. Slot combinations are never
expanded: a slip with dozens of optional, same-source or mutually exclusive
slots only costs as many parses as there are candidate commands.

A candidate only counts when a single usage route consumes the whole command.
The field order that is reported belongs to that receiving route alone; routes
that merely accept some of the tokens are used for nothing but rejection.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from docopt import (
    DocoptExit,
    DocoptLanguageError,
    _Argument,
    _BranchPattern,
    _Either,
    _LeafPattern,
    _Option,
    _OptionsShortcut,
    _Tokens,
    _formal_usage,
    _parse_docstring_sections,
    _parse_longer,
    _parse_options,
    _parse_pattern,
    _parse_shorts,
)

__all__ = [
    "Slip",
    "FieldEntry",
    "CommandMatch",
    "ReconcileResult",
    "ReconcileError",
    "reconcile",
    "reconcile_many",
    "load_slip",
]


class ReconcileError(ValueError):
    """A batch slip itself is malformed and cannot be reconciled."""


@dataclass(frozen=True)
class Slip:
    """One archived batch slip.

    Attributes
    ----------
    name:
        Identifier of the slip, used on the report.
    doc:
        The original command's docopt-ng docstring. Long-option abbreviation,
        positional handling and ``[default: ...]`` values all come from here.
    receipt:
        The archived field sequence, in booked order.
    candidate_argv:
        Raw argument vectors allowed by the paper form. Each entry is one
        complete candidate command; entries are tried in this order.
    field_options:
        Canonical option names (``--field`` or ``-f``) that count as booked
        fields. Defaults to every value-taking option declared in the slip.
    options_first:
        The slip's own ``options_first`` setting, passed straight to docopt-ng.
    """

    name: str
    doc: str
    receipt: list[str]
    candidate_argv: list[list[str]]
    field_options: list[str] | None = None
    options_first: bool = False


@dataclass(frozen=True)
class FieldEntry:
    """One field slot as consumed by the receiving usage route."""

    slot: str
    option: str
    slot_index: int
    value: str
    origin: str
    argv_index: int | None
    raw_token: str | None

    def identity(self) -> tuple[str, str, int, str, str]:
        """Slot-level identity, independent of the concrete command tokens.

        Two commands book the same fields only when the same route slots take
        the same values from the same source (argv or a default). The raw
        argv index is deliberately excluded: it identifies the typing, not
        the booked field.
        """

        return (self.slot, self.option, self.slot_index, self.value, self.origin)

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "option": self.option,
            "slot_index": self.slot_index,
            "value": self.value,
            "origin": self.origin,
            "argv_index": self.argv_index,
            "raw_token": self.raw_token,
        }


@dataclass(frozen=True)
class CommandMatch:
    """A candidate command that reproduces the archived receipt."""

    command: list[str]
    command_string: str
    field_order: list[str]
    fields: list[FieldEntry]
    receiving_route: list[str]

    def identity(self) -> tuple[tuple[str, str, int, str, str], ...]:
        """The booked fields as route slots, independent of the raw typing."""

        return tuple(entry.identity() for entry in self.fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "command_string": self.command_string,
            "receiving_route": self.receiving_route,
            "field_order": self.field_order,
            "fields": [entry.as_dict() for entry in self.fields],
        }


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of reconciling one slip."""

    slip: str
    status: str
    match: CommandMatch | None
    alternate: CommandMatch | None
    inconsistent_fields: list[dict[str, Any]]
    attempts: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "slip": self.slip,
            "status": self.status,
            "match": None if self.match is None else self.match.as_dict(),
            "alternate": None if self.alternate is None else self.alternate.as_dict(),
            "inconsistent_fields": self.inconsistent_fields,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class _PreparedSlip:
    name: str
    routes: list[_BranchPattern]
    options: list[_Option]
    field_names: set[str]
    options_first: bool


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def _parse_argv_with_origins(
    argv: list[str], options: list[_Option], options_first: bool
) -> tuple[list[_LeafPattern], dict[int, tuple[int, str]]]:
    """Parse argv once, tracking which argv token made every option leaf.

    The dispatch loop mirrors :func:`docopt._parse_argv` exactly (including
    numeric tokens, ``--`` and ``options_first``), while each option token is
    still split by the library's own :func:`_parse_longer` /
    :func:`_parse_shorts`. That keeps clustered short options and value
    swallowing identical to the original parser, and lets every produced leaf
    be charged to the single argv token that produced it.
    """

    tokens = _Tokens(argv)
    parsed: list[_LeafPattern] = []
    origins: dict[int, tuple[int, str]] = {}
    current = tokens.current()
    while current is not None:
        argv_index = len(argv) - len(tokens)
        raw_token = current
        if current == "--":
            parsed.extend(_Argument(None, value) for value in tokens)
            break
        if current.startswith("--"):
            leaves = _parse_longer(tokens, options, argv=True)
        elif (
            current.startswith("-")
            and current != "-"
            and not _is_number(current)
        ):
            leaves = _parse_shorts(tokens, options)
        elif options_first:
            parsed.extend(_Argument(None, value) for value in tokens)
            break
        else:
            parsed.append(_Argument(None, tokens.move()))
            current = tokens.current()
            continue
        for leaf in leaves:
            origins[id(leaf)] = (argv_index, raw_token)
        parsed.extend(leaves)
        current = tokens.current()
    return parsed, origins


def _canonical_option_names(options: list[_Option]) -> dict[str, str]:
    names: dict[str, str] = {}
    for option in options:
        canonical = option.name
        if canonical is None:
            continue
        names[canonical] = canonical
        if option.short is not None:
            names[option.short] = canonical
        if option.longer is not None:
            names[option.longer] = canonical
    return names


def _usage_routes(pattern: _BranchPattern) -> list[_BranchPattern]:
    """The one-route-per-usage-line branches of the original pattern tree.

    Unlike :func:`docopt._transform`, this keeps the original ``_Either`` /
    ``_NotRequired`` / ``_OneOrMore`` structure, so optional slots and nested
    mutually exclusive branches keep their real matching semantics. A usage
    message with a single line yields that one route directly.
    """

    for child in pattern.flat(_Either):
        return list(child.children)
    return [pattern]


def _prepare(slip: Slip) -> _PreparedSlip:
    try:
        sections = _parse_docstring_sections(slip.doc)
    except DocoptLanguageError as exc:
        raise ReconcileError(f"slip {slip.name!r}: {exc}") from exc
    DocoptExit.usage = sections.usage_header + sections.usage_body
    options = [
        *_parse_options(sections.before_usage),
        *_parse_options(sections.after_usage),
    ]
    try:
        pattern = _parse_pattern(_formal_usage(sections.usage_body), options)
    except DocoptLanguageError as exc:
        raise ReconcileError(f"slip {slip.name!r}: {exc}") from exc
    pattern_options = set(pattern.flat(_Option))
    for shortcut in pattern.flat(_OptionsShortcut):
        shortcut.children = [
            option for option in options if option not in pattern_options
        ]
    fixed = pattern.fix()
    routes = _usage_routes(fixed)

    aliases = _canonical_option_names(options)
    if slip.field_options is None:
        field_names = {
            option.name
            for option in options
            if option.argcount == 1 and option.name is not None
        }
    else:
        field_names = set()
        for requested in slip.field_options:
            canonical = aliases.get(requested)
            if canonical is None:
                raise ReconcileError(
                    f"slip {slip.name!r}: field option {requested!r} "
                    "is not declared in the docstring"
                )
            field_names.add(canonical)
    return _PreparedSlip(
        name=slip.name,
        routes=routes,
        options=options,
        field_names=field_names,
        options_first=slip.options_first,
    )


def _parse_candidate(argv: list[str] | str) -> list[str]:
    if isinstance(argv, str):
        return shlex.split(argv)
    return list(argv)


def _route_signature(route: _BranchPattern) -> list[str]:
    return [leaf.name or "?" for leaf in route.flat()]


def _receiving_route(
    routes: list[_BranchPattern], parsed: list[_LeafPattern]
) -> _BranchPattern | None:
    """Return the first usage route that consumes the whole command.

    A route that matches but leaves tokens behind does not receive the command
    and is ignored, no matter how many of the tokens it happens to accept.
    """

    for route in routes:
        matched, left, _ = route.match(parsed, [])
        if matched and not left:
            return route
    return None


def _booked_values(
    route: _BranchPattern,
    parsed: list[_LeafPattern],
    field_names: set[str],
    origins: dict[int, tuple[int, str]],
) -> list[FieldEntry]:
    """Field values booked through the receiving route, in route slot order.

    docopt-ng exposes one key per option name: repeated / ``...`` slots with
    the same name accumulate supplied values in argv order into a single list,
    while an unsupplied option falls back to its own ``[default: ...]`` value.
    The two sources never mix, which is exactly what is reproduced here.
    """

    supplied: dict[str, list[_Option]] = {}
    for leaf in parsed:
        if isinstance(leaf, _Option) and leaf.name in field_names:
            supplied.setdefault(leaf.name, []).append(leaf)

    defaults: dict[str, Any] = {}
    ordered_names: list[str] = []
    for leaf in route.flat(_Option):
        if leaf.name not in field_names or leaf.name in ordered_names:
            continue
        ordered_names.append(leaf.name)
        defaults[leaf.name] = leaf.value

    entries: list[FieldEntry] = []
    for name in ordered_names:
        given = supplied.get(name, [])
        if given:
            for slot_index, source_leaf in enumerate(given):
                argv_index, raw_token = origins.get(
                    id(source_leaf), (None, None)
                )
                entries.append(
                    FieldEntry(
                        slot=f"{name}[{slot_index}]",
                        option=name,
                        slot_index=slot_index,
                        value=str(source_leaf.value),
                        origin="argv",
                        argv_index=argv_index,
                        raw_token=raw_token,
                    )
                )
            continue
        value = defaults.get(name)
        if isinstance(value, list):
            default_values = list(value)
        elif value is None:
            default_values = []
        else:
            default_values = [value]
        for slot_index, default in enumerate(default_values):
            entries.append(
                FieldEntry(
                    slot=f"{name}[{slot_index}]",
                    option=name,
                    slot_index=slot_index,
                    value=str(default),
                    origin="default",
                    argv_index=None,
                    raw_token=None,
                )
            )
    return entries


def _evaluate(
    prepared: _PreparedSlip, argv: list[str]
) -> tuple[CommandMatch | None, str]:
    working_options = list(prepared.options)
    try:
        parsed, origins = _parse_argv_with_origins(
            argv, working_options, prepared.options_first
        )
    except DocoptExit as exc:
        return None, f"rejected while tokenizing: {str(exc).splitlines()[0]}"
    except DocoptLanguageError as exc:
        return None, f"ambiguous option: {exc}"
    route = _receiving_route(prepared.routes, parsed)
    if route is None:
        return None, "no usage route consumes the whole command"
    fields = _booked_values(route, parsed, prepared.field_names, origins)
    match = CommandMatch(
        command=argv,
        command_string=shlex.join(argv),
        field_order=[entry.value for entry in fields],
        fields=fields,
        receiving_route=_route_signature(route),
    )
    return match, "ok"


def _diff_fields(first: CommandMatch, other: CommandMatch) -> list[dict[str, Any]]:
    """Slot-level differences between two otherwise receipt-matching commands."""

    left = {entry.identity(): entry for entry in first.fields}
    right = {entry.identity(): entry for entry in other.fields}
    diff: list[dict[str, Any]] = []
    for identity in sorted(set(left) | set(right), key=lambda item: item[2]):
        if identity in left and identity in right:
            continue
        source = left.get(identity) or right.get(identity)
        assert source is not None
        present_in = "matched_command" if identity in left else "alternate_command"
        diff.append(
            {
                "slot": source.slot,
                "option": source.option,
                "value": source.value,
                "origin": source.origin,
                "present_in": present_in,
                "matched_command": first.command_string,
                "alternate_command": other.command_string,
            }
        )
    return diff


def reconcile(slip: Slip) -> ReconcileResult:
    """Reconcile one slip against its candidate commands."""

    if not slip.candidate_argv:
        raise ReconcileError(f"slip {slip.name!r}: no candidate commands supplied")
    prepared = _prepare(slip)
    receipt = list(slip.receipt)
    attempts: list[dict[str, Any]] = []
    matches: list[CommandMatch] = []
    for raw_argv in slip.candidate_argv:
        argv = _parse_candidate(raw_argv)
        match, reason = _evaluate(prepared, argv)
        if match is None:
            attempts.append(
                {"command": shlex.join(argv), "accepted": False, "reason": reason}
            )
            continue
        attempts.append(
            {
                "command": match.command_string,
                "accepted": True,
                "field_order": match.field_order,
                "receipt_matches": match.field_order == receipt,
            }
        )
        if match.field_order == receipt:
            matches.append(match)

    if not matches:
        return ReconcileResult(
            slip=slip.name,
            status="no_match",
            match=None,
            alternate=None,
            inconsistent_fields=[],
            attempts=attempts,
        )

    match = matches[0]
    alternate = next(
        (
            candidate
            for candidate in matches[1:]
            if candidate.identity() != match.identity()
        ),
        None,
    )
    status = "ambiguous" if alternate is not None else "unique"
    inconsistent = [] if alternate is None else _diff_fields(match, alternate)
    return ReconcileResult(
        slip=slip.name,
        status=status,
        match=match,
        alternate=alternate,
        inconsistent_fields=inconsistent,
        attempts=attempts,
    )


def reconcile_many(slips: list[Slip]) -> list[ReconcileResult]:
    """Reconcile a batch of small slips, each fed through the library on its own."""

    return [reconcile(slip) for slip in slips]


def load_slip(payload: dict[str, Any]) -> Slip:
    """Build a :class:`Slip` from a plain JSON-like mapping."""

    try:
        name = payload["name"]
        doc = payload["doc"]
        receipt = list(payload["receipt"])
        candidates = payload["candidate_argv"]
    except KeyError as missing:
        raise ReconcileError(
            f"slip payload is missing key {missing.args[0]!r}"
        ) from None
    return Slip(
        name=str(name),
        doc=doc,
        receipt=[str(value) for value in receipt],
        candidate_argv=[_parse_candidate(candidate) for candidate in candidates],
        field_options=payload.get("field_options"),
        options_first=bool(payload.get("options_first", False)),
    )
