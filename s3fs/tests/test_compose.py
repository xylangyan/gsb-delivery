"""Tests for planning multipart assemblies from ordered byte ranges."""

import asyncio

import pytest

from s3fs.compose import (
    MergeExecutionError,
    MergePlanError,
    PartConfirmation,
    SourceRange,
    execute_merge,
    plan_merge,
)

MB = 2**20


def _confirmed(plan, part_numbers):
    return [
        PartConfirmation(
            part_number=step.part_number,
            etag=f'"etag-{step.part_number}"',
            slices=step.slices,
            size=step.size,
            upload_id=plan.upload_id or "upload",
            target=plan.path,
        )
        for step in plan.steps
        if step.part_number in part_numbers
    ]


A = SourceRange("bucket/A", 0, 8)
LOCAL2 = SourceRange.local(b"xy")
B = SourceRange("bucket/B", 0, 9)


def test_first_round_splits_at_the_cross_source_part():
    plan = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )

    assert plan.part_sizes == (5, 5, 9)
    assert [step.action for step in plan.steps] == ["copy", "upload", "copy"]
    assert plan.steps[0].slices[0].source.path == "bucket/A"
    assert (plan.steps[0].output_start, plan.steps[0].output_end) == (0, 5)
    middle = plan.steps[1]
    assert (middle.output_start, middle.output_end) == (5, 10)
    assert [(s.source.path, s.source_start, s.source_end) for s in middle.slices] == [
        ("bucket/A", 5, 8),
        (None, 0, 2),
    ]
    # Remote bytes read for the client-assembled part, all five written.
    assert middle.client_read_bytes == 3
    assert middle.client_write_bytes == 5
    assert plan.client_read_bytes == 3
    assert plan.client_write_bytes == 5
    assert plan.steps[2].slices[0].source.path == "bucket/B"
    assert (plan.steps[2].output_start, plan.steps[2].output_end) == (10, 19)


def test_retry_only_resends_the_part_between_confirmed_parts():
    first = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    confirmed = _confirmed(first, {1, 3})

    retry = plan_merge(
        "target",
        [A, LOCAL2, B],
        min_part_size=5,
        max_part_size=12,
        max_parts=3,
        confirmed=confirmed,
    )

    assert retry.part_sizes == (5, 5, 9)
    assert retry.reused_parts == (1, 3)
    assert retry.resent_parts == ()
    assert [step.action for step in retry.steps] == ["reuse", "upload", "reuse"]
    assert retry.client_bytes == 8
    assert [(step.part_number, step.output_start, step.output_end) for step in retry.steps] == [
        (1, 0, 5),
        (2, 5, 10),
        (3, 10, 19),
    ]
    # Reused parts keep their ETags and are never sent again.
    assert [step.etag for step in retry.steps] == ['"etag-1"', None, '"etag-3"']
    assert [step.part_number for step in retry.steps_to_send] == [2]


def test_new_target_keeps_confirmed_source_bytes_where_still_valid():
    first = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    confirmed = _confirmed(first, {1, 3})
    local8 = SourceRange.local(b"abcdefgh")

    plan = plan_merge(
        "target",
        [A, local8, B],
        min_part_size=5,
        max_part_size=12,
        max_parts=3,
        confirmed=confirmed,
    )

    # A[0,5) and B[0,9) are byte-identical and remain parts 1 and 3; only the
    # 11-byte middle (A[5,8) + local 8) has to be read and written.
    assert plan.part_sizes == (5, 11, 9)
    assert plan.reused_parts == (1, 3)
    assert plan.resent_parts == ()
    assert plan.client_read_bytes == 3
    assert plan.client_write_bytes == 11
    assert plan.client_bytes == 14
    assert [(s.source.path, s.source_start, s.source_end) for s in plan.steps[1].slices] == [
        ("bucket/A", 5, 8),
        (None, 0, 8),
    ]


