"""Read-only duty audit engine for ADTK-style Pipenet incident records.

The engine replays chronological ``fit`` / ``fit_detect`` / ``detect`` records
against the exact ADTK 0.6.2 semantics (verified separately in
``crosscheck_adtk.py`` against the real library) without ever fitting or
mutating any live model: every replay is an in-memory bookkeeping exercise.

Semantics implemented (ADTK 0.6.2):

* ``DummyRegressor(strategy="mean")`` prediction is the mean of the target on
  complete training rows (rows with any missing input column are dropped).
* ``RegressionAD(side="positive", c=3)`` trains an ``InterQuartileRangeAD`` on
  the absolute residuals with ``c=(None, 3)``. A point fires iff the residual
  is positive and strictly greater than ``Q3 + 3 * IQR`` of the training
  absolute residuals. Missing input -> NaN label, never an alarm.
* ``QuantileAD(high=0.5)`` fires iff the value is strictly greater than the
  training median; missing values propagate as NaN.
* ``OrAggregator``: alarm if any branch alarms; NaN if no branch alarms and at
  least one branch is NaN; normal otherwise.
* A fit on a node without any valid training value raises ``RuntimeError`` and
  leaves that node's state untouched; nodes fitted earlier in the same call
  keep their updates (Pipenet executes steps in graph order).
* ``skip_fit`` keeps the node's existing fitted state; the node still runs
  prediction in ``fit_detect`` / ``detect``.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

ALARM = True
NORMAL = False
UNKNOWN = None


# ---------------------------------------------------------------------------
# Numeric helpers (pandas-compatible: linear-interpolation quantiles)
# ---------------------------------------------------------------------------


def quantile(value: Sequence[float], q: float) -> float:
    """Pandas ``Series.quantile(q)`` with the default linear interpolation."""
    vals = sorted(value)
    n = len(vals)
    if n == 1:
        return float(vals[0])
    pos = q * (n - 1)
    low = int(pos)
    high = min(low + 1, n - 1)
    frac = pos - low
    return float(vals[low] * (1 - frac) + vals[high] * frac)


def _complete_rows(columns: Dict[str, Sequence[Optional[float]]]) -> List[Dict[str, float]]:
    length = len(next(iter(columns.values())))
    rows = []
    for i in range(length):
        row = {name: values[i] for name, values in columns.items()}
        if any(v is None for v in row.values()):
            continue
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Fitted node state
# ---------------------------------------------------------------------------


@dataclass
class RegressionState:
    """Bookkeeping state of one positive-side RegressionAD branch."""

    feature_cols: List[str]
    target: str
    c: float
    mean: Optional[float] = None
    bound: Optional[float] = None

    def fit(self, columns: Dict[str, Sequence[Optional[float]]]) -> None:
        rows = _complete_rows(columns)
        if not rows:
            raise RuntimeError("Valid values are not enough for training.")
        targets = [row[self.target] for row in rows]
        mean = sum(targets) / len(targets)
        abs_resid = sorted(abs(t - mean) for t in targets)
        q1 = quantile(abs_resid, 0.25)
        q3 = quantile(abs_resid, 0.75)
        self.mean = mean
        self.bound = q3 + self.c * (q3 - q1)

    def predict_row(self, row: Dict[str, Optional[float]]) -> Optional[bool]:
        if any(row.get(col) is None for col in self.feature_cols):
            return UNKNOWN
        residual = row[self.target] - self.mean
        if residual <= 0:
            return NORMAL
        return ALARM if residual > self.bound else NORMAL

    def explain_row(self, row: Dict[str, Optional[float]]) -> Dict[str, Any]:
        if any(row.get(col) is None for col in self.feature_cols):
            return {"residual": None, "mean": self.mean, "bound": self.bound,
                    "label": UNKNOWN, "reason": "missing input -> NaN"}
        residual = row[self.target] - self.mean
        label = NORMAL
        if residual > 0 and residual > self.bound:
            label = ALARM
        return {"residual": round(residual, 6), "mean": round(self.mean, 6),
                "bound": round(self.bound, 6), "label": label}


@dataclass
class QuantileState:
    column: str
    high_quantile: float
    bound: Optional[float] = None

    def fit(self, columns: Dict[str, Sequence[Optional[float]]]) -> None:
        vals = [v for v in columns[self.column] if v is not None]
        if not vals:
            raise RuntimeError("Valid values are not enough for training.")
        self.bound = quantile(vals, self.high_quantile)

    def predict_row(self, row: Dict[str, Optional[float]]) -> Optional[bool]:
        value = row.get(self.column)
        if value is None:
            return UNKNOWN
        return ALARM if value > self.bound else NORMAL

    def explain_row(self, row: Dict[str, Optional[float]]) -> Dict[str, Any]:
        value = row.get(self.column)
        if value is None:
            return {"value": None, "bound": self.bound, "label": UNKNOWN,
                    "reason": "missing input -> NaN"}
        label = ALARM if value > self.bound else NORMAL
        return {"value": value, "bound": self.bound, "label": label}


def or_aggregate(labels: Sequence[Optional[bool]]) -> Optional[bool]:
    if any(label == ALARM for label in labels):
        return ALARM
    if any(label is None for label in labels):
        return UNKNOWN
    return NORMAL


# ---------------------------------------------------------------------------
# Hypothesis replay
# ---------------------------------------------------------------------------


@dataclass
class BranchSpec:
    name: str
    kind: str  # "regression_positive" or "quantile_high"
    columns: List[str]
    target: Optional[str] = None
    c: Optional[float] = None
    high_quantile: Optional[float] = None
    train: Dict[str, List[Optional[float]]] = field(default_factory=dict)


@dataclass
class Snapshot:
    regression: Dict[str, RegressionState]
    quantile: Dict[str, QuantileState]
    shared_groups: Dict[str, str]

    def clone(self) -> "Snapshot":
        return Snapshot(
            regression={k: RegressionState(**v.__dict__.copy())
                        for k, v in self.regression.items()},
            quantile={k: QuantileState(**v.__dict__.copy())
                      for k, v in self.quantile.items()},
            shared_groups=dict(self.shared_groups),
        )

    def set_regression_mean(self, branch: str, mean: float) -> None:
        owner = self.shared_groups.get(branch, branch)
        for name, group in self.shared_groups.items():
            if group == owner:
                self.regression[name].mean = mean

    def summary(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for name, state in self.regression.items():
            out[name] = {
                "kind": "regression_positive",
                "regressor_mean": None if state.mean is None else round(state.mean, 6),
                "residual_bound": None if state.bound is None else round(state.bound, 6),
                "shared_owner": self.shared_groups.get(name, name),
            }
        for name, state in self.quantile.items():
            out[name] = {
                "kind": "quantile_high",
                "upper_bound": state.bound,
            }
        return out


def build_checkpoint(specs: Sequence[BranchSpec], wiring: Dict[str, Any]) -> Snapshot:
    regression: Dict[str, RegressionState] = {}
    quantile: Dict[str, QuantileState] = {}
    for spec in specs:
        if spec.kind == "regression_positive":
            state = RegressionState(list(spec.columns), spec.target, spec.c)
            state.fit(spec.train)
            regression[spec.name] = state
        elif spec.kind == "quantile_high":
            state = QuantileState(spec.columns[0], spec.high_quantile)
            state.fit(spec.train)
            quantile[spec.name] = state
        else:
            raise ValueError(f"unknown branch kind: {spec.kind}")
    shared_groups: Dict[str, str] = {}
    for group in wiring.get("share_regressor", []):
        anchor = group["anchor"]
        for member in group["members"]:
            shared_groups[member] = anchor
        anchor_mean = regression[anchor].mean
        for member in group["members"]:
            regression[member].mean = anchor_mean
    return Snapshot(regression, quantile, shared_groups)


def _row(columns: Dict[str, List[Optional[float]]], index: int) -> Dict[str, Optional[float]]:
    return {name: values[index] for name, values in columns.items()}


def _branch_labels(snapshot: Snapshot, specs: Sequence[BranchSpec],
                   columns: Dict[str, List[Optional[float]]]) -> List[List[Optional[bool]]]:
    n_rows = len(next(iter(columns.values())))
    per_branch: Dict[str, List[Optional[bool]]] = {}
    for spec in specs:
        labels = []
        for i in range(n_rows):
            row = _row(columns, i)
            if spec.kind == "regression_positive":
                state = snapshot.regression[spec.name]
                labels.append(state.predict_row(row))
            else:
                labels.append(snapshot.quantile[spec.name].predict_row(row))
        per_branch[spec.name] = labels
    return [per_branch[spec.name] for spec in specs]


@dataclass
class ReplayResult:
    choices: Dict[str, str]
    consistent: bool
    snapshot: Optional[Snapshot] = None
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    first_mismatch: Optional[Dict[str, Any]] = None


def _fit_node(snapshot: Snapshot, spec: BranchSpec,
              columns: Dict[str, List[Optional[float]]]) -> None:
    node_columns = {name: columns[name] for name in spec.columns}
    if spec.kind == "regression_positive":
        state = snapshot.regression[spec.name]
        state.fit(node_columns)
        snapshot.set_regression_mean(spec.name, state.mean)
    else:
        snapshot.quantile[spec.name].fit(node_columns)


def replay_history(case: Dict[str, Any], choices: Dict[str, str]) -> ReplayResult:
    specs = [BranchSpec(**spec) for spec in case["branches"]]
    wiring_name = choices["wiring"]
    wiring = next(w for w in case["wirings"] if w["name"] == wiring_name)
    checkpoint = build_checkpoint(specs, wiring)
    snapshot = checkpoint.clone()
    result = ReplayResult(choices=choices, consistent=True, snapshot=snapshot)

    for record in case["records"]:
        condition = record.get("only_if")
        if condition and any(choices.get(k) != v for k, v in condition.items()):
            continue
        op = record["op"]
        columns = record.get("input")
        skip = set(record.get("skip", []))
        entry: Dict[str, Any] = {"id": record["id"], "op": op}

        if op == "reload":
            snapshot = checkpoint.clone()
            result.snapshot = snapshot
            entry["detail"] = "reloaded original checkpoint"
            entry["state_after"] = snapshot.summary()
            result.timeline.append(entry)
            continue

        failed_node = record.get("failed_node")
        if op in ("fit", "fit_detect"):
            for spec in specs:
                if spec.name in skip:
                    continue
                try:
                    _fit_node(snapshot, spec, columns)
                except RuntimeError as exc:
                    entry["failure"] = {"node": spec.name, "message": str(exc)}
                    if failed_node != spec.name:
                        result.consistent = False
                        result.first_mismatch = {
                            "record": record["id"],
                            "reason": f"unexpected failure at node {spec.name}: {exc}",
                        }
                        return result
                    break
            else:
                if failed_node is not None:
                    result.consistent = False
                    result.first_mismatch = {
                        "record": record["id"],
                        "reason": f"expected failure at {failed_node} but call succeeded",
                    }
                    return result

        if op in ("fit_detect", "detect"):
            branch_matrix = _branch_labels(snapshot, specs, columns)
            aggregate = [or_aggregate(labels) for labels in zip(*branch_matrix)]
            entry["branch_labels"] = {
                spec.name: branch_matrix[i] for i, spec in enumerate(specs)
            }
            entry["aggregate"] = aggregate
            observed = record.get("observed")
            if observed is not None:
                observed = [None if v is None else bool(v) for v in observed]
                if observed != aggregate:
                    result.consistent = False
                    for i, (exp, got) in enumerate(zip(observed, aggregate)):
                        if exp != got:
                            result.first_mismatch = {
                                "record": record["id"],
                                "row": i,
                                "expected": exp,
                                "predicted": got,
                                "reason": "aggregate label mismatch",
                            }
                            break
                    return result
        entry["state_after"] = snapshot.summary()
        result.timeline.append(entry)
    return result


def enumerate_histories(case: Dict[str, Any]) -> List[ReplayResult]:
    points = case["choice_points"]
    keys = list(points)
    results = []
    for combo in itertools.product(*(points[k] for k in keys)):
        choices = dict(zip(keys, combo))
        results.append(replay_history(case, choices))
    return results


# ---------------------------------------------------------------------------
# Follow-up minute planning
# ---------------------------------------------------------------------------


def predict_archive(snapshot: Snapshot, specs: Sequence[BranchSpec],
                    minutes: Dict[str, Dict[str, Any]]
                    ) -> Tuple[Dict[str, Optional[bool]], Dict[str, Dict[str, Optional[bool]]]]:
    aggregate_signature: Dict[str, Optional[bool]] = {}
    branch_signature: Dict[str, Dict[str, Optional[bool]]] = {}
    for minute_id, payload in minutes.items():
        columns = payload["input"]
        matrix = _branch_labels(snapshot, specs, columns)
        branch_signature[minute_id] = {
            spec.name: matrix[i][0] for i, spec in enumerate(specs)
        }
        aggregate_signature[minute_id] = or_aggregate([row[0] for row in matrix])
    return aggregate_signature, branch_signature


def _partition(signature_by_history: Dict[str, Dict[str, Optional[bool]]],
               minute_ids: Sequence[str]) -> List[Tuple[str, ...]]:
    groups: Dict[Tuple[Optional[bool], ...], List[str]] = {}
    for key, sig in signature_by_history.items():
        groups.setdefault(tuple(sig[m] for m in minute_ids), []).append(key)
    return tuple(sorted(tuple(sorted(v)) for v in groups.values()))


def minimum_separating_minutes(
    histories: List[ReplayResult], minutes: Dict[str, Dict[str, Any]], case: Dict[str, Any]
) -> Tuple[List[str], Dict[Tuple[Optional[bool], ...], List[str]]]:
    """Smallest archive subset reaching the finest partition detect can reach.

    Histories indistinguishable even with every archive minute stay grouped;
    the caller reports them as evidence gaps rather than forcing more probes.
    """
    specs = [BranchSpec(**spec) for spec in case["branches"]]
    minute_ids = list(minutes)
    signature_by_history: Dict[str, Dict[str, Optional[bool]]] = {}
    for result in histories:
        key = json.dumps(result.choices, sort_keys=True)
        signature_by_history[key] = predict_archive(result.snapshot, specs, minutes)[0]

    finest = _partition(signature_by_history, minute_ids)
    best: Optional[List[str]] = None
    for size in range(1, len(minute_ids) + 1):
        for subset in itertools.combinations(minute_ids, size):
            if _partition(signature_by_history, list(subset)) == finest:
                best = list(subset)
                break
        if best is not None:
            break
    if best is None:
        best = minute_ids
    final_groups: Dict[Tuple[Optional[bool], ...], List[str]] = {}
    for key in signature_by_history:
        sig = tuple(signature_by_history[key][m] for m in best)
        final_groups.setdefault(sig, []).append(key)
    return best, final_groups


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _label_text(label: Optional[bool]) -> str:
    return {ALARM: "告警", NORMAL: "正常", UNKNOWN: "未知"}[label]


def _choice_text(choices: Dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in choices.items())


def audit(case: Dict[str, Any]) -> Dict[str, Any]:
    results = enumerate_histories(case)
    consistent = [r for r in results if r.consistent]
    rejected = [r for r in results if not r.consistent]

    specs = [BranchSpec(**spec) for spec in case["branches"]]
    minutes = case["archive_minutes"]
    chosen, groups = minimum_separating_minutes(consistent, minutes, case)
    signature_lookup: Dict[str, Dict[str, Optional[bool]]] = {}
    branch_signature_lookup: Dict[str, Dict[str, Dict[str, Optional[bool]]]] = {}
    for r in consistent:
        key = json.dumps(r.choices, sort_keys=True)
        agg_sig, branch_sig = predict_archive(r.snapshot, specs, minutes)
        signature_lookup[key] = agg_sig
        branch_signature_lookup[key] = branch_sig

    report_records = [r for r in case["records"]
                      if r["op"] == "fit_detect" and r.get("observed") is not None]
    minute_attribution = []
    if report_records:
        rec = report_records[-1]
        n_rows = len(rec["observed"])
        for i in range(n_rows):
            fired_sets, unknown_branches = [], set()
            for result in consistent:
                entry = next(e for e in result.timeline if e["id"] == rec["id"])
                fired_sets.append({
                    spec.name for spec in specs
                    if entry["branch_labels"][spec.name][i] == ALARM
                })
                unknown_branches.update(
                    spec.name for spec in specs
                    if entry["branch_labels"][spec.name][i] == UNKNOWN
                )
            definite = set.intersection(*fired_sets) if fired_sets else set()
            possible = set.union(*fired_sets) if fired_sets else set()
            minute_attribution.append({
                "row": i,
                "observed": rec["observed"][i],
                "definite_triggers": sorted(definite),
                "possible_triggers": sorted(possible),
                "unknown_branches": sorted(unknown_branches),
            })

    follow_up_groups = []
    evidence_gaps = []
    for sig, keys in groups.items():
        histories_choices = [json.loads(k) for k in keys]
        follow_up_groups.append({
            "signature": dict(zip(chosen, sig)),
            "histories": histories_choices,
            "branch_level": {
                k: branch_signature_lookup[k] for k in keys
            },
        })
        if len(keys) > 1:
            open_dimensions = sorted({
                dim for dim in histories_choices[0]
                if any(c[dim] != histories_choices[0][dim] for c in histories_choices[1:])
            })
            evidence_gaps.append({
                "indistinguishable_histories": histories_choices,
                "choice_dimensions_left_open": open_dimensions,
                "missing_evidence": (
                    "现存进程的 detect 对全部归档分钟给出相同标签；需要该选择点所在时点的"
                    "装载/执行回执，或当时的分支级标签，才能区分。"
                ),
            })

    return {
        "case_id": case.get("case_id"),
        "consistent_histories": [
            {"choices": r.choices,
             "archive_signature": signature_lookup[json.dumps(r.choices, sort_keys=True)],
             "branch_signature": branch_signature_lookup[json.dumps(r.choices, sort_keys=True)],
             "timeline": r.timeline}
            for r in consistent
        ],
        "rejected_histories": [
            {"choices": r.choices, "first_mismatch": r.first_mismatch} for r in rejected
        ],
        "minute_attribution": minute_attribution,
        "follow_up_plan": {
            "minimum_minutes": chosen,
            "groups": follow_up_groups,
        },
        "evidence_gaps": evidence_gaps,
    }


def render_text(report: Dict[str, Any]) -> str:
    lines = [f"核账案例: {report['case_id']}", "=" * 72, ""]
    lines.append(f"相符经过 {len(report['consistent_histories'])} 条，"
                 f"排除 {len(report['rejected_histories'])} 条。")
    lines.append("")
    lines.append("【相符经过与最终模型状态 / 补查签名】")
    for i, hist in enumerate(report["consistent_histories"], 1):
        last_state = hist["timeline"][-1]["state_after"]
        state_text = "; ".join(
            f"{node}: " + (
                f"均值={v.get('regressor_mean')}, 残差界={v.get('residual_bound')}"
                if v["kind"] == "regression_positive"
                else f"上界={v.get('upper_bound')}"
            )
            for node, v in sorted(last_state.items())
        )
        sig = ", ".join(f"{k}={_label_text(v)}"
                        for k, v in hist["archive_signature"].items())
        lines.append(f"  {i}. {_choice_text(hist['choices'])}")
        lines.append(f"     终态: {state_text}")
        lines.append(f"     补查签名: {sig}")
    lines.append("")
    lines.append("【已排除经过及首个对不上的记录】")
    for hist in report["rejected_histories"]:
        mm = hist["first_mismatch"]
        extra = (f"，第 {mm['row'] + 1} 分钟: 预测 {_label_text(mm['predicted'])} "
                 f"vs 日报 {_label_text(mm['expected'])}") if "row" in mm else ""
        lines.append(f"  - {_choice_text(hist['choices'])} -> 记录 {mm['record']}{extra}")
        lines.append(f"    原因: {mm['reason']}")
    lines.append("")
    lines.append("【日报逐分钟告警归属（跨全部相符经过）】")
    for item in report["minute_attribution"]:
        lines.append(
            f"  分钟 {item['row'] + 1} 日报={_label_text(item['observed'])} "
            f"确定触发={item['definite_triggers'] or '无'} "
            f"可能触发={item['possible_triggers'] or '无'} "
            f"缺数分支={item['unknown_branches'] or '无'}"
        )
    lines.append("")
    plan = report["follow_up_plan"]
    lines.append(f"【一次派工最少补查分钟】 {plan['minimum_minutes']}（按此顺序派工）")
    for group in plan["groups"]:
        sig = ", ".join(f"{m}={_label_text(v)}" for m, v in group["signature"].items())
        lines.append(f"  签名 [{sig}]: {len(group['histories'])} 条经过")
        for choices in group["histories"]:
            lines.append(f"    - {_choice_text(choices)}")
        for key, branch_sig in group["branch_level"].items():
            chosen_sig = {m: branch_sig[m] for m in plan["minimum_minutes"]}
            detail = "; ".join(
                f"{m}: " + "/".join(
                    f"{b}={_label_text(v)}" for b, v in chosen_sig[m].items()
                ) for m in plan["minimum_minutes"]
            )
            lines.append(f"      分支级 {key}: {detail}")
    lines.append("")
    lines.append("【现存进程也无法分开的历史（证据缺口）】")
    if not report["evidence_gaps"]:
        lines.append("  无。")
    for gap in report["evidence_gaps"]:
        for choices in gap["indistinguishable_histories"]:
            lines.append(f"  - {_choice_text(choices)}")
        lines.append(f"    未决维度: {gap['choice_dimensions_left_open']}")
        lines.append(f"    缺口说明: {gap['missing_evidence']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="只读 ADTK 事故核账入口")
    parser.add_argument("case", help="事故案例 JSON 文件")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    with open(args.case, encoding="utf-8") as handle:
        case = json.load(handle)
    report = audit(case)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
