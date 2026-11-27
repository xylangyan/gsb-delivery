"""Independent cross-check of hand calculations against real ADTK 0.6.2."""
import copy
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "_stubs"))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

import numpy as np
import pandas as pd

from adtk.aggregator import OrAggregator
from adtk.detector import QuantileAD, RegressionAD
from adtk.pipe import Pipenet

RPM = [100.0, 100.0]


class MeanReg:
    def fit(self, X, y):
        self.constant_ = float(np.mean(y))
        return self

    def predict(self, X):
        return np.full(len(X), self.constant_)


def make_pipenet(wiring):
    reg_a = MeanReg()
    reg_b = MeanReg() if wiring == "distinct" else reg_a
    steps = {
        "A": {
            "model": RegressionAD(reg_a, target="loadA", c=3.0, side="positive"),
            "input": "original",
            "subset": ["rpm", "loadA"],
        },
        "B": {
            "model": RegressionAD(reg_b, target="loadB", c=3.0, side="positive"),
            "input": "original",
            "subset": ["rpm", "loadB"],
        },
        "C": {
            "model": QuantileAD(high=0.5),
            "input": "original",
            "subset": ["vib"],
        },
        "or": {"model": OrAggregator(), "input": ["A", "B", "C"]},
    }
    return Pipenet(steps)


def df2(loadA, loadB, vib=(0, 2)):
    return pd.DataFrame(
        {"rpm": RPM, "loadA": list(loadA), "loadB": list(loadB), "vib": list(vib)},
        index=pd.date_range("2026-01-01", periods=2, freq="min"),
    )


def checkpoint_fit(net):
    net.fit(df2([9, 11], [18, 22]))


def states(net):
    out = {}
    for name in ("A", "B"):
        rad = net.steps[name]["model"]
        iqr = rad.pipe_.steps["iqr_ad"]["model"]
        out[name] = (rad.regressor.constant_, iqr.abs_high_)
    out["C"] = net.steps["C"]["model"].abs_high_
    return out


# ---- initial checkpoint, both wirings ----
for wiring in ("distinct", "shared"):
    net = make_pipenet(wiring)
    checkpoint_fit(net)
    print("checkpoint", wiring, states(net))

# ---- make-up fit: skip B, C all-missing -> failure at C ----
report_idx = pd.date_range("2026-11-27", periods=7, freq="min")
loadA7 = [12, 8, 8, 8, 8, np.nan, 25]
loadB7 = [39, 39, 40, 40, 41, 41, 60]
report_df = pd.DataFrame(
    {"rpm": [100.0] * 7, "loadA": loadA7, "loadB": loadB7, "vib": [0.0] * 7},
    index=report_idx,
)
makeup = df2([29, 31], [0, 0], vib=(np.nan, np.nan))

scenarios = {}
for wiring in ("distinct", "shared"):
    # failed process continued
    net = make_pipenet(wiring)
    checkpoint_fit(net)
    try:
        net.fit(makeup, skip_fit=["B"])
    except RuntimeError as e:
        print("make-up fit failed as expected:", e)
    scenarios[f"continue-{wiring}"] = net

    # reload original checkpoint before fit_detect
    net2 = make_pipenet(wiring)
    checkpoint_fit(net2)
    scenarios[f"reload-{wiring}"] = net2

observed = [False] * 5 + [np.nan, True]
print("\n--- fit_detect(skip A,C) report labels ---")
for key, net in scenarios.items():
    labels = net.fit_detect(report_df, skip_fit=["A", "C"])
    got = [None if pd.isna(v) else bool(v) for v in labels]
    exp = [None if pd.isna(v) else bool(v) for v in observed]
    print(key, got, "MATCH" if got == exp else "MISMATCH")
    print("   post fit_detect state:", states(net))
    inter = net.fit_detect(report_df, skip_fit=["A", "C"], return_intermediate=True)
    print("   A:", [None if pd.isna(v) else bool(v) for v in inter["A"]])
    print("   B:", [bool(v) for v in inter["B"]])
    print("   C:", [bool(v) for v in inter["C"]])

# ---- optional post-report calibration on A [49,51], skip B,C ----
calib = df2([49, 51], [0, 0])
print("\n--- after optional calibration ---")
for key, net in scenarios.items():
    n2 = copy.deepcopy(net)
    n2.fit(calib, skip_fit=["B", "C"])
    print(key + "+calib", states(n2))
for key, net in scenarios.items():
    print(key, states(net))

# ---- follow-up detect on archive minutes ----
archives = {
    "m1(35,0)": (35, 0),
    "m2(45,0)": (45, 0),
    "m3(0,55)": (0, 55),
    "m4(0,0)": (0, 0),
}


def followup(net):
    sig = {}
    for label, (a, b) in archives.items():
        d = pd.DataFrame(
            {"rpm": [100.0], "loadA": [float(a)], "loadB": [float(b)], "vib": [0.0]},
            index=pd.date_range("2026-02-01", periods=1, freq="min"),
        )
        v = net.detect(d).iloc[0]
        sig[label] = None if pd.isna(v) else bool(v)
    return sig


print("\n--- follow-up signatures (uncalibrated) ---")
for key, net in scenarios.items():
    print(key, followup(net))
print("--- follow-up signatures (calibrated) ---")
for key, net in scenarios.items():
    n2 = copy.deepcopy(net)
    n2.fit(calib, skip_fit=["B", "C"])
    print(key + "+calib", followup(n2))