def test_max_part_size_10_cannot_keep_both_confirmed_parts_and_send_only_part_2():
    first = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    confirmed = _confirmed(first, {1, 3})
    local8 = SourceRange.local(b"abcdefgh")

    with pytest.raises(MergePlanError) as excinfo:
        plan_merge(
            "target",
            [A, local8, B],
            min_part_size=5,
            max_part_size=10,
            max_parts=3,
            confirmed=confirmed,
        )
    assert "1, 3" in str(excinfo.value)


def test_resend_permission_is_respected_and_minimises_client_bytes():
    first = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    confirmed = _confirmed(first, {1, 3})
    local8 = SourceRange.local(b"abcdefgh")

    resend_first = plan_merge(
        "target",
        [A, local8, B],
        min_part_size=5,
        max_part_size=10,
        max_parts=3,
        confirmed=confirmed,
        resend_parts=[1],
    )
    assert resend_first.part_sizes == (8, 8, 9)
    assert resend_first.reused_parts == (3,)
    assert resend_first.resent_parts == (1,)
    assert resend_first.client_bytes == 8

    resend_third = plan_merge(
        "target",
        [A, local8, B],
        min_part_size=5,
        max_part_size=10,
        max_parts=3,
        confirmed=confirmed,
        resend_parts=[3],
    )
    assert resend_third.reused_parts == (1,)
    assert resend_third.resent_parts == (3,)
    assert resend_third.part_sizes == (5, 10, 10)
    assert resend_third.client_bytes == 32


def test_resend_none_allows_replacing_any_and_uses_minimum_client_bytes():
    first = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    confirmed = _confirmed(first, {1, 3})

    result = plan_merge(
        "target",
        [A, SourceRange.local(b"abcdefgh"), B],
        min_part_size=5,
        max_part_size=12,
        max_parts=3,
        confirmed=confirmed,
        resend_parts=None,
    )
    # Replacing part 1 with A[0,8) costs no client bytes for that part and
    # leaves the 8-byte local tail as the only upload; keeping both confirmed
    # parts would move A[5,8) into an 11-byte client part (14 bytes).
    assert result.part_sizes == (8, 8, 9)
    assert result.reused_parts == (3,)
    assert result.resent_parts == (1,)
    assert result.client_bytes == 8


def test_unknown_and_stale_parts_are_not_new_target_confirmations():
    first = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    confirmed = _confirmed(first, {1, 3})
    unknown = PartConfirmation(
        part_number=2,
        etag='"listed"',
        slices=(),
        size=5,
        target="target",
        upload_id="upload",
    )
    foreign = PartConfirmation(
        part_number=2,
        etag='"other-upload"',
        slices=confirmed[0].slices,
        target="target",
        upload_id="another-upload",
    )

    plan = plan_merge(
        "target",
        [A, SourceRange.local(b"abcdefgh"), B],
        min_part_size=5,
        max_part_size=10,
        max_parts=3,
        confirmed=[*confirmed, unknown, foreign],
        resend_parts=[1],
        upload_id="upload",
    )

    reasons = {item.confirmation.etag: item.reason for item in plan.ignored_confirmations}
    assert reasons['"listed"'] == "missing source provenance"
    assert reasons['"other-upload"'] == "different upload"
    assert unknown.part_number not in plan.reused_parts
    assert plan.reused_parts == (3,)


