#!/usr/bin/env python
"""Reproducible attribution for the two identical-area parcel downloads.

Uses only the pinned PyPNG public API (png.Reader / png.Writer); the
library itself is not modified. File observations are never mutated;
the two causal readings are replayed on top of the same bytes.

Subcommands:
  receipt     PNG public receipt: index histogram, palette alpha (tRNS),
              pHYs units, downstream valid-pixel count and area.
  compare     Prove the two downloads are byte-identical and yield
              identical downstream results.
  replay      Replay one causal reading (--scenario jia|yi) and report
              the true area and the misjudged positions under it.
  adjudicate  Apply one external evidence file and report what it
              excludes and what is still missing.
  selftest    Run the whole chain with assertions.
"""
import argparse
import hashlib
import os
import sys
from collections import Counter

import png

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")

JIA_DOWNLOAD = os.path.join(OUT, "parcel_jia_download.png")
YI_DOWNLOAD = os.path.join(OUT, "parcel_yi_download.png")
OLD_ARCHIVE = os.path.join(OUT, "old_archive_ratio_only.png")

INDEX_NORMAL = 1
INDEX_EMPTY_INTENDED = 3
INDEX_DECLARED_TRANSPARENT = 7

FILE_PX_PER_M = (100, 200)
JIA_TRUE_PX_PER_M = (200, 200)


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def observe(path):
    reader = png.Reader(file=open(path, "rb"))
    reader.preamble()
    width, height = reader.width, reader.height
    transparent_indices = set()
    if reader.trns:
        for index, alpha in enumerate(reader.trns):
            if alpha == 0:
                transparent_indices.add(index)
    palette = list(reader.palette()) if reader.plte else None
    phys = None
    if getattr(reader, "x_pixels_per_unit", None):
        phys = (reader.x_pixels_per_unit,
                reader.y_pixels_per_unit,
                reader.unit_is_meter)
    _, _, rows, info = png.Reader(file=open(path, "rb")).read()
    pixels = [list(row) for row in rows]
    hist = Counter(value for row in pixels for value in row)
    return {
        "path": path,
        "width": width,
        "height": height,
        "bitdepth": info["bitdepth"],
        "palette": palette,
        "transparent_indices": sorted(transparent_indices),
        "phys": phys,
        "pixels": pixels,
        "hist": hist,
    }


def transparent_positions(obs):
    positions = []
    for y, row in enumerate(obs["pixels"]):
        for x, value in enumerate(row):
            if value in obs["transparent_indices"]:
                positions.append((x, y))
    return positions


def index_positions(obs, index):
    return [(x, y) for y, row in enumerate(obs["pixels"])
            for x, value in enumerate(row) if value == index]


def downstream(obs):
    total = obs["width"] * obs["height"]
    dropped = len(transparent_positions(obs))
    valid = total - dropped
    phys = obs["phys"]
    if phys is None or not phys[2]:
        return {"valid": valid, "dropped": dropped, "area": None}
    area = valid / float(phys[0] * phys[1])
    return {"valid": valid, "dropped": dropped, "area": area}


def cmd_receipt(path):
    obs = observe(path)
    print("file:            %s" % obs["path"])
    print("sha256:          %s" % sha256(path))
    print("size:            %dx%d  bitdepth=%d" %
          (obs["width"], obs["height"], obs["bitdepth"]))
    print("index histogram: %s" %
          ", ".join("index %d x %d" % (k, obs["hist"][k])
                    for k in sorted(obs["hist"])))
    if obs["palette"] is not None:
        print("palette (PLTE+tRNS via Reader.palette()):")
        for index, entry in enumerate(obs["palette"]):
            print("  index %d: %s" % (index, tuple(entry)))
    print("transparent indices (tRNS alpha=0): %s" %
          (obs["transparent_indices"] or "none"))
    if obs["phys"] is None:
        print("pHYs:            absent")
    else:
        x, y, meter = obs["phys"]
        unit = "meter" if meter else "unspecified (ratio only)"
        print("pHYs:            %d x %d px/unit, unit=%s" % (x, y, unit))
    result = downstream(obs)
    print("downstream valid pixels: %d (dropped %d as transparent)"
          % (result["valid"], result["dropped"]))
    if result["area"] is None:
        print("downstream area: REFUSED - no meter declaration; "
              "a pixel ratio alone is not a scale")
    else:
        print("downstream area: %.6g m^2" % result["area"])
    return obs


def cmd_compare(path_a, path_b):
    hash_a, hash_b = sha256(path_a), sha256(path_b)
    print("A %s\n  sha256 %s" % (path_a, hash_a))
    print("B %s\n  sha256 %s" % (path_b, hash_b))
    print("byte-identical: %s" % (hash_a == hash_b))
    result_a = downstream(observe(path_a))
    result_b = downstream(observe(path_b))
    print("downstream A: valid=%d area=%s" % (result_a["valid"], result_a["area"]))
    print("downstream B: valid=%d area=%s" % (result_b["valid"], result_b["area"]))
    print("downstream identical: %s" % (result_a == result_b))
    if hash_a == hash_b and result_a == result_b:
        print("=> bytes + final result cannot separate the two causes; "
              "external evidence is required")


