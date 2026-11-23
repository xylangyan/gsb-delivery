import itertools
import os
import tempfile

import partridge as ptg
import pytest

from partridge import extract as extract_module
from partridge.transfers import can_transfer_feed
from .helpers import fixture


FIXTURE = "platform-transfer-2026"
MULTI = "multi-witness-2026"


def answers(feed):
    return {
        pair: can_transfer_feed(feed, *pair)
        for pair in [
            ("IN-1", "OUT-1"),
            ("IN-2", "OUT-1"),
            ("IN-1", "OTHER-1"),
        ]
    }


def test_source_answers():
    feed = ptg.load_raw_feed(fixture(FIXTURE))
    assert answers(feed) == {
        ("IN-1", "OUT-1"): True,
        ("IN-2", "OUT-1"): False,
        ("IN-1", "OTHER-1"): True,
    }


def test_parent_station_survives_view():
    feed = ptg.load_feed(
        fixture(FIXTURE), view={"stops.txt": {"stop_id": ["HUB-A", "HUB-B"]}}
    )
    assert set(feed.stops.stop_id) == {"HUB", "HUB-A", "HUB-B"}
    parents = dict(zip(feed.stops.stop_id, feed.stops.parent_station.fillna("")))
    assert parents == {"HUB": "", "HUB-A": "HUB", "HUB-B": "HUB"}


def test_parent_station_carried_once():
    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(
            routes=["RIN", "ROUT"], stops=["HUB-A", "HUB-B", "SRC", "DST"]
        )
        survey = [
            ptg.Query("IN-1", "OUT-1", True),
            ptg.Query("IN-2", "OUT-1", False),
        ]
        result = ptg.extract_minimal_feed(fixture(FIXTURE), outfile, permit, survey)

        assert result.weight == result.stop_time_rows + result.parent_rows + result.rule_rows
        assert result.parent_rows == 1
        assert result.verified
        assert not result.unpreservable
        assert result.carried_trips == ["IN-1", "IN-2", "OUT-1"]

        reread = ptg.load_feed(outfile)
        assert can_transfer_feed(reread, "IN-1", "OUT-1") is True
        assert can_transfer_feed(reread, "IN-2", "OUT-1") is False
        assert "OTHER-1" not in set(reread.trips.trip_id)


def test_weight_counts_stop_time_parent_and_rule_rows():
    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(
            routes=["RIN", "ROUT"], stops=["HUB-A", "HUB-B", "SRC", "DST"]
        )
        result = ptg.extract_minimal_feed(
            fixture(FIXTURE),
            outfile,
            permit,
            [ptg.Query("IN-1", "OUT-1", True)],
        )
        # Sharing HUB-B needs two stop-time rows and one parent row; the
        # stop-pair permission keeps both platforms alive.
        assert (result.stop_time_rows, result.parent_rows, result.rule_rows) == (2, 1, 1)
        assert result.weight == 4


def test_scope_loss_reported_not_silently_narrowed():
    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(
            routes=["RIN", "ROUT"], stops=["HUB-A", "HUB-B", "SRC", "DST"]
        )
        result = ptg.extract_minimal_feed(
            fixture(FIXTURE),
            outfile,
            permit,
            [ptg.Query("IN-2", "OUT-1", False)],
        )
        assert result.verified
        assert not result.unpreservable
        # HUB-A is only named by the stop-pair permission; without a carried
        # stop-time row it cannot survive a re-read, so the permission is
        # reported as a scope loss rather than trimmed to one platform.
        assert [loss.rule for loss in result.scope_losses] == [
            ("HUB-A", "HUB-B", "", "", 0)
        ]
        reread = ptg.load_feed(outfile)
        assert (
            can_transfer_feed(reread, "IN-2", "OUT-1") is False
        )


def test_unpreservable_when_permit_hides_prohibition():
    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(
            routes=["RIN", "ROUT"],
            stops=["HUB-A", "HUB-B", "SRC", "DST"],
            rows={"transfers.txt": [("HUB-A", "HUB-B", "", "", 0)]},
        )
        result = ptg.extract_minimal_feed(
            fixture(FIXTURE),
            outfile,
            permit,
            [ptg.Query("IN-1", "OUT-1", True), ptg.Query("IN-2", "OUT-1", False)],
        )
        assert result.verified
        assert {item.from_trip_id for item in result.unpreservable} == {"IN-2"}
        assert "prohibition" in result.unpreservable[0].reason


def test_unauthorized_trip_cannot_enter_answer():
    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(
            routes=["RIN", "ROUT"],
            stops=["HUB-A", "HUB-B", "SRC", "DST"],
        )
        result = ptg.extract_minimal_feed(
            fixture(FIXTURE),
            outfile,
            permit,
            [ptg.Query("IN-1", "OUT-1", True)],
        )
        assert result.verified
        reread = ptg.load_feed(outfile)
        assert "OTHER-1" not in set(reread.trips.trip_id)
        assert can_transfer_feed(reread, "IN-1", "OTHER-1") is False


def test_out_of_window_trip_pair_is_unpreservable():
    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(trips=["IN-1", "IN-2"], stops=["HUB-A", "HUB-B"])
        result = ptg.extract_minimal_feed(
            fixture(FIXTURE),
            outfile,
            permit,
            [ptg.Query("IN-1", "OUT-1", True)],
        )
        assert result.unpreservable
        assert "outside" in result.unpreservable[0].reason


