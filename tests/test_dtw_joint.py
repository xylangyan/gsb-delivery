import math

import pytest

from dtaidistance import dtw, dtw_joint


VIB_NEW = [0., 0, 1, 2, 1, 0, 1, 0, 0, 2, 1, 0, 0]
VIB_REF = [0., 1, 2, 3, 1, 0, 0, 0, 2, 1, 0, 0, 0]
CUR_NEW = [0., 1, 1, 3, 2, 0, 0, 1, 0, 0, 1, 2, 1]
CUR_REF = [0., 0, 2, 1, 0, 0, 1, 0, 0, 0, 2, 1, 0]


def _series():
    return {"vibration": (VIB_NEW, VIB_REF), "current": (CUR_NEW, CUR_REF)}


def _sq(a, b):
    return dtw.distance(a, b, use_c=False) ** 2


def test_unconstrained_reduces_to_independent_dtw():
    result = dtw_joint.joint_alignment(_series())
    for name, (a, b) in _series().items():
        assert result.channels[name].cost == pytest.approx(_sq(a, b))
        assert result.channel_distances[name] == pytest.approx(
            dtw.distance(a, b, use_c=False))


def test_release_matches_unconstrained_dtw():
    anchors = [{"id": "mid", "vibration": (6, 6), "current": (5, 5)}]
    result = dtw_joint.joint_alignment(_series(), anchors)
    released = result.release()
    for name, (a, b) in _series().items():
        assert released[name] == pytest.approx(_sq(a, b))
    plain = dtw_joint.release(_series())
    assert plain == pytest.approx(released)


def test_released_alignment_keeps_path_costs():
    anchors = [{"id": "mid", "vibration": (6, 6), "current": (5, 5)}]
    result = dtw_joint.joint_alignment(_series(), anchors)
    released = result.released_alignment()
    for name, (a, b) in _series().items():
        channel = released.channels[name]
        assert channel.cost == pytest.approx(_sq(a, b))
        assert dtw_joint.path_cost(a, b, channel.path) == pytest.approx(channel.cost)


def test_anchor_points_are_on_both_paths():
    anchors = [
        {"id": "start", "vibration": (1, 1), "current": (1, 1)},
        {"id": "shift", "vibration": (8, 9), "current": (7, 8)},
    ]
    result = dtw_joint.joint_alignment(_series(), anchors)
    for name in _series():
        channel = result.channels[name]
        for placement in channel.anchors:
            assert placement.point in channel.path
        assert dtw_joint.path_cost(*_series()[name], channel.path) == pytest.approx(
            channel.cost)
    assert result.event_positions("shift") == {
        "vibration": (8, 9), "current": (7, 8)}


def test_buffer_allows_small_repeat_and_is_limited():
    anchors = [{"id": "p", "vibration": (6, 6), "current": (5, 5)}]
    exact = dtw_joint.joint_alignment(_series(), anchors)
    buffered = dtw_joint.joint_alignment(_series(), anchors, buffer=1)
    assert buffered.cost <= exact.cost
    for placement in buffered.channels["vibration"].anchors:
        assert abs(placement.first - 6) <= 1
        assert abs(placement.second - 6) <= 1
    # A zero buffer on the vibration channel keeps the anchor exact.
    per_channel = dtw_joint.joint_alignment(
        _series(), anchors, buffer={"vibration": 0, "current": 1})
    assert per_channel.event_positions("p")["vibration"] == (6, 6)

    with pytest.raises(ValueError):
        dtw_joint.joint_alignment(_series(), anchors, order_errors=["not-a-pair"])


def test_missed_pulse_second_candidate_anchor():
    # The current instrument missed the middle pulse; the vibration channel
    # has two candidates for the same physical event.
    anchors = [
        {"id": "start", "vibration": (1, 1), "current": (1, 1)},
        {"id": "middle", "vibration": [(5, 6), (6, 6)]},
        {"id": "end", "vibration": (12, 12), "current": (12, 12)},
    ]
    result = dtw_joint.joint_alignment(_series(), anchors)
    assert result.feasible
    assert "middle" in result.anchor_map
    assert "vibration" in result.event_positions("middle")
    assert "current" not in result.event_positions("middle")
    assert result.channels["vibration"].event_position("middle") in [(5, 6), (6, 6)]


def test_order_error_inverts_a_pair_but_keeps_others():
    anchors = [
        {"id": "a", "vibration": (1, 1), "current": (1, 1)},
        {"id": "b", "vibration": (2, 2), "current": (2, 2)},
        {"id": "c", "vibration": (3, 3), "current": (3, 3)},
    ]
    result = dtw_joint.joint_alignment(_series(), anchors, order_errors=[("a", "b")])
    assert result.anchor_order.index("c") == 2
    assert set(result.anchor_order[:2]) == {"a", "b"}


def test_unknown_anchor_reports_group():
    anchors = [
        {"id": "ok", "vibration": (1, 1), "current": (1, 1)},
        {"id": "bad", "vibration": (99, 1), "current": (1, 1)},
    ]
    with pytest.raises(dtw_joint.InfeasibleJointAlignment) as exc:
        dtw_joint.joint_alignment(_series(), anchors)
    assert "bad" in exc.value.groups


