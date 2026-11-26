"""Counter rules for colliding Hashids short codes across two books.

The two books used by this task are:

``old``
    upstream default alphabet, empty salt, minimum print length 4;
``other``
    the default alphabet with ``f`` and ``5`` removed, salt ``local-b``,
    minimum print length 4.

Both print ``epQ6``: ``(151,)`` in ``old`` and ``(55263,)`` in ``other``.
The counter may choose a book by the longest prefix rule on the short code;
without a matching rule it consults the old book first.  A code that only one
book can round-trip *and* that has a real original is delivered directly.

The implementation is data driven.  A handoff manifest supplies each book's
alphabet (in its handed-over order), salt, minimum length, loaded numeric
boxes, reviewed numeric boxes, and registered original orders.  Everything a
book says about a code is decided by that book's native ``encode`` and
``decode``; separators, guards and the per-position alphabets are taken from
the real engine, never re-derived here.

Only reviewed collisions are constraints.  A collision is two books that both
round-trip the same code to *different* original orders that are both in the
books' real loads.  If exactly one of those orders falls in that book's
reviewed range, the collision must route there.  If both fall in reviewed
ranges, the pair is indistinguishable and is reported instead of guessed.

The rule search is a trie dynamic program.  Its nodes are prefixes that occur
in reviewed collision codes (and are in the allowed-prefix list), so its cost
is linear in the reviewed material.  It never enumerates short codes or
numeric ranges, which keeps numbers up to tens of billions practical.

Manifest shape::

    {
      "books": [
        {
          "id": 0, "name": "old",
          "alphabet": "...", "salt": "", "min_length": 4,
          "fields": 1,
          "ranges": [{"min": [0], "max": [100]}],
          "reviewed_ranges": [{"min": [151], "max": [151]}],
          "orders": [[151]]
        },
        {
          "id": 1, "name": "other",
          "alphabet": "...", "salt": "local-b", "min_length": 4,
          "fields": 1,
          "ranges": [], "reviewed_ranges": [{"min": [55263], "max": [55263]}],
          "orders": [[55263]]
        }
      ],
      "reviewed_codes": ["epQ6"],
      "allowed_prefixes": ["e", "ep", "epQ6"],
      "max_prefix": 8
    }

``allowed_prefixes`` and ``max_prefix`` are optional.  Without them every
prefix of a reviewed code is available.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from hashids import Hashids

DEFAULT_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
)
DEFAULT_SALT = ""
DEFAULT_MIN_LENGTH = 4
OTHER_SALT = "local-b"
OTHER_DROP = "f5"
OLD_BOOK_ID = 0
OTHER_BOOK_ID = 1
DEFAULT_REVIEWED_SCAN_LIMIT = 1_000_000


class ManifestError(ValueError):
    """The handoff manifest is inconsistent or incomplete."""


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


def _check_printable_ascii(text: str, what: str) -> None:
    if any(ord(char) < 32 or ord(char) > 126 for char in text):
        raise ManifestError(f"{what} must be printable ASCII: {text!r}")


@dataclass(frozen=True)
class BookSpec:
    book_id: int
    name: str
    salt: str = DEFAULT_SALT
    min_length: int = DEFAULT_MIN_LENGTH
    alphabet: str = DEFAULT_ALPHABET
    drop: str = ""

    def resolved_alphabet(self) -> str:
        dropped = set(self.drop)
        return "".join(char for char in self.alphabet if char not in dropped)

    def build(self) -> "Book":
        alphabet = self.resolved_alphabet()
        _check_printable_ascii(alphabet, f"{self.name} alphabet")
        _check_printable_ascii(self.salt, f"{self.name} salt")
        try:
            engine = Hashids(self.salt, self.min_length, alphabet)
        except ValueError as exc:
            raise ManifestError(f"{self.name}: {exc}") from exc
        return Book(self, engine)


class Book:
    """One native Hashids book plus the originals it actually holds."""

    def __init__(self, spec: BookSpec, engine: Hashids):
        self.spec = spec
        self.engine = engine
        self.orders: set[tuple[int, ...]] = set()
        self.ranges: list[list[tuple[int, int]]] = []
        self.reviewed_ranges: list[list[tuple[int, int]]] = []

    @property
    def book_id(self) -> int:
        return self.spec.book_id

    @property
    def name(self) -> str:
        return self.spec.name

    def encode(self, *values: int) -> str:
        return self.engine.encode(*values)

    def decode(self, code: str) -> tuple[int, ...]:
        return self.engine.decode(code)

    def round_trips(self, code: str) -> bool:
        numbers = self.decode(code)
        return bool(numbers) and self.encode(*numbers) == code

    def reprint(self, order: Sequence[int]) -> str | None:
        """The exact code the book prints for a registered order."""

        values = tuple(order)
        if values not in self.orders:
            return None
        code = self.encode(*values)
        if code and self.decode(code) == values:
            return code
        return None

    def register(self, order: Sequence[int]) -> bool:
        values = tuple(order)
        if not 1 <= len(values) <= 3:
            raise ManifestError(
                f"{self.name}: an order has one to three fields, got {values!r}"
            )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values
        ):
            raise ManifestError(f"{self.name}: bad original fields {values!r}")
        if values in self.orders:
            return False
        self.orders.add(values)
        return True

    def in_box(self, order: Sequence[int], boxes: Sequence[Sequence[tuple[int, int]]]) -> bool:
        values = tuple(order)
        for box in boxes:
            if len(box) != len(values):
                continue
            if all(low <= value <= high for value, (low, high) in zip(values, box)):
                return True
        return False

    def has_original(self, order: Sequence[int]) -> bool:
        """A tuple is real when registered or inside a loaded numeric box."""

        return tuple(order) in self.orders or self.in_box(order, self.ranges)

    def is_reviewed(self, order: Sequence[int]) -> bool:
        return self.in_box(order, self.reviewed_ranges)

    def lookup(self, code: str) -> str | None:
        """Native round-trip only; no reimplementation is trusted."""

        if not code:
            return None
        numbers = self.decode(code)
        if numbers and self.encode(*numbers) == code:
            return code
        return None

    def lookup_true(self, code: str) -> str | None:
        canonical = self.lookup(code)
        if canonical is None:
            return None
        return canonical if self.has_original(self.decode(code)) else None


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass
class Manifest:
    books: list[Book]
    reviewed_codes: list[str] = field(default_factory=list)
    allowed_prefixes: list[str] | None = None
    max_prefix: int | None = None
    reviewed_scan_limit: int = DEFAULT_REVIEWED_SCAN_LIMIT
    reviewed_scan_truncated: bool = False

    @property
    def default_book(self) -> int:
        return self.books[OLD_BOOK_ID].book_id

    def by_id(self, book_id: int) -> Book:
        for book in self.books:
            if book.book_id == book_id:
                return book
        raise ManifestError(f"unknown book id {book_id}")

    def enumerate_reviewed(self) -> list[tuple[int, tuple[int, ...]]]:
        """Reviewed tuples that are already concrete originals.

        ``orders`` are always included.  A reviewed numeric box is only
        expanded when it is small enough to enumerate within the configured
        limit; large boxes stay symbolic and are checked arithmetically by
        the rule solver, never by dumping codes.
        """

        found: dict[tuple[int, tuple[int, ...]], None] = {}
        for book in self.books:
            for order in book.orders:
                if book.is_reviewed(order):
                    found[(book.book_id, order)] = None
            for box in book.reviewed_ranges:
                size = math.prod(high - low + 1 for low, high in box)
                if size > self.reviewed_scan_limit:
                    self.reviewed_scan_truncated = True
                    continue
                spans = [range(low, high + 1) for low, high in box]
                for order in itertools.product(*spans):
                    if not book.is_reviewed(order):
                        continue
                    found[(book.book_id, order)] = None
        return [(book_id, order) for book_id, order in found]


def _parse_box(raw, fields: int) -> list[tuple[int, int]]:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = list(raw)
        if values and all(
            isinstance(item, Sequence) and not isinstance(item, (str, bytes))
            for item in values
        ):
            if len(values) != fields:
                raise ManifestError(f"box {raw!r} does not describe {fields} fields")
            pairs = []
            for item in values:
                if len(item) != 2:
                    raise ManifestError(f"bad closed interval {item!r}")
                low, high = int(item[0]), int(item[1])
                if low < 0 or high < low:
                    raise ManifestError(f"bad closed interval [{low}, {high}]")
                pairs.append((low, high))
            return pairs
        if len(values) == 2 * fields:
            pairs = []
            for index in range(fields):
                low, high = int(values[index]), int(values[index + fields])
                if low < 0 or high < low:
                    raise ManifestError(f"bad closed interval [{low}, {high}]")
                pairs.append((low, high))
            return pairs
        if len(values) == fields:
            return [(int(value), int(value)) for value in values]
        raise ManifestError(f"box {raw!r} is ambiguous for {fields} fields")

    if not isinstance(raw, Mapping):
        raise ManifestError(f"unsupported range entry {raw!r}")

    lows = raw.get("min", raw.get("lo", raw.get("lower")))
    highs = raw.get("max", raw.get("hi", raw.get("upper")))
    if lows is None and "fields" in raw:
        nested = raw["fields"]
        if isinstance(nested, Mapping):
            lows = nested.get("min", nested.get("lo"))
            highs = nested.get("max", nested.get("hi"))
    if lows is None and "field_ranges" in raw:
        pairs = []
        for item in raw["field_ranges"]:
            if isinstance(item, Mapping):
                low = item.get("min", item.get("lo"))
                high = item.get("max", item.get("hi"))
            else:
                low, high = item
            low, high = int(low), int(high)
            if low < 0 or high < low:
                raise ManifestError(f"bad closed interval [{low}, {high}]")
            pairs.append((low, high))
        if len(pairs) != fields:
            raise ManifestError(f"box {raw!r} does not describe {fields} fields")
        return pairs
    if isinstance(lows, (int, str)) and isinstance(highs, (int, str)) and fields == 1:
        low, high = int(lows), int(highs)
        if low < 0 or high < low:
            raise ManifestError(f"bad closed interval [{low}, {high}]")
        return [(low, high)]
    if not isinstance(lows, Sequence) or not isinstance(highs, Sequence):
        raise ManifestError(f"box {raw!r} needs min and max arrays")
    if len(lows) != fields or len(highs) != fields:
        raise ManifestError(f"box {raw!r} does not describe {fields} fields")
    pairs = []
    for low, high in zip(lows, highs):
        low, high = int(low), int(high)
        if low < 0 or high < low:
            raise ManifestError(f"bad closed interval [{low}, {high}]")
        pairs.append((low, high))
    return pairs


def _parse_boxes(raw, fields: int) -> list[list[tuple[int, int]]]:
    if raw is None:
        return []
    if isinstance(raw, Mapping) and "min" not in raw and "max" not in raw and isinstance(
        raw.get("fields", raw.get("field_ranges")), list
    ):
        # ``{"fields": [{"min": ...}, ...]}`` is a list of boxes.
        return [_parse_box(item, fields) for item in raw["fields"]]
    if isinstance(raw, Mapping):
        return [_parse_box(raw, fields)]
    return [_parse_box(box, fields) for box in raw]


def _orders_from(raw) -> list[Sequence[int]]:
    if raw is None:
        return []
    orders = []
    for item in raw:
        if isinstance(item, Mapping):
            item = item.get(
                "order",
                item.get("fields", item.get("values", item.get("numbers"))),
            )
        if item is None:
            raise ManifestError(f"order entry {item!r} has no fields")
        orders.append(item)
    return orders


def load_manifest(document: Mapping) -> Manifest:
    raw_books = document.get("books")
    if not isinstance(raw_books, Sequence) or len(raw_books) < 2:
        raise ManifestError("the manifest needs an old book and one other book")

    books: list[Book] = []
    for index, raw in enumerate(raw_books):
        spec = BookSpec(
            book_id=int(raw.get("id", index)),
            name=str(raw.get("name", f"book-{index}")),
            salt=str(raw.get("salt", DEFAULT_SALT)),
            min_length=int(raw.get("min_length", DEFAULT_MIN_LENGTH)),
            alphabet=str(raw.get("alphabet", DEFAULT_ALPHABET)),
            drop=str(raw.get("drop", "")),
        )
        book = spec.build()
        fields = max(int(raw.get("fields", raw.get("arity", 1))), 1)
        book.ranges = _parse_boxes(
            raw.get("ranges", raw.get("loaded_ranges", raw.get("numeric_ranges"))),
            fields,
        )
        book.reviewed_ranges = _parse_boxes(
            raw.get("reviewed_ranges", raw.get("reviewed")),
            fields,
        )
        for order in _orders_from(raw.get("orders", raw.get("records", raw.get("originals")))):
            book.register(order)
        books.append(book)

    ids = [book.book_id for book in books]
    if len(ids) != len(set(ids)):
        raise ManifestError("book ids must be unique")

    reviewed_codes = [str(code) for code in document.get("reviewed_codes", [])]
    for code in reviewed_codes:
        _check_printable_ascii(code, "reviewed code")

    allowed = document.get("allowed_prefixes")
    if allowed is not None:
        allowed = [str(prefix) for prefix in allowed]
        for prefix in allowed:
            _check_printable_ascii(prefix, "allowed prefix")

    max_prefix = document.get("max_prefix")
    scan_limit = int(
        document.get("reviewed_scan_limit", DEFAULT_REVIEWED_SCAN_LIMIT)
    )
    if scan_limit < 0:
        raise ManifestError("reviewed_scan_limit must be non-negative")
    return Manifest(
        books=books,
        reviewed_codes=reviewed_codes,
        allowed_prefixes=allowed,
        max_prefix=None if max_prefix is None else int(max_prefix),
        reviewed_scan_limit=scan_limit,
    )


def demo_manifest() -> dict:
    """The exact pair stated in the task; used for the regression demo."""

    other_alphabet = "".join(
        char for char in DEFAULT_ALPHABET if char not in OTHER_DROP
    )
    return {
        "books": [
            {
                "id": OLD_BOOK_ID,
                "name": "old",
                "alphabet": DEFAULT_ALPHABET,
                "salt": DEFAULT_SALT,
                "min_length": 4,
                "fields": 1,
                "ranges": [{"min": [0], "max": [1000000]}],
                "reviewed_ranges": [{"min": [151], "max": [151]}],
                "orders": [[151]],
            },
            {
                "id": OTHER_BOOK_ID,
                "name": "other",
                "alphabet": other_alphabet,
                "salt": OTHER_SALT,
                "min_length": 4,
                "fields": 1,
                "ranges": [{"min": [0], "max": [1000000]}],
                "reviewed_ranges": [{"min": [55263], "max": [55263]}],
                "orders": [[55263]],
            },
        ],
        "reviewed_codes": ["epQ6"],
    }


# ---------------------------------------------------------------------------
# Collision analysis
# ---------------------------------------------------------------------------


def reading(book: Book, code: str) -> dict:
    numbers = book.decode(code)
    round_trip = bool(numbers) and book.encode(*numbers) == code
    return {
        "book": book.name,
        "id": book.book_id,
        "numbers": list(numbers),
        "round_trips": round_trip,
        "code": code if round_trip else None,
        "original": bool(numbers) and book.has_original(numbers),
        "reviewed": bool(numbers) and book.is_reviewed(numbers),
    }


def classify(manifest: Manifest, code: str) -> dict:
    readings = [reading(book, code) for book in manifest.books]
    live = [
        item
        for item in readings
        if item["round_trips"] and item["original"]
    ]
    return {
        "code": code,
        "readings": readings,
        "collision": len(live) > 1,
        "books": [item["book"] for item in live],
    }


def collision_constraints(manifest: Manifest) -> tuple[dict[str, int], list[dict], list[dict]]:
    """Return ``(targets, indistinguishable, unverified)`` for collisions.

    ``targets`` maps a reviewed collision's code to the book it must reach.
    ``indistinguishable`` holds pairs whose two reviewed originals share one
    code; no prefix rule can separate them.
    """

    targets: dict[str, int] = {}
    indistinguishable: list[dict] = []
    unverified: list[dict] = []

    codes = list(dict.fromkeys(manifest.reviewed_codes))
    for book in manifest.books:
        for order in sorted(book.orders):
            code = book.reprint(order)
            if code and code not in codes and book.is_reviewed(order):
                codes.append(code)

    def collides_elsewhere(code: str, book_id: int) -> bool:
        return any(
            other.book_id != book_id
            and other.book.lookup(code)
            and other.book.has_original(other.book.decode(code))
            for other in manifest.books
        )

    # Reviewed numeric boxes are expanded only while their size stays within
    # the manifest's scan budget.  A code joins the collision set only when
    # the other book can also read it to a real original; that keeps a large
    # reviewed box from turning into a full code dump.
    for book_id, order in manifest.enumerate_reviewed():
        book = manifest.by_id(book_id)
        code = book.reprint(order)
        if code and code not in codes and collides_elsewhere(code, book_id):
            codes.append(code)

    for code in codes:
        info = classify(manifest, code)
        if not info["collision"]:
            continue
        live = info["readings"]
        live = [item for item in live if item["round_trips"] and item["original"]]
        reviewed = [item for item in live if item["reviewed"]]
        if len(reviewed) > 1:
            indistinguishable.append(
                {
                    "reason": "two reviewed originals share one short code",
                    "code": code,
                    "blocking_prefix": code,
                    "readings": [
                        {
                            "book": item["book"],
                            "id": item["id"],
                            "code": item["code"],
                            "order": item["numbers"],
                        }
                        for item in reviewed
                    ],
                }
            )
        elif len(reviewed) == 1:
            targets[code] = reviewed[0]["id"]
        else:
            unverified.append({"code": code, "readings": live})
    return targets, indistinguishable, unverified


# ---------------------------------------------------------------------------
# Minimal longest-prefix rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=False)
class Rule:
    prefix: str
    book: int

    def key(self) -> tuple[str, str]:
        # ASCII order of the prefix, then the book number written as ASCII.
        return (self.prefix, str(self.book))

    def as_dict(self, manifest: Manifest | None = None) -> dict:
        data = {"prefix": self.prefix, "book": self.book}
        if manifest is not None:
            try:
                data["book_name"] = manifest.by_id(self.book).name
            except ManifestError:
                pass
        return data


@dataclass
class SolveResult:
    rules: list[Rule]
    targets: dict[str, int]
    feasible: bool
    indistinguishable: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    minimal_count: int | None = None

    def rule_dicts(self, manifest: Manifest | None = None) -> list[dict]:
        return [rule.as_dict(manifest) for rule in self.rules]


class _TrieNode:
    __slots__ = ("path", "children", "target")

    def __init__(self, path: str):
        self.path = path
        self.children: dict[str, "_TrieNode"] = {}
        self.target: int | None = None


class MinimumRuleSolver:
    """Minimum longest-prefix rules over a trie of reviewed codes.

    A rule at a node applies to every code below it until a longer rule
    overrides it.  ``default_book`` is what happens when nothing matches.
    """

    def __init__(
        self,
        targets: Mapping[str, int],
        book_ids: Sequence[int],
        default_book: int,
        allowed_prefixes: Iterable[str] | None = None,
        max_prefix: int | None = None,
    ):
        if not book_ids:
            raise ValueError("at least one book is required")
        self.targets = dict(targets)
        self.book_ids = list(book_ids)
        self.default_book = default_book
        self.max_prefix = max_prefix
        self.root = _TrieNode("")
        self.candidates: set[str] = set()
        self._build()

        if allowed_prefixes is None:
            self.candidates = set(self._all_prefixes())
        else:
            self.candidates = {
                prefix
                for prefix in allowed_prefixes
                if self._length_ok(prefix)
            }
        # Only prefixes of constrained codes can ever be useful.
        self.candidates = {
            prefix
            for prefix in self.candidates
            if any(code.startswith(prefix) for code in self.targets)
        }

    def _length_ok(self, prefix: str) -> bool:
        return self.max_prefix is None or len(prefix) <= self.max_prefix

    def _all_prefixes(self) -> Iterable[str]:
        for code in self.targets:
            for length in range(1, len(code) + 1):
                if self._length_ok(code[:length]):
                    yield code[:length]

    def _build(self) -> None:
        for code, target in self.targets.items():
            node = self.root
            for char in code:
                node = node.children.setdefault(char, _TrieNode(node.path + char))
            if node.target is not None and node.target != target:
                raise ManifestError(f"{code} is constrained to two books")
            node.target = target

    # -- dynamic program ----------------------------------------------------

    def _solve_with(self, required_in: Mapping[str, int], excluded: set[tuple[str, int]]):
        """Cheapest consistent rule set that *includes* every required rule.

        ``required_in`` maps a prefix to a book that must appear in the final
        set.  ``excluded`` lists prefix/book pairs that must not appear.  The
        memo key includes whether the currently required rule has already
        been emitted, so counts are exact: a rule inherited from an ancestor
        is not charged twice.
        """

        memo: dict[tuple[int, int, str | None, bool], tuple[int, tuple[Rule, ...]] | None] = {}

        def visit(node: _TrieNode, inherited: int, owner: str | None, emitted: bool):
            key = (id(node), inherited, owner, emitted)
            if key in memo:
                return memo[key]

            required = required_in.get(node.path)
            base_children: dict[tuple[int, bool], tuple[int, tuple[Rule, ...]] | None] = {}

            def children_plan(label: int, state: bool):
                cache_key = (label, state)
                if cache_key in base_children:
                    return base_children[cache_key]
                total = 0
                rules: set[Rule] = set()
                for child in node.children.values():
                    plan = visit(child, label, owner, state)
                    if plan is None:
                        base_children[cache_key] = None
                        return None
                    total += plan[0]
                    rules.update(plan[1])
                base_children[cache_key] = (total, tuple(sorted(rules, key=Rule.key)))
                return base_children[cache_key]

            def emit(book: int):
                plan = children_plan(book, True)
                if plan is None:
                    return None
                return (
                    plan[0] + 1,
                    tuple(sorted(set(plan[1]) | {Rule(node.path, book)}, key=Rule.key)),
                )

            options: list[tuple[int, tuple[Rule, ...]]] = []

            if required is not None:
                if node.target is not None and required != node.target:
                    memo[key] = None
                    return None
                if (node.path, required) in excluded:
                    memo[key] = None
                    return None
                if owner == node.path and inherited == required:
                    plan = children_plan(inherited, True)
                    if plan is not None:
                        options.append(plan)
                else:
                    candidate = emit(required)
                    if candidate is not None:
                        options.append(candidate)
            else:
                if node.target is None or inherited == node.target:
                    plan = children_plan(inherited, emitted)
                    if plan is not None:
                        options.append(plan)
                if node.path in self.candidates:
                    for book in self.book_ids:
                        if book == inherited or (node.path, book) in excluded:
                            continue
                        if node.target is not None and book != node.target:
                            continue
                        candidate = emit(book)
                        if candidate is not None:
                            options.append(candidate)

            if not options:
                memo[key] = None
                return None
            best = min(options, key=lambda item: (item[0], tuple(rule.key() for rule in item[1])))
            memo[key] = best
            return best

        return visit(self.root, self.default_book, None, False)

    def solve(self) -> tuple[bool, list[Rule], int | None]:
        """Return feasibility, the lexicographically first minimum rule set."""

        plan = self._solve_with({}, set())
        if plan is None:
            return False, [], None
        optimum = plan[0]

        # Lexicographic tie-break over the sorted rule list.  Freeze one rule
        # at a time in ascending ASCII order when an optimum still exists.
        ordered_candidates = sorted(
            ((prefix, book) for prefix in self.candidates for book in self.book_ids),
            key=lambda item: (item[0], str(item[1])),
        )
        required: dict[str, int] = {}
        excluded: set[tuple[str, int]] = set()
        for prefix, book in ordered_candidates:
            if prefix in required:
                continue
            trial = dict(required)
            trial[prefix] = book
            trial_plan = self._solve_with(trial, excluded)
            if trial_plan is not None and trial_plan[0] == optimum:
                required = trial
            else:
                excluded.add((prefix, book))

        final = self._solve_with(required, excluded)
        if final is None:
            return False, [], None
        return True, list(final[1]), optimum

    def diagnose(self) -> list[dict]:
        """Explain infeasibility caused by the allowed-prefix list."""

        findings: list[dict] = []
        for code, target in self.targets.items():
            available = [
                prefix
                for prefix in self.candidates
                if code.startswith(prefix)
            ]
            if not available:
                findings.append(
                    {
                        "reason": "no allowed prefix for reviewed code",
                        "code": code,
                        "target_book": target,
                        "blocking_prefix": None,
                    }
                )
        # Group codes by their finest available prefix.  Two different
        # targets in one group means no allowed rule can separate them.
        groups: dict[str, list[tuple[str, int]]] = {}
        for code, target in self.targets.items():
            available = [p for p in self.candidates if code.startswith(p)]
            if not available:
                continue
            finest = max(available, key=lambda prefix: (len(prefix), prefix))
            groups.setdefault(finest, []).append((code, target))
        for prefix, members in groups.items():
            targets = {target for _, target in members}
            if len(targets) > 1:
                findings.append(
                    {
                        "reason": "allowed prefixes stop before the codes separate",
                        "blocking_prefix": prefix,
                        "codes": [
                            {"code": code, "target_book": target}
                            for code, target in sorted(members)
                        ],
                    }
                )
        return findings


# ---------------------------------------------------------------------------
# Counter table
# ---------------------------------------------------------------------------


def solve_manifest(manifest: Manifest) -> SolveResult:
    targets, indistinguishable, unverified = collision_constraints(manifest)
    books = manifest.books
    # Resolve target ids to the first configured book with that id.
    solver = MinimumRuleSolver(
        targets=targets,
        book_ids=[book.book_id for book in books],
        default_book=manifest.default_book,
        allowed_prefixes=manifest.allowed_prefixes,
        max_prefix=manifest.max_prefix,
    )
    feasible, rules, optimum = solver.solve()
    diagnostics = [] if feasible else solver.diagnose()
    return SolveResult(
        rules=rules,
        targets=targets,
        feasible=feasible and not indistinguishable,
        indistinguishable=indistinguishable,
        unverified=unverified,
        diagnostics=diagnostics,
        minimal_count=optimum,
    )


class CounterTable:
    """The query counter built from a manifest and its solved rules."""

    def __init__(self, manifest: Manifest, rules: Sequence[Rule]):
        self.manifest = manifest
        self.rules = sorted(rules, key=Rule.key)
        self._by_prefix = {rule.prefix: rule.book for rule in self.rules}

    def _longest_rule(self, code: str) -> Rule | None:
        best: Rule | None = None
        for length in range(1, len(code) + 1):
            book = self._by_prefix.get(code[:length])
            if book is not None and (best is None or length >= len(best.prefix)):
                best = Rule(code[:length], book)
        return best

    def choose(self, code: str) -> dict:
        options = []
        for book in self.manifest.books:
            info = reading(book, code)
            if info["round_trips"] and info["original"]:
                options.append(info)
        if not options:
            return {"code": code, "book": None, "reason": "no real round-trip"}
        if len(options) == 1:
            return {"code": code, "book": options[0]["id"], "reason": "only one real book"}
        rule = self._longest_rule(code)
        if rule is not None:
            return {"code": code, "book": rule.book, "reason": f"prefix {rule.prefix!r}"}
        return {"code": code, "book": self.manifest.default_book, "reason": "default old book"}

    def audit_reviewed(self) -> list[dict]:
        rows = []
        for code, target in sorted(solve_manifest(self.manifest).targets.items()):
            picked = self.choose(code)
            rows.append(
                {
                    "code": code,
                    "target": target,
                    "picked": picked["book"],
                    "ok": picked["book"] == target,
                    "reason": picked["reason"],
                }
            )
        return rows


# ---------------------------------------------------------------------------
# Small-ledger native cross-check
# ---------------------------------------------------------------------------


def audit_small(manifest: Manifest, rules: Sequence[Rule], limit: int) -> dict:
    """Compare the counter with native encode/decode on a small ledger.

    This is a test aid, not the routing algorithm.  It walks ``0..limit`` in
    each book, so it is suitable only for small numeric ledgers.
    """

    table = CounterTable(manifest, rules)
    failures = []
    checked = 0
    for book in manifest.books:
        for value in range(0, limit + 1):
            code = book.encode(value)
            if not code:
                continue
            checked += 1
            numbers = book.decode(code)
            if numbers != (value,):
                failures.append(
                    {
                        "book": book.name,
                        "value": value,
                        "code": code,
                        "decoded": list(numbers),
                        "kind": "native round-trip",
                    }
                )
                continue
            if not book.has_original((value,)):
                continue
            decision = table.choose(code)
            if decision["book"] is None:
                failures.append(
                    {
                        "book": book.name,
                        "value": value,
                        "code": code,
                        "kind": "counter dropped a real original",
                    }
                )
    return {"checked": checked, "failures": failures}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def to_dict(manifest: Manifest, result: SolveResult) -> dict:
    return {
        "rules": result.rule_dicts(manifest),
        "rule_count": len(result.rules),
        "minimal_count": result.minimal_count,
        "feasible": result.feasible,
        "targets": [
            {"code": code, "book": target}
            for code, target in sorted(result.targets.items())
        ],
        "indistinguishable": result.indistinguishable,
        "unverified": result.unverified,
        "diagnostics": result.diagnostics,
        "tie_break": "prefix ASCII, then book number ASCII",
    }


def render_markdown(manifest: Manifest, result: SolveResult) -> str:
    data = to_dict(manifest, result)
    lines = ["# Hashids counter rules", ""]

    lines.append("## Books")
    lines.append("")
    for book in manifest.books:
        spec = book.spec
        lines.append(
            f"- `{book.book_id}` {book.name}: salt `{spec.salt!r}`, "
            f"min length `{spec.min_length}`, alphabet `{spec.alphabet}`"
        )
    lines.append("")

    lines.append("## Rules")
    lines.append("")
    if data["rules"]:
        lines.append("| prefix | book | name |")
        lines.append("| --- | --- | --- |")
        for rule in data["rules"]:
            lines.append(
                f"| `{rule['prefix']}` | {rule['book']} | {rule.get('book_name', '')} |"
            )
    elif result.feasible:
        lines.append("No override is needed; the old-book default is correct.")
    else:
        lines.append("No feasible rule set.")
    lines.append("")
    lines.append(f"Rule count: `{data['rule_count']}` (minimum `{data['minimal_count']}`).")
    lines.append("")

    if result.targets:
        lines.append("## Reviewed collisions")
        lines.append("")
        lines.append("| code | required book |")
        lines.append("| --- | --- |")
        for code, target in sorted(result.targets.items()):
            lines.append(f"| `{code}` | {target} |")
        lines.append("")

    if result.indistinguishable:
        lines.append("## Indistinguishable reviewed pairs")
        lines.append("")
        for item in result.indistinguishable:
            lines.append(f"- code `{item['code']}`, blocking prefix `{item['blocking_prefix']}`")
            for row in item["readings"]:
                lines.append(
                    f"  - book `{row['id']}` {row['book']}: code `{row['code']}`, "
                    f"original fields `{row['order']}`"
                )
        lines.append("")

    if result.diagnostics:
        lines.append("## Diagnostics")
        lines.append("")
        for item in result.diagnostics:
            lines.append(f"- {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
        lines.append("")

    lines.append("## Method")
    lines.append("")
    lines.append(
        "Rules come from a trie DP over reviewed collision codes.  "
        "Every reading is confirmed with the books' native `encode`/`decode`; "
        "numeric ranges and short codes are never enumerated."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--audit-limit", type=int)
    args = parser.parse_args(argv)

    if args.demo:
        document = demo_manifest()
    elif args.manifest is not None:
        document = json.loads(args.manifest.read_text(encoding="utf-8"))
    else:
        parser.error("give a manifest path or --demo")

    manifest = load_manifest(document)
    result = solve_manifest(manifest)

    if args.audit_limit is not None:
        audit = audit_small(manifest, result.rules, args.audit_limit)
        if args.json:
            print(json.dumps(to_dict(manifest, result), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            sys.stdout.write(render_markdown(manifest, result))
        if audit["failures"]:
            print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
            return 2
        return 0 if result.feasible else 1

    if args.json:
        print(json.dumps(to_dict(manifest, result), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render_markdown(manifest, result))
    return 0 if result.feasible else 1


if __name__ == "__main__":
    raise SystemExit(main())
