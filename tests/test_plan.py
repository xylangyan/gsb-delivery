import pathlib

import pytest

import py7zr
from py7zr.helpers import calculate_crc32
from py7zr.plan import (
    DeliveryPlan,
    ExtractionPlan,
    VerificationCache,
    blocks_from_header,
    plan_extraction,
)

testdata_path = pathlib.Path(__file__).parent.joinpath("data")


def make_two_block_archive(path: pathlib.Path, names=("a", "b", "c", "d")) -> pathlib.Path:
    """Create a two-block solid archive with one-byte members."""
    a, b, c, d = names
    with py7zr.SevenZipFile(path, "w") as archive:
        archive.writestr(b"a", a)
        archive.writestr(b"b", b)
    with py7zr.SevenZipFile(path, "a") as archive:
        archive.writestr(b"c", c)
        archive.writestr(b"d", d)
    return path


@pytest.fixture()
def fg_archive(tmp_path):
    return make_two_block_archive(tmp_path.joinpath("fg.7z"))


def stage_names(plan: DeliveryPlan) -> list[tuple[str, ...]]:
    return [step.staged_after for step in plan.steps]


def test_two_blocks_need_three_opens_at_quota_one(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        assert [block.names for block in planner.blocks] == [("a", "b"), ("c", "d")]
        plan = planner.build(["b", "d", "a", "c"], quota=1)
    assert plan.feasible
    assert len(plan.block_opens) == 3
    assert plan.total_open_cost == 3
    assert sum(1 for step in plan.steps if step.from_staging) == 1
    assert plan.peak_bytes == 1
    assert all(step.peak_bytes <= 1 for step in plan.steps)


def test_two_blocks_need_one_open_each_at_quota_two(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["b", "d", "a", "c"], quota=2)
    assert plan.feasible
    assert plan.block_opens == [0, 1]
    assert plan.total_open_cost == 2
    assert stage_names(plan) == [("a",), ("a", "c"), ("c",), ()]
    assert plan.peak_bytes == 2


def test_current_delivery_does_not_count_against_quota(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["a", "c"], quota=0)
    assert plan.feasible
    assert plan.block_opens == [0, 1]
    assert plan.peak_bytes == 0


def test_zero_quota_forces_a_reopen_per_request(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["b", "d", "a", "c"], quota=0)
    assert plan.feasible
    assert plan.block_opens == [0, 1, 0, 1]
    assert plan.total_open_cost == 4
    assert plan.peak_bytes == 0
    assert stage_names(plan) == [(), (), (), ()]


def test_repeated_request_reuses_staging(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["b", "b", "d", "d", "a", "c"], quota=1)
    assert plan.feasible
    assert plan.total_open_cost == 4
    # Each repeated request is delivered again; the second "b" and the final
    # "c" are served from a retained copy instead of a new decode.
    assert plan.steps[1].from_staging and plan.steps[1].name == "b"
    assert plan.steps[5].from_staging and plan.steps[5].name == "c"
    assert plan.peak_bytes <= 1


def test_per_block_open_costs_are_honoured(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        unit = planner.build(["b", "d", "a", "c"], quota=2)
        cost_map = ExtractionPlan(planner.blocks, open_costs={0: 100, 1: 1})
        priced = cost_map.build(["b", "d", "a", "c"], quota=2)
    assert unit.open_counts() == {0: 1, 1: 1}
    assert unit.total_open_cost == 2
    assert priced.total_open_cost == 101
    # Costs never change the number of opens; they only weight them.
    assert priced.block_opens == unit.block_opens


def test_plan_executes_and_returns_member_bytes(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        plan = ExtractionPlan.from_archive(archive).build(["b", "d", "a", "c"], quota=1)
        execution = plan.execute(archive)
    assert [delivery.name for delivery in execution.deliveries] == ["b", "d", "a", "c"]
    assert [delivery.data for delivery in execution.deliveries] == [b"b", b"d", b"a", b"c"]
    assert all(delivery.verified for delivery in execution.deliveries)
    assert execution.decoded_blocks == plan.block_opens
    assert execution.verified


def test_real_header_boundaries_are_the_evidence():
    with py7zr.SevenZipFile(testdata_path.joinpath("solid.7z")) as archive:
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["test1.txt", "test/test2.txt"], quota=None)
        execution = plan.execute(archive)
        header = archive.header
    block = planner.blocks[0]
    assert header.main_streams.substreamsinfo.num_unpackstreams_folders == [2]
    assert header.main_streams.packinfo.packsizes == [55]
    assert plan.proof.block_spans == ((0, 32, 55),)
    assert plan.proof.boundaries == ((0, 0, 33), (0, 33, 33))
    assert block.pack_offset == 32
    assert block.pack_size == 55
    assert block.unpack_size == 66
    assert [sub.offset for sub in block.substreams] == [0, 33]
    assert execution.boundary_checks == [(0, 0, 33, True), (0, 1, 66, True)]
    assert [delivery.name for delivery in execution.deliveries] == ["test1.txt", "test/test2.txt"]
    assert all(delivery.verified for delivery in execution.deliveries)
    assert sum(len(delivery.data) for delivery in execution.deliveries) == 66


def test_whole_block_checksum_switches_provisional_to_verified(tmp_path):
    path = make_two_block_archive(tmp_path.joinpath("crc.7z"))
    with py7zr.SevenZipFile(path) as archive:
        folders = archive.header.main_streams.unpackinfo.folders
        folders[0].digestdefined = True
        folders[0].crc = calculate_crc32(b"ab")
        subinfo = archive.header.main_streams.substreamsinfo
        subinfo.digestsdefined = [False] * len(subinfo.digestsdefined)
        planner = ExtractionPlan.from_archive(archive)
        blocks = blocks_from_header(archive.header, archive.pack_data_start, {f.filename: f.id for f in archive.files})
        assert blocks[0].crc32 == calculate_crc32(b"ab")
        assert [sub.crc32 for sub in blocks[0].substreams] == [None, None]
        plan = planner.build(["a", "b"], quota=None)
        statuses = []
        execution = plan.execute(archive, chunk_size=1, on_status=lambda kind, name: statuses.append((kind, name)))
    assert ("provisional", "a") in statuses
    assert statuses.index(("provisional", "a")) < statuses.index(("verified", "a"))
    assert ("verified", "block:0") in statuses
    assert all(delivery.verified for delivery in execution.deliveries)
    assert execution.provisional_names == []
    record = execution.verifications[0]
    assert record.block_scope_crc
    assert record.block_verified


def test_verified_blocks_are_reused_for_same_version(fg_archive):
    cache = VerificationCache()
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        first = planner.build(["b", "d", "a", "c"], quota=2).execute(archive, block_cache=cache)
        second = planner.build(["b", "d", "a", "c"], quota=2).execute(archive, block_cache=cache)
    assert len(cache) == 2
    assert first.decoded_blocks == [0, 1]
    assert first.reused_blocks == []
    assert second.decoded_blocks == []
    assert second.reused_blocks == [0, 1]
    assert all(delivery.verified and delivery.from_cache for delivery in second.deliveries[:2])
    assert [delivery.data for delivery in second.deliveries] == [b"b", b"d", b"a", b"c"]


def test_unreachable_block_marks_deliveries_unverifiable(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        archive.available_end = lambda: archive.pack_data_start + 5  # type: ignore[method-assign]
        planner = ExtractionPlan.from_archive(archive)
        assert planner.unverifiable_blocks() == [0, 1]
        plan = planner.build(["b", "d"], quota=None)
        assert plan.unverifiable == (0, 1)
        execution = plan.execute(archive)
    assert execution.decoded_blocks == []
    assert execution.unverifiable_blocks == [0, 1]
    assert execution.unverifiable_names == ["b", "d"]
    assert not any(delivery.verified for delivery in execution.deliveries)


def test_plan_extraction_helper_matches_archive_method(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        direct = archive.extraction_plan(["b", "d", "a", "c"], staging_quota=2)
        helper = plan_extraction(archive, ["b", "d", "a", "c"], staging_quota=2)
    assert direct.describe() == helper.describe()
    assert direct.steps == helper.steps


def test_unknown_member_is_rejected(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        with pytest.raises(KeyError):
            planner.build(["missing"], quota=None)


def test_sequential_same_block_order_shares_one_decode(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["a", "b", "c", "d"], quota=0)
        execution = plan.execute(archive)
    assert plan.block_opens == [0, 1]
    assert [step.continued for step in plan.steps] == [False, True, False, True]
    assert plan.peak_bytes == 0
    assert execution.decoded_blocks == [0, 1]
    assert [delivery.data for delivery in execution.deliveries] == [b"a", b"b", b"c", b"d"]


def test_whole_block_checksum_failure_flips_deliveries_to_failed(tmp_path):
    path = make_two_block_archive(tmp_path.joinpath("badcrc.7z"))
    with py7zr.SevenZipFile(path) as archive:
        folder = archive.header.main_streams.unpackinfo.folders[0]
        folder.digestdefined = True
        folder.crc = calculate_crc32(b"ab") ^ 0xFFFFFFFF
        subinfo = archive.header.main_streams.substreamsinfo
        subinfo.digestsdefined = [False] * len(subinfo.digestsdefined)
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["a", "b"], quota=None)
        events = []
        execution = plan.execute(
            archive, chunk_size=1, on_status=lambda kind, name: events.append((kind, name))
        )
    assert execution.deliveries[0].provisional or execution.deliveries[0].reason
    assert all(not delivery.verified for delivery in execution.deliveries)
    assert all(delivery.reason and "CRC" in delivery.reason for delivery in execution.deliveries)
    assert ("failed", "a") in events
    assert not execution.verifications[0].block_verified
    assert len(execution.verifications[0].failed) == 2


def test_member_crc_failure_is_isolated_to_that_member(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        subinfo = archive.header.main_streams.substreamsinfo
        # Corrupt only the CRC of block 0's second member.
        subinfo.digests[1] ^= 0xFF
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["a", "b"], quota=None)
        execution = plan.execute(archive)
    first, second = execution.deliveries
    assert first.verified and not first.provisional
    assert not second.verified and second.reason and "checksum" in second.reason
    record = execution.verifications[0]
    assert not record.block_verified
    assert "a" in record.members_verified
    assert "b" in record.failed


def test_boundary_mismatch_is_reported_before_verification(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        # Claim the first member is one byte longer than the block actually
        # provides for it. Execution must not split at the shifted boundary.
        subinfo = archive.header.main_streams.substreamsinfo
        subinfo.unpacksizes[0] += 1
        with pytest.raises(Exception) as excinfo:
            ExtractionPlan.from_archive(archive)
        assert type(excinfo.value).__name__ == "SubstreamBoundaryError"


def test_failed_block_does_not_prevent_later_verified_delivery(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        folder = archive.header.main_streams.unpackinfo.folders[0]
        folder.digestdefined = True
        folder.crc = calculate_crc32(b"zz")
        subinfo = archive.header.main_streams.substreamsinfo
        # Only block 0 has a whole-block checksum; block 1 keeps member CRCs.
        subinfo.digestsdefined[0] = False
        subinfo.digestsdefined[1] = False
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["b", "d", "a", "c"], quota=1)
        execution = plan.execute(archive)
    by_name = {delivery.name: delivery for delivery in execution.deliveries}
    assert by_name["b"].reason is not None and not by_name["b"].verified
    assert by_name["a"].reason is not None and not by_name["a"].verified
    assert by_name["d"].verified and by_name["c"].verified


def test_cheapest_plan_prefers_reopen_over_expensive_staging(tmp_path):
    path = tmp_path.joinpath("cost.7z")
    with py7zr.SevenZipFile(path, "w") as archive:
        archive.writestr(b"x" * 10, "x")
        archive.writestr(b"y" * 10, "y")
    with py7zr.SevenZipFile(path, "a") as archive:
        archive.writestr(b"z", "z")
    with py7zr.SevenZipFile(path) as archive:
        planner = ExtractionPlan.from_archive(archive, open_costs={0: 1, 1: 1})
        # Ask for x, then z, then x. With a quota of 10, keeping x (10 bytes)
        # forces z away, so the second x needs a reopen. With a quota of 20,
        # both fit and the plan pays one open per block.
        tight = planner.build(["x", "z", "x"], quota=10)
        roomy = planner.build(["x", "z", "x"], quota=20)
    assert roomy.total_open_cost == 2
    assert tight.total_open_cost >= roomy.total_open_cost
    assert tight.peak_bytes <= 10
    assert roomy.peak_bytes <= 20
    assert all(step.peak_bytes <= 10 for step in tight.steps)
    assert all(step.peak_bytes <= 20 for step in roomy.steps)


def test_execution_peak_matches_plan(fg_archive):
    with py7zr.SevenZipFile(fg_archive) as archive:
        planner = ExtractionPlan.from_archive(archive)
        plan = planner.build(["b", "d", "a", "c"], quota=2)
        execution = plan.execute(archive)
    assert execution.observed_peak == plan.peak_bytes
    assert execution.quota_ok