def test_infeasible_result_mode_reports_conflicts():
    anchors = [
        {"id": "a", "vibration": (8, 8), "current": (8, 8)},
        {"id": "b", "vibration": (2, 2), "current": (2, 2)},
    ]
    result = dtw_joint.joint_alignment(_series(), anchors, on_infeasible="result")
    assert not result.feasible
    assert result.cost == math.inf
    assert {"a", "b"}.issubset(result.conflicts)
    assert result.paths == {"vibration": [], "current": []}


def test_tie_break_prefers_vibration_sample_numbers():
    # Two equal-cost anchored plans; the vibration path with the smaller
    # first sample sequence must win.
    series = {
        "vibration": ([0., 0, 0, 0], [0., 0, 0, 0]),
        "current": ([0., 0, 0, 0], [0., 0, 0, 0]),
    }
    anchors = [{"id": "p", "vibration": [(1, 2), (2, 1)]}]
    result = dtw_joint.joint_alignment(series, anchors)
    vibration = result.channels["vibration"]
    assert vibration.event_position("p") == (1, 2)


def test_memory_is_not_product_of_channel_grids():
    # A long but sparse anchor list should not allocate a product matrix.
    n = 400
    a = [float(i % 7) for i in range(n)]
    b = [float((i * 3) % 7) for i in range(n)]
    series = {"vibration": (a, b), "current": (b, a)}
    anchors = [{"id": "p{}".format(i), "vibration": (i, i), "current": (i, i)}
               for i in range(20, n, 60)]
    result = dtw_joint.joint_alignment(series, anchors)
    assert result.feasible
    assert result.cost >= 0
    for placement_list in result.anchors.values():
        assert len(placement_list) == len(anchors)


if __name__ == "__main__":
    test_unconstrained_reduces_to_independent_dtw()
    test_release_matches_unconstrained_dtw()
    test_anchor_points_are_on_both_paths()
    test_buffer_allows_small_repeat_and_is_limited()
    test_missed_pulse_second_candidate_anchor()
    test_order_error_inverts_a_pair_but_keeps_others()
    test_unknown_anchor_reports_group()
    test_infeasible_result_mode_reports_conflicts()
    test_tie_break_prefers_vibration_sample_numbers()
    test_memory_is_not_product_of_channel_grids()

def test_compare_two_trials_reports_common_event():
    trial = {
        "vibration": [0., 1, 2, 1, 0, 1, 2, 1, 0],
        "current": [0., 0, 1, 2, 1, 0, 1, 0, 0],
    }
    control = {
        "vibration": [0., 1, 2, 1, 0, 0, 1, 2, 1],
        "current": [0., 1, 2, 0, 0, 0, 0, 0, 0],
    }
    anchors = [{"id": "shift", "vibration": (4, 4), "current": (3, 3)}]
    result = dtw_joint.compare(trial, control, anchors)
    assert result.feasible
    assert result.event_positions("shift") == {
        "vibration": (4, 4), "current": (3, 3)}
    assert result.cost == pytest.approx(
        result.channels["vibration"].cost + result.channels["current"].cost)


def test_flat_anchor_tuple_form():
    trial = {"vibration": [0., 1, 2, 1, 0], "current": [0., 0, 1, 0, 0]}
    control = {"vibration": [0., 1, 2, 1, 0], "current": [0., 1, 0, 0, 0]}
    result = dtw_joint.joint_alignment(
        {"vibration": (trial["vibration"], control["vibration"]),
         "current": (trial["current"], control["current"])},
        anchors=[(0, 0, 0, 0), (4, 4, 4, 4)])
    assert result.event_positions(0) == {"vibration": (0, 0), "current": (0, 0)}
    assert result.event_positions(1) == {"vibration": (4, 4), "current": (4, 4)}


def test_order_error_rescues_inverted_pair():
    series = {
        "vibration": ([0., 0, 0, 0], [0., 0, 0, 0]),
        "current": ([0., 0, 0, 0], [0., 0, 0, 0]),
    }
    anchors = [
        {"id": "a", "vibration": (2, 2), "current": (2, 2)},
        {"id": "b", "vibration": (1, 1), "current": (1, 1)},
    ]
    with pytest.raises(dtw_joint.InfeasibleJointAlignment):
        dtw_joint.joint_alignment(series, anchors)
    result = dtw_joint.joint_alignment(series, anchors, order_errors=[("a", "b")])
    assert result.anchor_order == ["b", "a"]


def test_long_segment_path_matches_cost():
    n = 700
    a = [float(i % 5) for i in range(n)]
    b = [float((i * 2) % 5) for i in range(n)]
    cost, path = dtw_joint._segment_path(a, b, None, (n - 1, n - 1))
    assert cost == pytest.approx(dtw_joint._segment_cost(a, b, None, (n - 1, n - 1)))
    assert dtw_joint.path_cost(a, b, path) == pytest.approx(cost)
    assert path[0] == (0, 0) and path[-1] == (n - 1, n - 1)
