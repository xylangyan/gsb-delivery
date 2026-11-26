"""Prefix-by-prefix online/offline regression for :class:`KneeLocator`.

The public entry point is :func:`regress_prefix_range`.  Every prefix is
constructed from scratch: adding a point changes min/max normalization and can
therefore move an old conclusion backwards, so normalized state must not be
updated incrementally.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

try:  # Support both ``python -m kneed.prefix_regression`` and direct execution.
    from .knee_locator import KneeLocator
except ImportError:  # pragma: no cover - direct script path
    from knee_locator import KneeLocator

DEFAULT_S = 1.0
MIN_POINTS = 3

_REVERSED_X_ORIENTATIONS = {
    ("decreasing", "concave"),
    ("increasing", "convex"),
}


@dataclass(frozen=True)
class BatchSegment:
    """A run of equal upper-bound timings.

    ``count`` is the number of stored points in this run.  ``step`` is the
    integer batch-size stride used inside the run; the gap from the preceding
    point uses ``bridge_step`` when supplied and otherwise ``step``.
    """

    y: float
    count: int
    step: int = 1
    start_x: float | int | None = None
    bridge_step: int | None = None


def _scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def _float_evidence(value: Any) -> dict[str, Any]:
    number = float(value)
    return {"value": number, "hex": number.hex()}


def expand_segments(
    segments: Sequence[BatchSegment | Mapping[str, Any] | tuple[Any, ...]],
    x0: float | int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Expand compact timing/stride segments into dense ``x`` and ``y``.

    A mapping accepts the aliases ``y``/``time``/``timing``/``upper_bound``,
    ``count``/``points``/``length``, and ``step``/``stride``/``dx``.
    Three-item tuples are interpreted as ``(y, count, step)``.
    """

    chunks_x: list[np.ndarray] = []
    chunks_y: list[np.ndarray] = []
    next_x: float | int | None = x0

    for segment in segments:
        if isinstance(segment, tuple):
            if len(segment) != 3:
                raise ValueError("tuple segments must be (y, count, step)")
            seg = BatchSegment(y=segment[0], count=int(segment[1]), step=int(segment[2]))
        elif isinstance(segment, BatchSegment):
            seg = segment
        else:
            y_value = _first_key(
                segment, ("y", "time", "timing", "upper_bound", "cost", "duration")
            )
            count_value = _first_key(
                segment, ("count", "points", "length", "n"), required=True
            )
            step_value = _first_key(
                segment, ("step", "stride", "dx", "batch_step"), default=1
            )
            seg = BatchSegment(
                y=float(y_value),
                count=int(count_value),
                step=int(step_value),
                start_x=segment.get("start_x", segment.get("start_batch")),
                bridge_step=segment.get("bridge_step", segment.get("gap_step")),
            )

        if seg.count <= 0:
            raise ValueError("segment count must be positive")
        if seg.step <= 0:
            raise ValueError("batch step must be positive")

        if seg.start_x is not None:
            start_x = seg.start_x
        elif chunks_x:
            bridge = seg.bridge_step if seg.bridge_step is not None else seg.step
            start_x = chunks_x[-1][-1] + bridge
        else:
            start_x = x0 if next_x is None else next_x

        offsets = np.arange(seg.count, dtype=np.int64)
        chunk_x = np.asarray(start_x) + offsets * seg.step
        chunks_x.append(chunk_x)
        chunks_y.append(np.full(seg.count, float(seg.y), dtype=float))
        next_x = chunk_x[-1]

    return np.concatenate(chunks_x), np.concatenate(chunks_y)


def _first_key(
    mapping: Mapping[str, Any],
    keys: Sequence[str],
    default: Any = None,
    required: bool = False,
) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    if required:
        raise KeyError(f"missing one of required keys: {', '.join(keys)}")
    return default


