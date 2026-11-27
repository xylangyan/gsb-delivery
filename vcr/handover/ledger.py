"""The handover book: per-shift progress kept next to the original cassette.

The book is a separate YAML file. It is never part of the cassette's
interactions, so request matchers never see it. Each shift gets its own
section with an independent playback cursor, the original occurrences it has
used (each recorded once however often replayed), the new interactions it has
recorded, and the withdrawals it has observed.

A shift's progress is written as soon as the shift closes (and at every build
journal step); it never waits for another shift.
"""

import copy
import os
import tempfile
from contextlib import suppress
from pathlib import Path

import yaml

from vcr.request import Request
from vcr.serializers import compat

BOOK_FORMAT_VERSION = 1


class HandoverBookError(RuntimeError):
    """Raised when the handover book on disk cannot be reconciled."""


def _interaction_to_dict(request, response):
    return {
        "request": compat.convert_to_unicode(request._to_dict()),
        "response": compat.convert_to_unicode(copy.deepcopy(response)),
    }


def _interaction_from_dict(entry):
    request = Request._from_dict(entry["request"])
    response = compat.convert_to_bytes(copy.deepcopy(entry["response"]))
    return request, response


def _request_signature(request):
    """A stable identity for a recorded request, used to avoid double recording."""
    body = request.body
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, list):
        body = b"".join(part if isinstance(part, bytes) else str(part).encode("utf-8") for part in body)
    if isinstance(body, str):
        body = body.encode("utf-8")
    headers = {key.lower(): value for key, value in request.headers.items()}
    return (request.method, request.uri, body, repr(sorted(headers.items())))


class ShiftLedger:
    """One shift's view of the handover book."""

    def __init__(self, shift_id, section=None):
        self.shift_id = shift_id
        section = section or {}
        self.cursor = int(section.get("cursor", 0))
        # Original occurrence indices this shift has actually used, each once.
        self.used_occurrences = list(section.get("used_occurrences", []))
        # Original occurrence indices this shift has already reached (played or
        # traversed into its cursor); withdrawals after this point don't apply.
        self.reached_occurrences = list(section.get("reached_occurrences", []))
        recorded = section.get("recorded", [])
        self.recorded = [_interaction_from_dict(entry) for entry in recorded]
        self._recorded_signatures = [
            tuple(entry["signature"]) for entry in recorded if entry.get("signature")
        ]
        self.closed = bool(section.get("closed", False))
        self.sequence = int(section.get("sequence", 0))

    def to_dict(self):
        recorded = []
        for (request, response), signature in zip(
            self.recorded,
            self._recorded_signatures,
            strict=True,
        ):
            entry = _interaction_to_dict(request, response)
            entry["signature"] = list(signature)
            recorded.append(entry)
        return {
            "cursor": self.cursor,
            "used_occurrences": list(self.used_occurrences),
            "reached_occurrences": list(self.reached_occurrences),
            "recorded": recorded,
            "closed": self.closed,
            "sequence": self.sequence,
        }

    def mark_reached(self, occurrence_index):
        if occurrence_index not in self.reached_occurrences:
            self.reached_occurrences.append(occurrence_index)

    def mark_used(self, occurrence_index):
        """Remember a used original occurrence once, however often it's played."""
        self.mark_reached(occurrence_index)
        if occurrence_index not in self.used_occurrences:
            self.used_occurrences.append(occurrence_index)

    def advance_cursor(self, position):
        self.cursor = max(self.cursor, int(position))

    def already_recorded(self, request):
        signature = _request_signature(request)
        for index, known in enumerate(self._recorded_signatures):
            if tuple(known) == signature:
                return index
        return None

    def add_recorded(self, request, response):
        """Record a fresh fetch.

        Two identical fetches made in the same open session are two separate
        pickups and are both kept. Suppressing duplicates on reopen is the
        ledger's job (via the open-time baseline), not this method's.
        """
        self.sequence += 1
        self.recorded.append((request, response))
        self._recorded_signatures.append(_request_signature(request))
        return len(self.recorded) - 1, False


