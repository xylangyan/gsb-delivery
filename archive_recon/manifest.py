"""Handover manifest parsing and validation.

A manifest is the machine-readable form of the handover material.  It states the
original-second window, the per-second reading bounds, which seconds are known
present/absent, the target archive layout, and -- for every surviving legacy
cell -- which publication produced it and whether the cell is genuine
aggregation or a value written straight into an archive slot.
"""

import json
from dataclasses import dataclass, field

AGGREGATIONS = ("average", "sum")


class ManifestError(ValueError):
    pass


@dataclass
class CellClaim:
    level: int
    start: int
    state: str  # "value" | "empty" | "lost"
    value: float = None
    origin: str = None  # "aggregate" | "direct"
    publication: str = None
    reason: str = None  # "threshold" for empty


@dataclass
class LegacySpec:
    id: str
    archives: list
    xFilesFactor: float
    aggregationMethod: str
    file: str = None
    cells: list = field(default_factory=list)


@dataclass
class TargetSpec:
    archives: list
    xFilesFactor: float
    aggregationMethod: str
    path: str = "reconstructed.wsp"


@dataclass
class SeriesSpec:
    start: int
    seconds: int
    low: float = -1e9
    high: float = 1e9
    known: dict = field(default_factory=dict)
    present: set = field(default_factory=set)
    missing: set = field(default_factory=set)
    ranges: dict = field(default_factory=dict)
    published: dict = field(default_factory=dict)

    def bounds(self, second):
        if second in self.known:
            value = self.known[second]
            return value, value
        if second in self.ranges:
            return tuple(self.ranges[second])
        return self.low, self.high


@dataclass
class Spec:
    series: SeriesSpec
    legacy: list
    target: TargetSpec
    publications: list = field(default_factory=list)

    @property
    def end(self):
        return self.series.start + self.series.seconds

    def publication_index(self, name):
        if name is None:
            return None
        try:
            return self.publications.index(name)
        except ValueError:
            raise ManifestError("unknown publication %r" % name)


def _require(mapping, key, kind, where):
    if key not in mapping:
        raise ManifestError("%s is missing %r" % (where, key))
    value = mapping[key]
    if not isinstance(value, kind):
        raise ManifestError("%s.%s has the wrong type" % (where, key))
    return value


def _archive_list(raw, where):
    if not isinstance(raw, list) or not raw:
        raise ManifestError("%s.archives must be a non-empty list" % where)
    archives = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ManifestError("%s.archives entries must be [step, points]" % where)
        step, points = int(entry[0]), int(entry[1])
        if step <= 0 or points <= 0:
            raise ManifestError("%s.archives needs positive step/points" % where)
        archives.append((step, points))
    archives.sort()
    if len({step for step, _ in archives}) != len(archives):
        raise ManifestError("%s.archives has duplicate steps" % where)
    for i in range(len(archives) - 1):
        step, points = archives[i]
        next_step, next_points = archives[i + 1]
        if next_step % step:
            raise ManifestError(
                "%s: archive steps must divide (%d does not divide %d)"
                % (where, step, next_step)
            )
        if step * points >= next_step * next_points:
            raise ManifestError(
                "%s: archive retention must increase with step" % where
            )
        if points < next_step // step:
            raise ManifestError(
                "%s: archive %s cannot feed %s" % (where, archives[i], archives[i + 1])
            )
    return archives


def _cell(raw, archives, where):
    level = int(_require(raw, "level", int, where))
    start = int(_require(raw, "start", int, where))
    state = _require(raw, "state", str, where)
    if level not in [step for step, _ in archives]:
        raise ManifestError("%s: level %d is not an archive step" % (where, level))
    if start % level:
        raise ManifestError("%s: start %d is not aligned to %d" % (where, start, level))
    if state == "value":
        value = float(_require(raw, "value", (int, float), where))
        origin = _require(raw, "origin", str, where)
        if origin not in ("aggregate", "direct"):
            raise ManifestError("%s: origin must be aggregate or direct" % where)
        return CellClaim(level, start, state, value=value, origin=origin,
                         publication=raw.get("publication"))
    if state == "empty":
        reason = _require(raw, "reason", str, where)
        if reason != "threshold":
            raise ManifestError(
                "%s: an empty cell may only cite reason 'threshold'; "
                "rollover holes must be marked state 'lost'" % where
            )
        return CellClaim(level, start, state, reason=reason,
                         publication=raw.get("publication"))
    if state == "lost":
        return CellClaim(level, start, state, reason=raw.get("reason", "rollover"),
                         publication=raw.get("publication"))
    raise ManifestError("%s: unknown cell state %r" % (where, state))