def cmd_replay(path, scenario):
    obs = observe(path)
    result = downstream(obs)
    print("file observation (unchanged): valid=%d area=%.6g m^2 under file pHYs"
          % (result["valid"], result["area"]))
    if scenario == "jia":
        x_ppm, y_ppm = JIA_TRUE_PX_PER_M
        valid_positions = [
            (x, y) for y in range(obs["height"]) for x in range(obs["width"])
            if obs["pixels"][y][x] != INDEX_DECLARED_TRANSPARENT
        ]
        misjudged = []
        print("scenario jia: fully resampled; true grid %dx%d px/m; "
              "old pHYs %dx%d was copied; index 7 truly transparent"
              % (x_ppm, y_ppm, *FILE_PX_PER_M))
    else:
        x_ppm, y_ppm = FILE_PX_PER_M
        valid_positions = [
            (x, y) for y in range(obs["height"]) for x in range(obs["width"])
            if obs["pixels"][y][x] != INDEX_EMPTY_INTENDED
        ]
        misjudged = index_positions(obs, INDEX_DECLARED_TRANSPARENT)
        print("scenario yi: pHYs %dx%d px/m is correct; empty class is "
              "index %d but was misconfigured to index %d, which is a "
              "valid class here"
              % (x_ppm, y_ppm, INDEX_EMPTY_INTENDED,
                 INDEX_DECLARED_TRANSPARENT))
    area = len(valid_positions) / float(x_ppm * y_ppm)
    print("true valid pixels under this reading: %d" % len(valid_positions))
    print("true area under this reading: %.6g m^2" % area)
    if misjudged:
        print("positions misjudged by downstream (valid but dropped): %s"
              % misjudged)
    else:
        print("positions misjudged by downstream: none; "
              "the error is the stale pHYs scale, not the mask")
    print("NOTE: this area holds only if this scenario's assumptions hold; "
          "it must not be imposed on the other reading")


def load_evidence(path):
    evidence = {"points": []}
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key == "point":
            x, y, verdict = [part.strip() for part in value.split(",")]
            evidence["points"].append(((int(x), int(y)), verdict))
        else:
            evidence[key] = value.strip()
    return evidence


def cmd_adjudicate(png_path, evidence_path):
    obs = observe(png_path)
    evidence = load_evidence(evidence_path)
    kind = evidence.get("type")
    print("evidence: %s (type=%s)" % (evidence_path, kind))
    if kind == "boundaries":
        width_m = float(evidence["width_m"])
        height_m = float(evidence["height_m"])
        implied = (obs["width"] / width_m, obs["height"] / height_m)
        print("parcel boundaries: %.6g m x %.6g m -> true grid %.6g x %.6g px/m"
              % (width_m, height_m, implied[0], implied[1]))
        for name, grid in (("jia", JIA_TRUE_PX_PER_M),
                           ("yi", FILE_PX_PER_M)):
            match = all(abs(a - b) < 1e-9 for a, b in zip(implied, grid))
            print("  scenario %s geometry %s: %s"
                  % (name, grid, "CONSISTENT" if match else "EXCLUDED"))
        print("still missing: whether index 7 is truly empty; boundaries "
              "say nothing about class validity, so the valid-pixel count "
              "(8 vs 14) and hence the area stay open")
    elif kind == "field_review":
        for (x, y), verdict in evidence["points"]:
            index = obs["pixels"][y][x]
            print("  point (%d,%d) holds index %d; field verdict: %s"
                  % (x, y, index, verdict))
        verdict7 = [v for (p, v) in evidence["points"]
                    if obs["pixels"][p[1]][p[0]] == INDEX_DECLARED_TRANSPARENT]
        if verdict7 and verdict7[0] == "valid":
            print("  => EXCLUDED: jia's claim that index 7 is truly "
                  "transparent; consistent with yi's misconfiguration")
        elif verdict7:
            print("  => EXCLUDED: yi's claim that index 7 is a valid class; "
                  "consistent with jia")
        print("still missing: the true metric scale; point validity says "
              "nothing about px/m, so the area magnitude stays open "
              "without boundaries or a trustworthy pHYs")
    else:
        print("unknown evidence type: %s" % kind)
        return 2
    return 0


def cmd_selftest():
    for path in (JIA_DOWNLOAD, YI_DOWNLOAD, OLD_ARCHIVE):
        if not os.path.exists(path):
            print("missing %s; run make_samples.py first" % path)
            return 2
    obs = observe(JIA_DOWNLOAD)
    assert obs["hist"][7] == 8 and obs["hist"][3] == 2 and obs["hist"][1] == 6
    assert obs["transparent_indices"] == [7]
    assert obs["phys"] == (100, 200, True)
    assert downstream(obs)["area"] == 8 / 20000.0
    assert sha256(JIA_DOWNLOAD) == sha256(YI_DOWNLOAD)
    old = observe(OLD_ARCHIVE)
    assert old["phys"] == (100, 200, False)
    assert downstream(old)["area"] is None
    jia_area = 8 / float(200 * 200)
    yi_area = 14 / float(100 * 200)
    print("selftest ok: downstream=%.6g m^2, jia=%.6g m^2, yi=%.6g m^2, "
          "all distinct" % (downstream(obs)["area"], jia_area, yi_area))
    assert len({downstream(obs)["area"], jia_area, yi_area}) == 3
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("receipt")
    p.add_argument("png")
    p = sub.add_parser("compare")
    p.add_argument("png_a")
    p.add_argument("png_b")
    p = sub.add_parser("replay")
    p.add_argument("png")
    p.add_argument("--scenario", choices=["jia", "yi"], required=True)
    p = sub.add_parser("adjudicate")
    p.add_argument("png")
    p.add_argument("evidence")
    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    if args.command == "receipt":
        cmd_receipt(args.png)
    elif args.command == "compare":
        cmd_compare(args.png_a, args.png_b)
    elif args.command == "replay":
        cmd_replay(args.png, args.scenario)
    elif args.command == "adjudicate":
        return cmd_adjudicate(args.png, args.evidence)
    elif args.command == "selftest":
        return cmd_selftest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
