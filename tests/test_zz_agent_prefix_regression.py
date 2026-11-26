import numpy as np

from kneed import KneeLocator
from kneed.prefix_regression import (
    expand_segments,
    plateau_certificate,
    regress_prefix_range,
)


CLUE_X = [1, 2, 4, 7, 9, 13, 18, 24, 30, 31, 32]
CLUE_Y = [-8, 0, 8, 10, 10, 16, 22, 26, 32, 32, 42]


def test_clue_disagreement_last_one_prefix():
    report = regress_prefix_range(CLUE_X, CLUE_Y, 9, 11)

    assert report["first_disagreement"] == 10
    assert report["disagreement_through"] == 10
    assert report["disagreement_intervals"] == [
        {"first_prefix": 10, "through_prefix": 10}
    ]

    detail = report["first_disagreement_detail"]
    assert detail["offline_knee"] == 31
    assert detail["online_knee"] == 9

    offline_trigger = detail["offline"]["final_trigger"]
    online_trigger = detail["online"]["final_trigger"]
    assert offline_trigger["threshold_point"]["x"] == 31
    assert online_trigger["threshold_point"]["x"] == 9
    assert offline_trigger["observed"]["value"] < offline_trigger["threshold"]["value"]
    assert online_trigger["observed"]["value"] < online_trigger["threshold"]["value"]
    assert offline_trigger["observed"]["hex"] == "-0x1.7777777777778p-3"
    assert online_trigger["observed"]["hex"] == "-0x1.3333333333334p-2"


def test_small_prefixes_match_original_library_each_time():
    report = regress_prefix_range(CLUE_X, CLUE_Y, 3, len(CLUE_X))

    for item in report["prefixes"]:
        n_points = item["prefix"]
        offline = KneeLocator(
            CLUE_X[:n_points],
            CLUE_Y[:n_points],
            S=1.0,
            curve="convex",
            direction="increasing",
            interp_method="interp1d",
            online=False,
        )
        online = KneeLocator(
            CLUE_X[:n_points],
            CLUE_Y[:n_points],
            S=1.0,
            curve="convex",
            direction="increasing",
            interp_method="interp1d",
            online=True,
        )
        assert item["offline_knee"] == offline.knee
        assert item["online_knee"] == online.knee


def test_no_disagreement_range_reports_conclusion_evidence():
    report = regress_prefix_range(CLUE_X, CLUE_Y, 9, 9)

    assert report["first_disagreement"] is None
    conclusions = report["evidence"]["prefix_conclusions"]
    assert conclusions == [
        {"prefix": 9, "offline_knee": 30.0, "online_knee": 30.0, "agreed": True}
    ]
    assert report["evidence"]["critical_threshold_evidence"]


def test_expand_segments_allows_equal_y_across_stride_change():
    x, y = expand_segments(
        [
            {"y": 4, "count": 3, "step": 2, "start_x": 10},
            {"y": 4, "count": 3, "step": 5},
        ]
    )

    assert x.tolist() == [10, 12, 14, 19, 24, 29]
    assert y.tolist() == [4.0] * 6
    assert np.all(np.diff(x) > 0)


def test_plateau_certificate_skips_interior_prefixes():
    x = np.arange(3, 40_003, dtype=float)
    y = np.r_[np.linspace(0, 20, 12), np.full(39_988, 20.0)]

    certificate = plateau_certificate(x, y, 100, 40_000, window=3)

    assert certificate["same_y_between_endpoints"] is True
    assert certificate["evaluated_count"] < 20
    assert certificate["skipped_prefix_count"] > 39_000
    assert "all_spot_checks_agreed" in certificate