class HandoverLedger:
    """The shared handover book, opened by one shift at a time."""

    def __init__(self, book_path, cassette_path, data=None):
        self.book_path = os.fspath(book_path)
        self.cassette_path = os.fspath(cassette_path)
        data = data or {"version": BOOK_FORMAT_VERSION, "shifts": {}, "withdrawn": [], "build": None}
        self._data = data
        self._data.setdefault("version", BOOK_FORMAT_VERSION)
        self._data.setdefault("shifts", {})
        self._data.setdefault("withdrawn", [])
        self._data.setdefault("build", None)
        self.shift = None
        # Signatures present when the book was opened. A fetch whose signature
        # is already in this baseline is a resumed re-run (or another shift's
        # earlier submission) and must not be recorded again; identical fetches
        # made later in the same session are genuine new pickups.
        self.baseline_signatures = set()

    @classmethod
    def open(cls, config, cassette_path):
        book_path = config.book_path_for(cassette_path)
        path = Path(book_path)
        if path.is_file():
            with path.open() as handle:
                data = yaml.safe_load(handle) or {}
            if data.get("version") != BOOK_FORMAT_VERSION:
                raise HandoverBookError(
                    f"Handover book {book_path} has unsupported version {data.get('version')!r}",
                )
            if data.get("cassette") and data["cassette"] != os.fspath(cassette_path):
                raise HandoverBookError(
                    f"Handover book {book_path} belongs to cassette {data['cassette']!r}, "
                    f"not {os.fspath(cassette_path)!r}",
                )
        else:
            data = None
        ledger = cls(book_path, cassette_path, data)
        ledger._data["cassette"] = os.fspath(cassette_path)
        ledger.shift = ShiftLedger(config.shift_id, ledger._data["shifts"].get(config.shift_id))
        for section in ledger._data["shifts"].values():
            for entry in section.get("recorded", []):
                if entry.get("signature"):
                    ledger.baseline_signatures.add(tuple(entry["signature"]))
        return ledger

    @property
    def shift_id(self):
        return self.shift.shift_id

    @property
    def withdrawn_occurrences(self):
        return tuple(int(entry["occurrence"]) for entry in self._data["withdrawn"])

    def withdraw(self, occurrence_index, request):
        """Register the withdrawal of one original occurrence that has appeared.

        The occurrence is identified by its original index and a request
        fingerprint, so it is unambiguous which appeared original was pulled.
        """
        occurrence_index = int(occurrence_index)
        for entry in self._data["withdrawn"]:
            if entry["occurrence"] == occurrence_index:
                return
        self._data["withdrawn"].append(
            {
                "occurrence": occurrence_index,
                "request": _request_signature(request)[1],
                "method": request.method,
            },
        )

    def is_withdrawn(self, occurrence_index):
        return int(occurrence_index) in self.withdrawn_occurrences

    def can_play_original(self, occurrence_index):
        """A reached page stays playable for this shift; new opens hide withdrawals."""
        if not self.is_withdrawn(occurrence_index):
            return True
        return occurrence_index in self.shift.reached_occurrences

    def record_playback(self, occurrence_index):
        self.shift.mark_used(int(occurrence_index))

    def record_new_interaction(self, request, response):
        """Record a new pickup, but never twice across a reopen.

        A fetch whose signature was already in the book when this shift opened
        is a resumed re-run (or the other shift's earlier submission), so it is
        not recorded again and nothing previously submitted is deleted. Two
        identical fetches made during the same open session are distinct
        pickups and are both kept. Returns ``(shift_id, index, recorded_before)``.
        """
        signature = _request_signature(request)
        if signature in self.baseline_signatures:
            for shift_id, section in self._data["shifts"].items():
                for index, entry in enumerate(section.get("recorded", [])):
                    if tuple(entry.get("signature", ())) == signature:
                        return shift_id, index, True
        index, recorded_before = self.shift.add_recorded(request, response)
        return self.shift_id, index, recorded_before

    def all_recorded_interactions(self):
        """New interactions from every shift, in original handover order.

        Each shift's entries carry their per-shift sequence; the global order
        interleaves shifts by the order their sections were first written.
        """
        ordered = []
        for shift_id, section in self._data["shifts"].items():
            for sequence, entry in enumerate(section.get("recorded", []), start=1):
                ordered.append((shift_id, sequence, _interaction_from_dict(entry)))
        if self.shift is not None and self.shift_id not in self._data["shifts"]:
            for sequence, entry in enumerate(self.shift.to_dict()["recorded"], start=1):
                ordered.append((self.shift_id, sequence, entry))
        return ordered

    def used_original_occurrences(self):
        """Original occurrences used by any shift, each counted once."""
        used = set()
        for section in self._data["shifts"].values():
            used.update(section.get("used_occurrences", []))
        if self.shift is not None:
            used.update(self.shift.used_occurrences)
        return sorted(used)

    # -- build journal -----------------------------------------------------

    def begin_build(self, target_path):
        build = self._data.get("build") or {}
        if build.get("status") == "complete" and build.get("target") == os.fspath(target_path):
            return build
        # A failed attempt's partial journal must not short-circuit the retry.
        same_target = build.get("target") == os.fspath(target_path)
        self._data["build"] = {
            "status": "in_progress",
            "target": os.fspath(target_path),
            "steps": build.get("steps", []) if same_target and build.get("status") != "failed" else [],
            "last_good": build.get("last_good"),
            "failure": None,
        }
        return self._data["build"]

    def mark_build_step(self, step, detail=None):
        build = self._data["build"]
        done = {entry["step"] for entry in build["steps"]}
        if step not in done:
            build["steps"].append({"step": step, "detail": detail})
        self.save()

    def build_step_done(self, step):
        build = self._data.get("build") or {}
        return any(entry["step"] == step for entry in build.get("steps", []))

    def fail_build(self, receive_config, page_turn, reason):
        build = self._data["build"]
        build["status"] = "failed"
        build["failure"] = {
            "receive_config": receive_config,
            "page_turn": page_turn,
            "reason": reason,
        }
        self.save()

    def complete_build(self, target_path):
        build = self._data["build"]
        build["status"] = "complete"
        build["target"] = os.fspath(target_path)
        build["failure"] = None
        build["last_good"] = os.fspath(target_path)
        self.save()

    @property
    def build_state(self):
        return copy.deepcopy(self._data.get("build"))

    # -- persistence -------------------------------------------------------

    def close_shift(self):
        self.shift.closed = True
        self.save()

    def save(self):
        if self.shift is not None:
            self._data["shifts"][self.shift_id] = self.shift.to_dict()
        payload = yaml.safe_dump(self._data, sort_keys=False, allow_unicode=True)
        path = Path(self.book_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.remove(tmp_name)
            raise

    def raw_data(self):
        return copy.deepcopy(self._data)
