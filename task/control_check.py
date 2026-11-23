"""Byte-exact controls for mrcfile.update_header_stats in this environment.

Run with: .venv/bin/python task/control_check.py

All header values are printed as the four raw bytes in file order on this
little-endian host.  dmin/dmax/dmean/rms are at byte offsets 76/80/84/216 of
the 1024-byte MRC header.
"""
from fractions import Fraction
import json
import numpy as np
from mrcfile.mrcobject import MrcObject

FIELDS = ("dmin", "dmax", "dmean", "rms")
OFFSETS = {"dmin": 76, "dmax": 80, "dmean": 84, "rms": 216}


def header_bytes(arr):
    obj = MrcObject()
    obj._create_default_attributes()
    obj._set_new_data(np.ascontiguousarray(arr, dtype=np.float32))
    obj.update_header_stats()
    raw = obj.header.tobytes()
    return {name: raw[OFFSETS[name]:OFFSETS[name] + 4].hex() for name in FIELDS}


def exact_stats(arr):
    values = [Fraction(float(x)) for x in np.asarray(arr, dtype=np.float32).ravel()]
    total = sum(values)
    total_sq = sum(x * x for x in values)
    return {
        "n": len(values),
        "sum": str(total),
        "sumsq": str(total_sq),
        "min": float(np.asarray(arr, dtype=np.float32).min()),
        "max": float(np.asarray(arr, dtype=np.float32).max()),
    }


def apply(arr, flat_index, new_value):
    out = np.array(arr, dtype=np.float32, copy=True)
    out.ravel()[flat_index] = np.float32(new_value)
    return out


def show(label, old, new):
    print(f"-- {label}")
    print(" old vector:", np.asarray(old, dtype=np.float32).ravel().tolist())
    print(" new vector:", np.asarray(new, dtype=np.float32).ravel().tolist())
    print(" exact:", json.dumps(exact_stats(old), ensure_ascii=False))
    print(" old bytes:", json.dumps(header_bytes(old), ensure_ascii=False))
    print(" new bytes:", json.dumps(header_bytes(new), ensure_ascii=False))


def show_two_block(label, first, second, corrected_value):
    f1 = np.concatenate([first, second])
    f2 = np.concatenate([second, first])
    n1 = apply(f1, 0, corrected_value)
    n2 = apply(f2, 0, corrected_value)
    print(f"== {label}")
    print(" block A summary:", json.dumps(exact_stats(first), ensure_ascii=False))
    print(" block B summary:", json.dumps(exact_stats(second), ensure_ascii=False))
    print(" summaries equal:",
          exact_stats(first) == exact_stats(second))
    print(f" correction: flat index 0, old {float(first[0])!r}, "
          f"new {corrected_value!r}")
    print(" F1 = A+B; F2 = B+A  (same block multiset, different order)")
    print(" old bytes F1:", json.dumps(header_bytes(f1), ensure_ascii=False))
    print(" old bytes F2:", json.dumps(header_bytes(f2), ensure_ascii=False))
    print(" new bytes F1:", json.dumps(header_bytes(n1), ensure_ascii=False))
    print(" new bytes F2:", json.dumps(header_bytes(n2), ensure_ascii=False))


def main():
    # dmean-only divergence.  A and B have the same summary; both files have
    # two blocks with that summary, so swapping A/B does not change the
    # manifest.  The old header bytes are identical; after the same in-place
    # correction, only dmean differs.
    a = np.array([-1048576.0, 0.5, -16777216.0, 3.0, 1048576.0, 16777216.0],
                 dtype=np.float32)
    b = np.array([-1048576.0, 3.0, 1048576.0, -16777216.0, 0.5, 16777216.0],
                 dtype=np.float32)
    show_two_block("dmean control", a, b, 1.5)

    # rms-only divergence.  U and V have the same summary (they are two
    # permutations of one multiset) and the same old header bytes.  After the
    # same correction, dmin/dmax/dmean bytes are identical while rms differs
    # by one ULP.
    u = np.array([16777216.0, 1048576.0, 3.0, 2.0, -16777216.0, 1000000.0],
                 dtype=np.float32)
    v = np.array([16777216.0, 3.0, 1000000.0, -16777216.0, 2.0, 1048576.0],
                 dtype=np.float32)
    show_two_block("rms control", u, v, 3.0)

    # dmin ambiguity: the correction removes the minimum occurrence in A but
    # not in B.  Same old header and same summary; after correction dmin
    # differs while dmax/dmean/rms agree.
    aa = np.array([1.0, 5.0, 1.0, 4.0, 4.0], dtype=np.float32)
    bb = np.array([1.0, 5.0, 2.0, 2.0, 5.0], dtype=np.float32)
    show("dmin control A", aa, apply(aa, 0, 4.0))
    show("dmin control B", bb, apply(bb, 0, 4.0))

    # dmax ambiguity: the correction removes the maximum occurrence in A but
    # not in B.
    cc = np.array([1.0, 1.0, 4.0, 4.0, 5.0], dtype=np.float32)
    dd = np.array([1.0, 2.0, 2.0, 5.0, 5.0], dtype=np.float32)
    show("dmax control A", cc, apply(cc, 4, 4.0))
    show("dmax control B", dd, apply(dd, 4, 4.0))


if __name__ == "__main__":
    main()