def parse(raw):
    where = "manifest"
    series_raw = _require(raw, "series", dict, where)
    start = int(_require(series_raw, "start", int, where + ".series"))
    seconds = int(_require(series_raw, "seconds", int, where + ".series"))
    if start <= 0 or seconds <= 0:
        raise ManifestError("series.start must be positive and seconds non-zero")
    low = float(series_raw.get("range", [-1e9, 1e9])[0])
    high = float(series_raw.get("range", [-1e9, 1e9])[1])
    if low > high:
        raise ManifestError("series.range is empty")

    known = {}
    for item in series_raw.get("known", []):
        t = int(item["second"]) - start
        known[t] = float(item["value"])
    ranges = {}
    for item in series_raw.get("ranges", []):
        t = int(item["second"]) - start
        lo, hi = (float(item["range"][0]), float(item["range"][1]))
        if lo > hi:
            raise ManifestError("series.ranges has an empty range at %d" % (t + start))
        ranges[t] = (lo, hi)
    present = {int(t) - start for t in series_raw.get("present", [])}
    missing = {int(t) - start for t in series_raw.get("missing", [])}
    published = {int(k) - start: v for k, v in series_raw.get("published", {}).items()}
    for t in list(known) + list(present) + list(missing) + list(ranges):
        if not 0 <= t < seconds:
            raise ManifestError("second %d is outside the series window" % (t + start))
    if set(known) & missing or present & missing:
        raise ManifestError("a second cannot be both present and missing")
    for t, value in known.items():
        if not low <= value <= high:
            raise ManifestError("known value at %d is outside the series range" % (t + start))
        if t in ranges and not ranges[t][0] <= value <= ranges[t][1]:
            raise ManifestError("known value at %d contradicts its range" % (t + start))
    series = SeriesSpec(start, seconds, low, high, known, present, missing, ranges,
                        published)

    target_raw = _require(raw, "target", dict, where)
    target = TargetSpec(
        _archive_list(_require(target_raw, "archives", list, where + ".target"),
                      where + ".target"),
        float(target_raw.get("xFilesFactor", 0.5)),
        target_raw.get("aggregationMethod", "average"),
        target_raw.get("path", "reconstructed.wsp"),
    )
    if target.aggregationMethod not in AGGREGATIONS:
        raise ManifestError("target aggregation must be average or sum")
    if not 0.0 <= target.xFilesFactor <= 1.0:
        raise ManifestError("target xFilesFactor must be in [0, 1]")
    for step, _ in target.archives:
        if start % step or seconds % step:
            raise ManifestError(
                "target step %d does not tile the series window" % step
            )

    publications = list(raw.get("publications", []))
    if len(set(publications)) != len(publications):
        raise ManifestError("publications must be unique")

    legacy = []
    for i, leg_raw in enumerate(_require(raw, "legacy", list, where)):
        leg_where = "legacy[%d]" % i
        archives = _archive_list(
            _require(leg_raw, "archives", list, leg_where), leg_where
        )
        leg = LegacySpec(
            str(_require(leg_raw, "id", str, leg_where)),
            archives,
            float(leg_raw.get("xFilesFactor", 0.5)),
            leg_raw.get("aggregationMethod", "average"),
            leg_raw.get("file"),
            [],
        )
        if leg.aggregationMethod not in AGGREGATIONS:
            raise ManifestError("%s: unsupported aggregation" % leg_where)
        if not 0.0 <= leg.xFilesFactor <= 1.0:
            raise ManifestError("%s: xFilesFactor must be in [0, 1]" % leg_where)
        seen = set()
        for j, cell_raw in enumerate(leg_raw.get("cells", [])):
            cell = _cell(cell_raw, archives, "%s.cells[%d]" % (leg_where, j))
            key = (cell.level, cell.start)
            if key in seen and cell.state != "value":
                raise ManifestError("%s: duplicate claim for %s" % (leg_where, key))
            seen.add(key)
            leg.cells.append(cell)
        for cell in leg.cells:
            if not series.start <= cell.start < series.start + series.seconds:
                raise ManifestError(
                    "%s: cell %s lies outside the series window"
                    % (leg_where, (cell.level, cell.start))
                )
        legacy.append(leg)
    if not legacy:
        raise ManifestError("manifest has no legacy archives")

    spec = Spec(series, legacy, target, publications)
    for leg in legacy:
        for cell in leg.cells:
            spec.publication_index(cell.publication)
    for t, name in published.items():
        spec.publication_index(name)
    return spec


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return parse(json.load(handle))


def validate_geometry(spec):
    """Require the window to be tiled by every archive step and to fit retention."""
    for leg in spec.legacy:
        for step, _ in leg.archives:
            if spec.series.start % step or spec.series.seconds % step:
                raise ManifestError(
                    "legacy %s step %d does not tile the series window" % (leg.id, step)
                )
    for step, _ in spec.target.archives:
        if spec.series.start % step or spec.series.seconds % step:
            raise ManifestError(
                "target step %d does not tile the series window" % step
            )
    window = spec.series.seconds
    for step, points in spec.target.archives:
        if step * points < window:
            raise ManifestError(
                "target archive %d:%d is shorter than the window (%d < %d)"
                % (step, points, step * points, window)
            )
    for leg in spec.legacy:
        for step, points in leg.archives:
            if step * points < window:
                raise ManifestError(
                    "legacy %s archive %d:%d is shorter than the window"
                    % (leg.id, step, points)
                )
