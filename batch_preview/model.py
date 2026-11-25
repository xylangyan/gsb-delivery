"""Batch specification data model.

A batch describes one preview run:

* ``manuals``  -- the source manuals (Markdown text), keyed by manual id.
* ``segments`` -- named selections, each a 1-based inclusive line range of a
  manual.  Segments are the units releases pick from.
* ``releases`` -- each release is an ordered list of segment names.
* ``marks``    -- the approval table: positions (in the original manual) that
  are allowed to change their reference/definition label, together with the
  approved replacement labels (at most a handful of candidates each).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SegmentSpec:
    name: str
    manual: str
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive


@dataclass
class MarkSpec:
    manual: str
    candidates: list[str]
    line: int | None = None  # 1-based
    col: int | None = None  # 1-based
    offset: int | None = None  # 0-based char offset (alternative to line/col)


@dataclass
class BatchSpec:
    manuals: dict[str, str]
    segments: dict[str, SegmentSpec]
    releases: dict[str, list[str]]
    marks: list[MarkSpec] = field(default_factory=list)
    time_budget: float = 10.0


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def load_spec(data: dict, *, base_dir: Path | None = None) -> BatchSpec:
    """Build a :class:`BatchSpec` from a decoded JSON mapping."""
    _require(isinstance(data, dict), "batch spec must be a JSON object")

    manuals: dict[str, str] = {}
    for name, value in (data.get("manuals") or {}).items():
        if isinstance(value, str):
            manuals[name] = value
        elif isinstance(value, dict) and "text" in value:
            manuals[name] = value["text"]
        elif isinstance(value, dict) and "path" in value:
            _require(base_dir is not None, f"manual {name!r}: path needs base_dir")
            manuals[name] = (base_dir / value["path"]).read_text(encoding="utf-8")
        else:
            raise ValueError(f"manual {name!r}: expected string or object")
    _require(bool(manuals), "batch spec needs at least one manual")

    segments: dict[str, SegmentSpec] = {}
    for name, value in (data.get("segments") or {}).items():
        manual = value["manual"]
        _require(manual in manuals, f"segment {name!r}: unknown manual {manual!r}")
        start_line = int(value["start_line"])
        end_line = int(value["end_line"])
        _require(start_line >= 1, f"segment {name!r}: start_line must be >= 1")
        _require(
            end_line >= start_line, f"segment {name!r}: end_line before start_line"
        )
        segments[name] = SegmentSpec(name, manual, start_line, end_line)

    releases: dict[str, list[str]] = {}
    for name, value in (data.get("releases") or {}).items():
        _require(isinstance(value, list), f"release {name!r}: expected a list")
        for seg in value:
            _require(seg in segments, f"release {name!r}: unknown segment {seg!r}")
        releases[name] = list(value)
    _require(bool(releases), "batch spec needs at least one release")

    marks: list[MarkSpec] = []
    for i, value in enumerate(data.get("marks") or []):
        manual = value["manual"]
        _require(manual in manuals, f"marks[{i}]: unknown manual {manual!r}")
        candidates = [str(c) for c in value.get("candidates") or []]
        offset = value.get("offset")
        line = value.get("line")
        col = value.get("col")
        _require(
            offset is not None or (line is not None and col is not None),
            f"marks[{i}]: need either offset or line+col",
        )
        marks.append(
            MarkSpec(
                manual=manual,
                candidates=candidates,
                line=line,
                col=col,
                offset=offset,
            )
        )

    budget = float(data.get("time_budget_seconds", 10.0))
    return BatchSpec(
        manuals=manuals,
        segments=segments,
        releases=releases,
        marks=marks,
        time_budget=budget,
    )


def load_spec_file(path: str | Path) -> BatchSpec:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return load_spec(data, base_dir=path.parent)