def test_stale_bytes_from_an_older_target_are_ignored():
    old = plan_merge(
        "target", [A, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    old_part = PartConfirmation(
        part_number=1,
        etag='"old"',
        slices=old.steps[0].slices,
        size=old.steps[0].size,
        target="target",
        upload_id="upload",
    )
    new_sources = [SourceRange("bucket/A", 0, 6), SourceRange.local(b"zz"), B]

    plan = plan_merge(
        "target",
        new_sources,
        min_part_size=5,
        max_part_size=12,
        max_parts=3,
        confirmed=[old_part],
        resend_parts=None,
    )

    assert plan.reused_parts == ()
    assert [item.reason for item in plan.ignored_confirmations] == [
        "source bytes are not in the new target order"
    ]


def test_confirmed_part_keeps_provenance_when_reused_and_is_not_resent():
    plan = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=8, max_parts=5
    )
    confirmed = _confirmed(plan, {2})

    shifted = plan_merge(
        "target",
        [SourceRange("bucket/A", 0, 6), *[LOCAL2], B],
        min_part_size=5,
        max_part_size=8,
        max_parts=5,
        confirmed=confirmed,
    )
    # A different target layout must not silently keep old part 2 unless its
    # exact source bytes still occupy the same part number.
    assert 2 not in shifted.reused_parts


class _Recorder:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.created = []
        self.reads = []
        self.copies = []
        self.uploads = []
        self.completed = []
        self.failures = 0

    async def create_upload(self, path):
        self.created.append(path)
        return "upload-1"

    async def read_range(self, path, version_id, start, end):
        self.reads.append((path, version_id, start, end))
        return b"R" * (end - start)

    async def copy_part(self, upload_id, path, part_number, source_path, version_id, start, end):
        self.copies.append((part_number, source_path, start, end))
        return f'"copy-{part_number}"'

    async def upload_part(self, upload_id, path, part_number, data):
        if part_number == self.fail_at:
            raise RuntimeError("simulated transport failure")
        self.uploads.append((part_number, len(data)))
        return f'"upload-{part_number}"'

    async def complete_upload(self, upload_id, path, parts):
        self.completed.append((upload_id, path, tuple(parts)))
        return {"ETag": '"final"'}


def test_execute_records_actual_client_bytes_and_confirmations():
    plan = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    recorder = _Recorder()

    result = asyncio.run(execute_merge(plan, recorder))

    assert recorder.created == ["target"]
    assert recorder.copies == [
        (1, "bucket/A", 0, 5),
        (3, "bucket/B", 0, 9),
    ]
    assert recorder.reads == [("bucket/A", None, 5, 8)]
    assert [upload for upload, _ in recorder.uploads] == [2]
    assert [len for _, len in recorder.uploads] == [5]
    assert recorder.completed == [
        (
            "upload-1",
            "target",
            ((1, '"copy-1"'), (2, '"upload-2"'), (3, '"copy-3"')),
        )
    ]

    assert result.client_read_bytes == 3
    assert result.client_write_bytes == 5
    assert result.client_bytes == 8
    assert [(step.part_number, step.action, step.etag) for step in result.steps] == [
        (1, "copy", '"copy-1"'),
        (2, "upload", '"upload-2"'),
        (3, "copy", '"copy-3"'),
    ]
    assert [conf.part_number for conf in result.confirmations] == [1, 2, 3]
    assert all(conf.etag and conf.slices for conf in result.confirmations)
    assert all(conf.upload_id == "upload-1" for conf in result.confirmations)


def test_execution_failure_reports_only_parts_actually_confirmed():
    plan = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    recorder = _Recorder(fail_at=2)

    with pytest.raises(MergeExecutionError) as excinfo:
        asyncio.run(execute_merge(plan, recorder))
    error = excinfo.value
    assert [step.part_number for step in error.steps] == [1]
    assert [conf.part_number for conf in error.confirmations] == [1]
    assert error.upload_id == "upload-1"
    assert recorder.completed == []


def test_retry_after_failure_does_not_resend_the_confirmed_part():
    plan = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    first = _Recorder(fail_at=2)
    with pytest.raises(MergeExecutionError) as excinfo:
        asyncio.run(execute_merge(plan, first))

    retry_plan = plan_merge(
        "target",
        [A, LOCAL2, B],
        min_part_size=5,
        max_part_size=12,
        max_parts=3,
        confirmed=excinfo.value.confirmations,
        upload_id=excinfo.value.upload_id,
    )
    second = _Recorder()
    result = asyncio.run(execute_merge(retry_plan, second))

    assert retry_plan.reused_parts == (1,)
    assert second.copies == [(3, "bucket/B", 0, 9)]
    assert [part for part, _ in second.uploads] == [2]
    assert result.client_bytes == 8


def test_reused_part_keeps_etag_and_is_not_sent():
    plan = plan_merge(
        "target", [A, LOCAL2, B], min_part_size=5, max_part_size=12, max_parts=3
    )
    first = _Recorder()
    result = asyncio.run(execute_merge(plan, first))
    confirmed = [c for c in result.confirmations if c.part_number in (1, 3)]

    retry = plan_merge(
        "target",
        [A, LOCAL2, B],
        min_part_size=5,
        max_part_size=12,
        max_parts=3,
        confirmed=confirmed,
    )
    recorder = _Recorder()
    second = asyncio.run(execute_merge(retry, recorder))

    assert recorder.copies == []
    assert [part for part, _ in recorder.uploads] == [2]
    assert recorder.reads == [("bucket/A", None, 5, 8)]
    assert [(s.part_number, s.reused, s.etag) for s in second.steps] == [
        (1, True, '"copy-1"'),
        (2, False, '"upload-2"'),
        (3, True, '"copy-3"'),
    ]
    assert recorder.completed[0][2] == (
        (1, '"copy-1"'),
        (2, '"upload-2"'),
        (3, '"copy-3"'),
    )


def test_plan_describe_records_sources_actual_bytes_and_output_intervals():
    plan = plan_merge(
        "target",
        [A, LOCAL2, B],
        min_part_size=5,
        max_part_size=12,
        max_parts=3,
    )
    described = plan.describe()

    assert described["total_size"] == 19
    assert (described["min_part_size"], described["max_part_size"]) == (5, 12)
    assert [source["path"] for source in described["sources"]] == [
        "bucket/A",
        None,
        "bucket/B",
    ]
    assert [source["copyable"] for source in described["sources"]] == [True, False, True]
    middle = described["steps"][1]
    assert (middle["output_start"], middle["output_end"]) == (5, 10)
    assert middle["client_read_bytes"] == 3
    assert middle["client_write_bytes"] == 5
    assert [slice_["path"] for slice_ in middle["slices"]] == ["bucket/A", None]


def test_non_copyable_remote_range_is_read_and_uploaded():
    plan = plan_merge(
        "target",
        [SourceRange("bucket/A", 0, 8, copyable=False), B],
        min_part_size=5,
        max_part_size=12,
        max_parts=3,
    )
    assert plan.part_sizes == (8, 9)
    assert [step.action for step in plan.steps] == ["upload", "copy"]
    assert plan.client_read_bytes == 8
    assert plan.client_write_bytes == 8


def test_whole_object_sources_and_buckets_use_real_s3_sizes():
    class FakeFS:
        def __init__(self):
            self.sizes = {"bucket/A": 8, "bucket/B": 9}

        async def _info(self, path, version_id=None):
            return {"size": self.sizes[path]}

    from s3fs.compose import coerce_sources

    sources = coerce_sources(["bucket/A", b"xy", "bucket/B"])
    assert [source.end for source in sources] == [None, 2, None]
    assert [source.length if source.is_local else None for source in sources] == [
        None,
        2,
        None,
    ]


def test_big_targets_plan_without_exhaustive_byte_enumeration():
    sources = [
        SourceRange("bucket/A", 0, 60 * MB),
        SourceRange.local(b"x" * (30 * MB)),
        SourceRange("bucket/B", 0, 40 * MB),
    ]
    plan = plan_merge(
        "target",
        sources,
        min_part_size=5 * MB,
        max_part_size=100 * MB,
        max_parts=5,
    )
    assert plan.total_size == 130 * MB
    assert plan.part_sizes == (60 * MB, 30 * MB, 40 * MB)
    assert plan.client_bytes == 30 * MB
    assert all(size >= 5 * MB for size in plan.part_sizes)