def test_missing_files_leave_nothing_answerable():
    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(
            routes=["RIN", "ROUT"],
            stops=["HUB-A", "HUB-B"],
            files=["trips.txt", "stop_times.txt", "stops.txt"],
        )
        result = ptg.extract_minimal_feed(
            fixture(FIXTURE),
            outfile,
            permit,
            [ptg.Query("IN-1", "OUT-1", True)],
        )
        assert result.unpreservable
        assert "transfers.txt" in result.unpreservable[0].reason


def test_choice_is_cheapest_of_all_witnesses():
    raw = extract_module._Raw(ptg.load_raw_feed(fixture(MULTI)))
    permit = ptg.Permit(stops=["H1-A", "H1-B", "H2-A", "H2-B"])
    allowed = extract_module._allowed_stops(raw, permit)
    query = ptg.Query("IN", "OUT", True)
    entries = [(query, raw.witness_pairs("IN", "OUT"))]

    weights = []
    for choice in itertools.product(*[options for _q, options in entries]):
        plan = extract_module._plan_for(raw, permit, entries, choice, allowed)
        weights.append(plan.weight()[0])
    assert len(weights) > 1

    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        result = ptg.extract_minimal_feed(
            fixture(MULTI), outfile, permit, [query]
        )
        assert result.weight == min(weights)
        assert result.verified
        reread = ptg.load_feed(outfile)
        assert can_transfer_feed(reread, "IN", "OUT") is True


def test_extracts_from_zip_input():
    from .helpers import zip_file

    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(
            routes=["RIN", "ROUT"], stops=["HUB-A", "HUB-B", "SRC", "DST"]
        )
        result = ptg.extract_minimal_feed(
            zip_file(FIXTURE), outfile, permit, [ptg.Query("IN-1", "OUT-1", True)]
        )
        assert result.verified
        assert result.weight == 4


def test_reported_weight_matches_carried_rows():
    import zipfile

    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        permit = ptg.Permit(
            routes=["RIN", "ROUT"], stops=["HUB-A", "HUB-B", "SRC", "DST"]
        )
        result = ptg.extract_minimal_feed(
            fixture(FIXTURE),
            outfile,
            permit,
            [ptg.Query("IN-1", "OUT-1", True), ptg.Query("IN-2", "OUT-1", False)],
        )
        with zipfile.ZipFile(outfile) as archive:
            stop_times = archive.read("stop_times.txt").decode().strip().splitlines()
            stops = archive.read("stops.txt").decode().strip().splitlines()
            transfers = archive.read("transfers.txt").decode().strip().splitlines()

        stop_time_rows = len(stop_times) - 1
        parent_rows = sum(
            1
            for line in stops[1:]
            if line.split(",")[0] == "HUB"
        )
        rule_rows = len(transfers) - 1
        assert (stop_time_rows, parent_rows, rule_rows) == (
            result.stop_time_rows,
            result.parent_rows,
            result.rule_rows,
        )
        assert result.weight == stop_time_rows + parent_rows + rule_rows


def test_nested_parent_kept_and_counted_once():
    with tempfile.TemporaryDirectory() as tmpdir:
        outfile = os.path.join(tmpdir, "slim.zip")
        result = ptg.extract_minimal_feed(
            fixture("nested-parents-2026"),
            outfile,
            ptg.Permit(stops=["P-A", "P-B"]),
            [ptg.Query("X", "Y", True)],
        )
        reread = ptg.load_feed(outfile)
        assert set(reread.stops.stop_id) == {"P", "P-A", "P-B"}
        assert result.parent_rows == 1
        assert result.verified
        assert can_transfer_feed(reread, "X", "Y") is True


def test_trip_rule_overrides_station_rule():
    with tempfile.TemporaryDirectory() as tmpdir:
        directory = tmpdir

        def write(name, text):
            with open(os.path.join(directory, name), "w") as handle:
                handle.write(text.strip() + "\n")

        write(
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://x,Etc/UTC",
        )
        write(
            "routes.txt",
            "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3",
        )
        write(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
            "start_date,end_date\nS,1,1,1,1,1,1,1,20260101,20261231",
        )
        write(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
            "H,H,0,0,1,\nA,PA,0,0,0,H\nB,PB,0,0,0,H",
        )
        write("trips.txt", "route_id,service_id,trip_id\nR,S,X\nR,S,Y")
        write(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "X,08:00:00,08:00:00,A,1\nY,08:10:00,08:10:00,B,1",
        )
        write(
            "transfers.txt",
            "from_stop_id,to_stop_id,from_trip_id,to_trip_id,transfer_type\n"
            "H,H,,,3\n,,X,Y,0",
        )
        feed = ptg.load_raw_feed(directory)
        # A station-level prohibition is overridden by a trip-pair permission.
        assert can_transfer_feed(feed, "X", "Y") is True


@pytest.mark.parametrize("route_ids", [["RIN"], ["ROUT"]])
def test_route_view_still_works(route_ids):
    feed = ptg.load_feed(fixture(FIXTURE), view={"trips.txt": {"route_id": route_ids}})
    expected = set(
        ptg.load_raw_feed(fixture(FIXTURE))
        .trips.query("route_id in @route_ids")
        .trip_id
    )
    assert set(feed.trips.trip_id) == expected