def archive_to_xy(archive: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """Load either dense arrays or compact run segments from a mapping."""

    if "x" in archive and "y" in archive:
        return np.asarray(archive["x"]), np.asarray(archive["y"])
    if "batches" in archive and ("timings" in archive or "times" in archive):
        y_key = "timings" if "timings" in archive else "times"
        return np.asarray(archive["batches"]), np.asarray(archive[y_key])

    segments = archive.get("segments")
    if segments is None and {"y_runs", "counts", "steps"} <= archive.keys():
        segments = [
            {"y": y, "count": count, "step": step}
            for y, count, step in zip(
                archive["y_runs"], archive["counts"], archive["steps"]
            )
        ]
    if segments is None:
        raise ValueError("archive must contain x/y or segments")

    return expand_segments(segments, x0=archive.get("x0", archive.get("start_batch", 0)))


def _reversed_x(direction: str, curve: str) -> bool:
    return (direction, curve) in _REVERSED_X_ORIENTATIONS


def _raw_index(index: int, n: int, reversed_x: bool) -> int:
    return n - 1 - index if reversed_x else index


def trace_kneed_locator(kl: KneeLocator, online: bool = True) -> dict[str, Any]:
    """Replay the exact threshold state used by :class:`KneeLocator.find_knee`.

    Offline replay stops at the first accepted trigger, matching the library's
    early return.  Returned values retain double precision and include strict
    comparison results, including equality-at-threshold cases.
    """

    n_points = int(kl.N)
    if kl.maxima_indices.size == 0:
        return {"events": [], "closest_call": None}

    maxima = {int(index): position for position, index in enumerate(kl.maxima_indices)}
    minima = {int(index) for index in kl.minima_indices}
    max_position = 0
    threshold: Any = None
    threshold_index: int | None = None
    detection_active = True
    events: list[dict[str, Any]] = []
    closest_call: dict[str, Any] | None = None

    for i in range(n_points - 1):
        if i < int(kl.maxima_indices[0]):
            continue

        j = i + 1
        if i in maxima:
            threshold = kl.Tmx[max_position]
            threshold_index = i
            max_position += 1
            detection_active = True
        if i in minima:
            threshold = 0.0
            detection_active = False

        if not detection_active:
            continue

        observed = kl.y_difference[j]
        margin = observed - threshold
        hit = bool(observed < threshold)
        call = {
            "i": i,
            "j": j,
            "threshold_index": threshold_index,
            "threshold": threshold,
            "observed": observed,
            "margin": margin,
            "distance_to_threshold": abs(margin),
            "strict_less": hit,
        }
        if hit:
            events.append(call)
        if closest_call is None or call["distance_to_threshold"] < closest_call[
            "distance_to_threshold"
        ]:
            closest_call = call
        if hit and not online:
            break

    return {"events": events, "closest_call": closest_call}


def _enrich_call(
    call: Mapping[str, Any] | None,
    kl: KneeLocator,
    reversed_x: bool,
) -> dict[str, Any] | None:
    if call is None:
        return None

    n_points = int(kl.N)
    result: dict[str, Any] = {}
    for key, value in call.items():
        if key in {"i", "j", "threshold_index"} and value is not None:
            result[key] = int(value)
        elif key in {"threshold", "observed", "margin", "distance_to_threshold"}:
            result[key] = _float_evidence(value["value"] if isinstance(value, dict) else value)
        else:
            result[key] = _scalar(value)

    threshold_index = call.get("threshold_index")
    if threshold_index is not None:
        raw_max = _raw_index(int(threshold_index), n_points, reversed_x)
        result["threshold_point"] = {
            "transformed_index": int(threshold_index),
            "raw_index": raw_max,
            "x": _scalar(kl.x[raw_max]),
            "y": _scalar(kl.y[raw_max]),
            "x_normalized": _float_evidence(kl.x_normalized[int(threshold_index)]),
            "y_normalized": _float_evidence(kl.y_normalized[int(threshold_index)]),
            "difference": _float_evidence(kl.y_difference[int(threshold_index)]),
        }

    candidate_j = int(call["j"])
    raw_candidate = _raw_index(candidate_j, n_points, reversed_x)
    result["candidate_point"] = {
        "transformed_index": candidate_j,
        "raw_index": raw_candidate,
        "x": _scalar(kl.x[raw_candidate]),
        "y": _scalar(kl.y[raw_candidate]),
        "x_normalized": _float_evidence(kl.x_normalized[candidate_j]),
        "y_normalized": _float_evidence(kl.y_normalized[candidate_j]),
        "difference": _float_evidence(kl.y_difference[candidate_j]),
    }
    return result


def _side_detail(
    kl: KneeLocator,
    reversed_x: bool,
    online: bool,
) -> dict[str, Any]:
    trace = trace_kneed_locator(kl, online=online)
    events = [_enrich_call(event, kl, reversed_x) for event in trace["events"]]
    closest = _enrich_call(trace["closest_call"], kl, reversed_x)
    final_trigger = events[-1] if events else None

    if final_trigger is not None and kl.knee is not None:
        trigger_index = int(final_trigger["threshold_index"])
        raw_index = _raw_index(trigger_index, int(kl.N), reversed_x)
        trigger_knee = _scalar(kl.x[raw_index])
        if trigger_knee != _scalar(kl.knee):
            final_trigger["trace_consistency_error"] = {
                "traced_knee": trigger_knee,
                "library_knee": _scalar(kl.knee),
            }

    return {
        "knee": _scalar(kl.knee),
        "knee_y": _scalar(kl.knee_y),
        "norm_knee": _float_evidence(kl.norm_knee) if kl.norm_knee is not None else None,
        "trigger_count": len(events),
        "first_trigger": events[0] if events else None,
        "final_trigger": final_trigger,
        "closest_call": closest,
        "mean_normalized_x_step": _float_evidence(
            np.abs(np.diff(kl.x_normalized).mean())
        ),
    }


def _same_knee(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return bool(np.asarray(left) == np.asarray(right))


def evaluate_prefix(
    x: Sequence[float] | np.ndarray,
    y: Sequence[float] | np.ndarray,
    prefix: int,
    *,
    S: float = DEFAULT_S,
    curve: str = "convex",
    direction: str = "increasing",
    interp_method: str = "interp1d",
    polynomial_degree: int = 7,
) -> dict[str, Any]:
    """Evaluate online and offline knees for exactly the first ``prefix`` points."""

    x_array = np.asarray(x)
    y_array = np.asarray(y)
    if x_array.shape != y_array.shape or x_array.ndim != 1:
        raise ValueError("x and y must be one-dimensional arrays of equal length")
    if not MIN_POINTS <= prefix <= len(x_array):
        raise ValueError(f"prefix must be in [{MIN_POINTS}, {len(x_array)}]")

    x_prefix = x_array[:prefix]
    y_prefix = y_array[:prefix]
    if np.any(np.diff(x_prefix) <= 0):
        raise ValueError("batch sizes x must be strictly increasing")

    last_index = prefix - 1

    if np.min(y_prefix) == np.max(y_prefix):
        return {
            "prefix": prefix,
            "last_index": last_index,
            "last_x": _scalar(x_prefix[-1]),
            "last_y": _scalar(y_prefix[-1]),
            "eligible": False,
            "agreed": True,
            "disagreement": False,
            "reason": "constant_y_prefix",
            "offline": {"knee": None},
            "online": {"knee": None},
            "offline_knee": None,
            "online_knee": None,
        }

    common = {
        "S": S,
        "curve": curve,
        "direction": direction,
        "interp_method": interp_method,
        "polynomial_degree": polynomial_degree,
    }
    offline_kl = KneeLocator(x_prefix, y_prefix, online=False, **common)
    online_kl = KneeLocator(x_prefix, y_prefix, online=True, **common)
    reversed_x = _reversed_x(direction, curve)

    offline = _side_detail(offline_kl, reversed_x, online=False)
    online = _side_detail(online_kl, reversed_x, online=True)
    agreed = _same_knee(offline_kl.knee, online_kl.knee)

    return {
        "prefix": prefix,
        "last_index": last_index,
        "last_x": _scalar(x_prefix[-1]),
        "last_y": _scalar(y_prefix[-1]),
        "eligible": True,
        "agreed": agreed,
        "disagreement": not agreed,
        "offline_knee": _scalar(offline_kl.knee),
        "online_knee": _scalar(online_kl.knee),
        "offline": offline,
        "online": online,
    }


def _disagreement_intervals(reports: Sequence[Mapping[str, Any]]) -> list[dict[str, int]]:
    intervals: list[dict[str, int]] = []
    start: int | None = None
    end: int | None = None

    for report in reports:
        prefix = int(report["prefix"])
        if report.get("disagreement"):
            if start is None:
                start = end = prefix
            else:
                end = prefix
        elif start is not None:
            intervals.append({"first_prefix": start, "through_prefix": int(end)})
            start = end = None

    if start is not None:
        intervals.append({"first_prefix": start, "through_prefix": int(end)})
    return intervals


def _critical_evidence(reports: Sequence[Mapping[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    candidates: list[tuple[float, int, str, dict[str, Any]]] = []
    for report in reports:
        if not report.get("eligible"):
            continue
        for mode in ("offline", "online"):
            call = report[mode].get("closest_call")
            if call is None:
                continue
            distance = float(call["distance_to_threshold"]["value"])
            candidates.append(
                (
                    distance,
                    int(report["prefix"]),
                    mode,
                    {
                        "prefix": int(report["prefix"]),
                        "mode": mode,
                        "distance_to_threshold": call["distance_to_threshold"],
                        "strict_less": call["strict_less"],
                        "threshold": call["threshold"],
                        "observed": call["observed"],
                    },
                )
            )
    candidates.sort(key=lambda item: item[0])
    return [item[3] for item in candidates[:limit]]


def plateau_certificate(
    x: Sequence[float] | np.ndarray,
    y: Sequence[float] | np.ndarray,
    start: int,
    end: int,
    *,
    window: int = 2,
    critical_prefixes: Sequence[int] | None = None,
    recheck_edges: bool = False,
    S: float = DEFAULT_S,
    curve: str = "convex",
    direction: str = "increasing",
    **kwargs: Any,
) -> dict[str, Any]:
    """Check a long constant-y plateau without scanning every prefix.

    The two required spot checks are ``start`` and ``end``.  Small windows are
    evaluated only around explicitly supplied critical prefixes and detected
    batch-stride changes.  Set ``recheck_edges=True`` to also window the two
    spot-check endpoints.
    """

    x_array = np.asarray(x)
    y_array = np.asarray(y)
    if not MIN_POINTS <= start <= end <= len(x_array):
        raise ValueError("require 3 <= start <= end <= len(x)")

    plateau_y = y_array[start - 1]
    same_y = bool(np.all(y_array[start - 1 : end] == plateau_y))
    strides = np.diff(x_array[max(0, start - 2) : end])
    stride_changes: list[int] = []
    if strides.size:
        change_positions = np.flatnonzero(np.diff(strides) != 0)
        stride_changes = [int(pos + start) for pos in change_positions]

    checkpoints: set[int] = {start, end}
    window_centers = list(stride_changes)
    if critical_prefixes:
        window_centers.extend(int(prefix) for prefix in critical_prefixes)
    if recheck_edges:
        window_centers.extend((start, end))

    for center in window_centers:
        checkpoints.update(
            range(max(MIN_POINTS, center - window), min(end, center + window) + 1)
        )
    ordered = sorted(prefix for prefix in checkpoints if start <= prefix <= end)
    reports = [
        evaluate_prefix(
            x_array,
            y_array,
            prefix,
            S=S,
            curve=curve,
            direction=direction,
            **kwargs,
        )
        for prefix in ordered
    ]
    all_agreed = all(report["agreed"] for report in reports)
    first_failure = next(
        (report["prefix"] for report in reports if report["disagreement"]), None
    )

    return {
        "certificate_type": "constant_y_plateau_spot_check",
        "start_prefix": start,
        "end_prefix": end,
        "plateau_y": _scalar(plateau_y),
        "same_y_between_endpoints": same_y,
        "stride_change_prefixes": stride_changes,
        "critical_prefixes": [int(prefix) for prefix in critical_prefixes or []],
        "window": window,
        "recheck_edges": recheck_edges,
        "evaluated_prefixes": ordered,
        "evaluated_count": len(reports),
        "skipped_prefix_count": end - start + 1 - len(ordered),
        "all_spot_checks_agreed": all_agreed,
        "first_spot_check_disagreement": first_failure,
        "proof_basis": (
            "Recomputed each checkpoint from scratch with KneeLocator; "
            "the unevaluated interior is one unchanged compressed y/stride run."
        ),
        "reports": reports,
    }


def regress_prefix_range(
    x: Sequence[float] | np.ndarray | None = None,
    y: Sequence[float] | np.ndarray | None = None,
    start: int = MIN_POINTS,
    end: int | None = None,
    S: float = DEFAULT_S,
    *,
    curve: str = "convex",
    direction: str = "increasing",
    max_exhaustive: int | None = 2048,
    allow_plateau_certificate: bool = True,
    evidence_limit: int = 3,
    **kwargs: Any,
) -> dict[str, Any]:
    """Find the first online/offline disagreement in an inclusive prefix range.

    Prefix numbers are one-based point counts, so the ninth入账 point is
    ``prefix=9``.  Small ranges are exhaustively checked.  A range larger than
    ``max_exhaustive`` uses :func:`plateau_certificate` when possible.
    """

    if x is None or y is None:
        raise ValueError("x and y are required")
    x_array = np.asarray(x)
    y_array = np.asarray(y)
    if len(x_array) != len(y_array):
        raise ValueError("x and y must be the same length")

    start = max(MIN_POINTS, int(start))
    end = len(x_array) if end is None else int(end)
    if end > len(x_array):
        raise ValueError("end exceeds the number of points")
    if start > end:
        return {
            "start_prefix": start,
            "end_prefix": end,
            "mode": "empty",
            "prefixes": [],
            "first_disagreement": None,
        }

    prefix_count = end - start + 1
    use_certificate = (
        allow_plateau_certificate
        and max_exhaustive is not None
        and prefix_count > max_exhaustive
    )
    if use_certificate:
        certificate = plateau_certificate(
            x_array, y_array, start, end, S=S, curve=curve, direction=direction, **kwargs
        )
        return {
            "start_prefix": start,
            "end_prefix": end,
            "mode": "plateau_spot_check",
            "S": S,
            "curve": curve,
            "direction": direction,
            "first_disagreement": certificate["first_spot_check_disagreement"],
            "disagreement_intervals": [],
            "certificate": certificate,
            "evidence": certificate["reports"][:2],
        }

    reports = [
        evaluate_prefix(
            x_array,
            y_array,
            prefix,
            S=S,
            curve=curve,
            direction=direction,
            **kwargs,
        )
        for prefix in range(start, end + 1)
    ]
    intervals = _disagreement_intervals(reports)
    first_report = next((report for report in reports if report["disagreement"]), None)
    first_interval = intervals[0] if intervals else None

    evidence: dict[str, Any] = {
        "checked_prefix_count": len(reports),
        "prefix_conclusions": [
            {
                "prefix": report["prefix"],
                "offline_knee": report["offline_knee"],
                "online_knee": report["online_knee"],
                "agreed": report["agreed"],
            }
            for report in reports
        ],
        "endpoint_evidence": [reports[0], reports[-1]] if reports else [],
        "critical_threshold_evidence": _critical_evidence(reports, evidence_limit),
    }

    first_prefix = first_report["prefix"] if first_report else None
    through_prefix = first_interval["through_prefix"] if first_interval else None
    return {
        "start_prefix": start,
        "end_prefix": end,
        "prefix_range": (start, end),
        "mode": "exhaustive",
        "S": S,
        "curve": curve,
        "direction": direction,
        "first_disagreement": first_prefix,
        "first_disagreement_prefix": first_prefix,
        "first_divergence": first_prefix,
        "disagreement_start": first_prefix,
        "disagreement_through": through_prefix,
        "last_disagreement_prefix": through_prefix,
        "through_prefix": through_prefix,
        "disagreement_end": through_prefix,
        "first_disagreement_detail": first_report,
        "first": first_prefix,
        "last": through_prefix,
        "offline_knee": first_report["offline_knee"] if first_report else None,
        "online_knee": first_report["online_knee"] if first_report else None,
        "conclusions": (
            {
                "offline": first_report["offline_knee"],
                "online": first_report["online_knee"],
            }
            if first_report
            else None
        ),
        "ongoing_at_end": bool(
            first_report is not None
            and first_interval is not None
            and first_interval["through_prefix"] == end
        ),
        "disagreement_intervals": intervals,
        "prefixes": reports,
        "reports": reports,
        "evidence": evidence,
    }


class PrefixRegression:
    """Reusable, range-adjustable entry bound to one dense or compact curve."""

    def __init__(
        self,
        x: Sequence[float] | np.ndarray | Mapping[str, Any],
        y: Sequence[float] | np.ndarray | None = None,
        *,
        S: float = DEFAULT_S,
        curve: str = "convex",
        direction: str = "increasing",
        **kwargs: Any,
    ) -> None:
        if y is None:
            if not isinstance(x, Mapping):
                raise ValueError("a single mapping argument must be an archive")
            self.x, self.y = archive_to_xy(x)
        else:
            self.x = np.asarray(x)
            self.y = np.asarray(y)
        self.S = S
        self.curve = curve
        self.direction = direction
        self.options = kwargs

    def run(self, start: int = MIN_POINTS, end: int | None = None, **overrides: Any):
        options = {
            "S": self.S,
            "curve": self.curve,
            "direction": self.direction,
            **self.options,
            **overrides,
        }
        return regress_prefix_range(self.x, self.y, start, end, **options)

    find_first_disagreement = run
    regress = run

    def plateau_certificate(self, start: int, end: int, **overrides: Any):
        options = {
            "S": self.S,
            "curve": self.curve,
            "direction": self.direction,
            **self.options,
            **overrides,
        }
        return plateau_certificate(self.x, self.y, start, end, **options)


find_first_disagreement = regress_prefix_range
find_first_difference = regress_prefix_range
find_disagreements = regress_prefix_range
compare_prefixes = regress_prefix_range
prefix_regression = regress_prefix_range
regression_entry = regress_prefix_range
run_regression = regress_prefix_range


def _format_text(report: Mapping[str, Any]) -> str:
    lines = [
        f"range: {report['start_prefix']}..{report['end_prefix']} ({report['mode']})",
        f"call: curve={report.get('curve')} direction={report.get('direction')} S={report.get('S')}",
    ]
    first = report.get("first_disagreement")
    if first is None:
        lines.append("result: no online/offline disagreement")
        if report.get("mode") == "exhaustive":
            lines.append(
                f"evidence: checked {report['evidence']['checked_prefix_count']} prefixes"
            )
        else:
            cert = report["certificate"]
            lines.append(
                "evidence: evaluated "
                f"{cert['evaluated_count']} checkpoints, skipped "
                f"{cert['skipped_prefix_count']} interior prefixes"
            )
        return "\n".join(lines)

    detail = report["first_disagreement_detail"]
    lines.append(
        f"first disagreement: prefix {first}, through "
        f"{report.get('disagreement_through')}"
    )
    lines.append(
        f"conclusions at prefix {first}: offline={detail['offline_knee']!r}, "
        f"online={detail['online_knee']!r}"
    )
    for mode in ("offline", "online"):
        trigger = detail[mode].get("final_trigger")
        if trigger:
            lines.append(
                f"{mode} trigger: {trigger['observed']['value']!r} "
                f"< {trigger['threshold']['value']!r} at "
                f"raw x={trigger['threshold_point']['x']!r}, "
                f"candidate x={trigger['candidate_point']['x']!r}"
            )
    return "\n".join(lines)


def _load_input(path: str | None) -> Mapping[str, Any]:
    if path and path != "-":
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return json.load(sys.stdin)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="JSON file; '-' or omitted means stdin")
    parser.add_argument("--start", type=int, default=MIN_POINTS)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--S", type=float, default=DEFAULT_S, dest="S")
    parser.add_argument("--curve", default="convex")
    parser.add_argument("--direction", default="increasing")
    parser.add_argument("--max-exhaustive", type=int, default=2048)
    parser.add_argument("--text", action="store_true", help="print text instead of JSON")
    args = parser.parse_args(argv)

    archive = _load_input(args.input if args.input != "-" else None)
    x, y = archive_to_xy(archive)
    report = regress_prefix_range(
        x,
        y,
        args.start,
        args.end,
        S=args.S,
        curve=args.curve,
        direction=args.direction,
        max_exhaustive=args.max_exhaustive,
    )
    if args.text:
        print(_format_text(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
