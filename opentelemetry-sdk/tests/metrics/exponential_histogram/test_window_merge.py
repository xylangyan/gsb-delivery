# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# pylint: disable=protected-access

from random import Random
from unittest.mock import Mock

from opentelemetry.context import Context
from opentelemetry.sdk.metrics._internal.aggregation import (
    AggregationTemporality,
    _ExponentialBucketHistogramAggregation,
)
from opentelemetry.sdk.metrics._internal.exponential_histogram.window_merge import (
    AmbiguousWindow,
    DeterminateWindow,
    ExponentialHistogramWindowArchive,
    MinutePoint,
    _new_mapping,
)
from opentelemetry.sdk.metrics._internal.measurement import Measurement
from opentelemetry.sdk.metrics._internal.view import _default_reservoir_factory
from opentelemetry.test import TestCase


def aggregate_point(values, max_size, max_scale, record_min_max=True):
    aggregation = _ExponentialBucketHistogramAggregation(
        Mock(),
        _default_reservoir_factory(_ExponentialBucketHistogramAggregation),
        AggregationTemporality.DELTA,
        0,
        max_size=max_size,
        max_scale=max_scale,
        record_min_max=record_min_max,
    )

    for value in values:
        aggregation.aggregate(Measurement(value, 0, Mock(), Context()))

    return aggregation.collect(AggregationTemporality.DELTA, 1)


def ingest_values(archive, minute, values, max_size=16384, max_scale=None, record_min_max=True):
    # Collectors archive with their own max_size/max_scale; by default
    # they use a max_size large enough to never downscale below their
    # max_scale and the dashboard's max_scale.
    if max_scale is None:
        max_scale = archive._max_scale

    point = aggregate_point(
        values,
        max_size=max_size,
        max_scale=max_scale,
        record_min_max=record_min_max,
    )
    archive.ingest(minute, MinutePoint.from_data_point(point))


def trim_counts(counts):
    # The SDK exports the whole circular backing array of its buckets, so
    # the exported counts may carry trailing zeros up to the capacity.
    # The capacity depends on the order the measurements arrived in, which
    # the archive does not know; the logical buckets end at the last
    # non-zero count.
    counts = tuple(counts)

    while len(counts) > 1 and counts[-1] == 0:
        counts = counts[:-1]

    return counts


def result_tuple(result):
    return (
        result.scale,
        result.zero_count,
        result.positive.offset,
        result.positive.counts,
        result.negative.offset,
        result.negative.counts,
    )


def point_tuple(point):
    return (
        point.scale,
        point.zero_count,
        point.positive.offset,
        trim_counts(point.positive.bucket_counts),
        point.negative.offset,
        trim_counts(point.negative.bucket_counts),
    )


class TestWindowMerge(TestCase):
    def assert_matches_replay(self, result, values, max_size, max_scale):
        replay = aggregate_point(values, max_size, max_scale)
        self.assertIsInstance(result, DeterminateWindow)
        self.assertEqual(result_tuple(result), point_tuple(replay))

    def assert_example_consistent(self, example, points_by_minute, max_scale_dashboard):
        for minute_example in example:
            point = points_by_minute[minute_example.minute]
            mapping = _new_mapping(point.scale)
            positive = {}
            negative = {}
            zero_count = 0

            for value in minute_example.values:
                if value == 0:
                    zero_count += 1
                elif value > 0:
                    index = mapping.map_to_index(value)
                    positive[index] = positive.get(index, 0) + 1
                else:
                    index = mapping.map_to_index(-value)
                    negative[index] = negative.get(index, 0) + 1

            expected_positive = {
                point.positive_offset + position: count for position, count in enumerate(point.positive_counts) if count
            }
            expected_negative = {
                point.negative_offset + position: count for position, count in enumerate(point.negative_counts) if count
            }

            self.assertEqual(positive, expected_positive)
            self.assertEqual(negative, expected_negative)
            self.assertEqual(zero_count, point.zero_count)

            if point.min is not None:
                self.assertEqual(min(minute_example.values), point.min)

            if point.max is not None:
                self.assertEqual(max(minute_example.values), point.max)

    def assert_examples_separated(self, result, max_size, max_scale):
        # Both example sets must reproduce every archived summary of the
        # window but must be separated into different results by the
        # dashboard configuration.
        replayed = []

        for example in (result.example_a, result.example_b):
            values = [value for minute_example in example for value in minute_example.values]
            replayed.append(point_tuple(aggregate_point(values, max_size, max_scale)))

        self.assertNotEqual(replayed[0], replayed[1])

    def test_determinate_merge_matches_full_replay(self):
        rng = Random(42)

        for _ in range(30):
            max_scale = rng.randint(0, 5)
            max_size = rng.choice([2, 4, 8, 16, 160])
            minute_count = rng.randint(1, 4)
            archive = ExponentialHistogramWindowArchive(max_size=max_size, max_scale=max_scale)
            all_values = []

            for minute in range(minute_count):
                values = [rng.choice([-1, 1]) * 2 ** rng.uniform(-3, 3) for _ in range(rng.randint(1, 20))]
                values += [0.0] * rng.randint(0, 3)
                all_values.extend(values)
                ingest_values(archive, minute, values)

            report = archive.window(0, minute_count)
            self.assert_matches_replay(report.result, all_values, max_size, max_scale)

    def test_extreme_minute_sliding_out_recomputes_scale(self):
        max_size = 4
        max_scale = 4
        archive = ExponentialHistogramWindowArchive(max_size=max_size, max_scale=max_scale)
        minutes_values = {
            0: [1.0, 2.0, 3.0],
            1: [1.0e6],
            2: [1.5, 2.5],
            3: [2.0, 3.0],
            4: [1.2, 2.2],
        }

        for minute, values in minutes_values.items():
            ingest_values(archive, minute, values)

        # The window that contains the extreme minute is merged into
        # coarse buckets.
        first = archive.window(0, 3)
        self.assert_matches_replay(
            first.result,
            [1.0, 2.0, 3.0, 1.0e6, 1.5, 2.5],
            max_size,
            max_scale,
        )

        # Once the extreme minute has slid out, the window is merged
        # again at the finer scale the remaining minutes allow.
        second = archive.window(2, 3)
        self.assert_matches_replay(second.result, [1.5, 2.5, 2.0, 3.0, 1.2, 2.2], max_size, max_scale)
        self.assertGreater(second.result.scale, first.result.scale)

    def test_coarse_minute_makes_buckets_ambiguous(self):
        max_size = 160
        max_scale = 5
        archive = ExponentialHistogramWindowArchive(max_size=max_size, max_scale=max_scale)

        # Minute 0 was archived by a collector configured with a lower
        # max_scale, minute 1 with the dashboard's max_scale.
        ingest_values(archive, 0, [1.2, 1.5, 1.7, 9.0], max_scale=2)
        ingest_values(archive, 1, [1.3, 8.0], max_scale=5)

        report = archive.window(0, 2)
        result = report.result
        self.assertIsInstance(result, AmbiguousWindow)

        # The scale is still determined because the window distribution
        # fits in max_size at max_scale.
        self.assertEqual(result.scale, 5)
        self.assertIsNone(result.scale_range)

        # Still-confirmable counts.
        self.assertEqual(result.zero_count, 0)
        self.assertEqual(result.count, 6)
        self.assertEqual(result.positive_count, 6)
        self.assertEqual(result.negative_count, 0)

        # The fine minute's contribution is confirmed at the window scale.
        fine_point = aggregate_point([1.3, 8.0], max_size, max_scale)
        self.assertEqual(result.definite_positive.offset, fine_point.positive.offset)
        self.assertEqual(
            result.definite_positive.counts,
            trim_counts(fine_point.positive.bucket_counts),
        )

        # The coarse minute is reported unresolved at its archived scale.
        self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(result.unresolved[0].minute, 0)
        self.assertEqual(result.unresolved[0].scale, 2)

        points_by_minute = {
            0: archive._minutes[0],
            1: archive._minutes[1],
        }
        self.assert_example_consistent(result.example_a, points_by_minute, max_scale)
        self.assert_example_consistent(result.example_b, points_by_minute, max_scale)
        self.assert_examples_separated(result, max_size, max_scale)

    def test_ambiguous_scale_without_min_max(self):
        # A single coarse minute without recorded min/max leaves even the
        # window scale undetermined.
        archive = ExponentialHistogramWindowArchive(max_size=3, max_scale=2)
        archive.ingest(
            0,
            MinutePoint(
                scale=0,
                positive_offset=0,
                positive_counts=(3, 2),
                negative_offset=0,
                negative_counts=(0,),
                zero_count=0,
            ),
        )

        result = archive.window(0, 1).result
        self.assertIsInstance(result, AmbiguousWindow)
        self.assertIsNone(result.scale)
        self.assertEqual(result.scale_range, (0, 2))
        self.assertIsNone(result.definite_positive)
        self.assertEqual(result.count, 5)

        points_by_minute = {0: archive._minutes[0]}
        self.assert_example_consistent(result.example_a, points_by_minute, 2)
        self.assert_example_consistent(result.example_b, points_by_minute, 2)
        self.assert_examples_separated(result, 3, 2)

    def test_recorded_min_max_pin_the_scale(self):
        # The same coarse minute with recorded min/max pins the window
        # scale exactly, only the bucket distribution stays ambiguous.
        archive = ExponentialHistogramWindowArchive(max_size=3, max_scale=2)
        archive.ingest(
            0,
            MinutePoint(
                scale=0,
                positive_offset=0,
                positive_counts=(3, 2),
                negative_offset=0,
                negative_counts=(0,),
                zero_count=0,
                min=1.5,
                max=3.5,
            ),
        )

        result = archive.window(0, 1).result
        self.assertIsInstance(result, AmbiguousWindow)
        self.assertEqual(result.scale, 1)
        self.assertIsNone(result.scale_range)

        points_by_minute = {0: archive._minutes[0]}
        self.assert_example_consistent(result.example_a, points_by_minute, 2)
        self.assert_example_consistent(result.example_b, points_by_minute, 2)
        self.assert_examples_separated(result, 3, 2)

    def test_all_zero_window(self):
        archive = ExponentialHistogramWindowArchive(max_size=4, max_scale=4)
        ingest_values(archive, 0, [0.0, 0.0])
        ingest_values(archive, 1, [0.0])

        result = archive.window(0, 2).result
        self.assert_matches_replay(result, [0.0, 0.0, 0.0], 4, 4)
        self.assertEqual(result.scale, 0)
        self.assertEqual(result.zero_count, 3)

    def test_empty_window(self):
        archive = ExponentialHistogramWindowArchive(max_size=4, max_scale=4)
        self.assertIsNone(archive.window(0, 2).result)

    def test_negative_only_window(self):
        archive = ExponentialHistogramWindowArchive(max_size=4, max_scale=3)
        ingest_values(archive, 0, [-1.0, -2.0, -8.0])
        ingest_values(archive, 1, [-1.5])

        result = archive.window(0, 2).result
        self.assert_matches_replay(result, [-1.0, -2.0, -8.0, -1.5], 4, 3)
        self.assertEqual(result.positive.counts, (0,))

    def test_archive_finer_than_dashboard(self):
        # A collector may archive with a max_scale higher than the
        # dashboard's; downscaling its buckets is exact.
        archive = ExponentialHistogramWindowArchive(max_size=8, max_scale=2)
        ingest_values(archive, 0, [1.0, 1.5, 2.0, 100.0], max_scale=6)
        ingest_values(archive, 1, [1.2], max_scale=6)

        result = archive.window(0, 2).result
        self.assert_matches_replay(result, [1.0, 1.5, 2.0, 100.0, 1.2], 8, 2)

    def test_correction_keeps_emitted_reports(self):
        archive = ExponentialHistogramWindowArchive(max_size=4, max_scale=4)
        ingest_values(archive, 0, [1.0, 2.0])
        ingest_values(archive, 1, [2.0, 4.0])
        ingest_values(archive, 2, [3.0])

        report = archive.window(0, 2)
        self.assert_matches_replay(report.result, [1.0, 2.0, 2.0, 4.0], 4, 4)
        version_before = archive.version

        # A correction to a minute the window has not slid to does not
        # change the emitted report nor the window's recomputation.
        archive.correct(2, MinutePoint.from_data_point(aggregate_point([9.0], 16384, 4)))
        self.assertGreater(archive.version, version_before)
        self.assertIs(archive.get_report(report.report_id), report)

        recomputed = archive.window(0, 2)
        self.assertEqual(result_tuple(recomputed.result), result_tuple(report.result))
        self.assertEqual(
            result_tuple(archive.window_as_of(0, 2, report.version)),
            result_tuple(report.result),
        )

        # A correction inside the window changes new queries but the old
        # report keeps its original data.
        archive.correct(1, MinutePoint.from_data_point(aggregate_point([7.0], 16384, 4)))
        updated = archive.window(0, 2)
        self.assert_matches_replay(updated.result, [1.0, 2.0, 7.0], 4, 4)
        self.assertIs(archive.get_report(report.report_id), report)
        self.assert_matches_replay(report.result, [1.0, 2.0, 2.0, 4.0], 4, 4)
        self.assertEqual(
            result_tuple(archive.window_as_of(0, 2, report.version)),
            result_tuple(report.result),
        )

    def test_ingest_rejects_duplicate_minute(self):
        archive = ExponentialHistogramWindowArchive()
        ingest_values(archive, 0, [1.0])

        with self.assertRaises(ValueError):
            archive.ingest(0, archive._minutes[0])

    def test_invalid_max_size(self):
        with self.assertRaises(ValueError):
            ExponentialHistogramWindowArchive(max_size=1)

        with self.assertRaises(ValueError):
            ExponentialHistogramWindowArchive(max_size=16385)

    def test_partial_min_max_recording_still_pins(self):
        # One minute did not record min/max, but its archived buckets
        # stay below the other minute's recorded max, so the window
        # extreme is still pinned and the result determinate.
        archive = ExponentialHistogramWindowArchive(max_size=2, max_scale=-1)
        ingest_values(archive, 0, [1.0, 4.0, -1.0, -4.0], record_min_max=False)
        ingest_values(archive, 1, [1.0, 2.5, -1.0, -3.7])
        ingest_values(archive, 2, [0.02, 190.0, -0.02, -150.0], max_scale=-3)

        all_values = [
            1.0,
            4.0,
            -1.0,
            -4.0,
            1.0,
            2.5,
            -1.0,
            -3.7,
            0.02,
            190.0,
            -0.02,
            -150.0,
        ]
        result = archive.window(0, 3).result
        self.assert_matches_replay(result, all_values, 2, -1)

    def test_window_with_missing_minutes(self):
        # Minutes that never reported are treated as minutes without
        # measurements.
        archive = ExponentialHistogramWindowArchive(max_size=4, max_scale=4)
        ingest_values(archive, 0, [1.0, 2.0])
        ingest_values(archive, 2, [4.0])

        result = archive.window(0, 4).result
        self.assert_matches_replay(result, [1.0, 2.0, 4.0], 4, 4)

    def test_correction_can_change_determinacy(self):
        archive = ExponentialHistogramWindowArchive(max_size=160, max_scale=5)
        ingest_values(archive, 0, [1.2, 1.5], max_scale=5)
        ingest_values(archive, 1, [1.3, 8.0], max_scale=5)

        first = archive.window(0, 2)
        self.assertIsInstance(first.result, DeterminateWindow)

        # Correcting minute 1 to a summary archived with a lower
        # max_scale makes the window buckets ambiguous.
        archive.correct(
            1,
            MinutePoint.from_data_point(aggregate_point([1.3, 8.0], 160, 2)),
        )
        second = archive.window(0, 2)
        self.assertIsInstance(second.result, AmbiguousWindow)
        self.assertIs(archive.get_report(first.report_id), first)
        self.assertIsInstance(archive.get_report(first.report_id).result, DeterminateWindow)

    def test_randomized_against_replay(self):
        # Collectors with arbitrary max_size/max_scale configurations;
        # determinate results must match a full replay exactly, ambiguous
        # results must come with separating example measurement sets.
        rng = Random(7)

        for _ in range(40):
            max_scale = rng.randint(0, 4)
            max_size = rng.choice([4, 8, 32, 160])
            archive = ExponentialHistogramWindowArchive(max_size=max_size, max_scale=max_scale)
            minute_count = rng.randint(1, 4)
            all_values = []
            points_by_minute = {}

            for minute in range(minute_count):
                values = [rng.choice([-1, 1]) * 2 ** rng.uniform(-2, 4) for _ in range(rng.randint(1, 15))]
                values += [0.0] * rng.randint(0, 2)
                all_values.extend(values)
                collector_max_scale = max_scale + rng.randint(0, 2)
                collector_max_size = rng.choice([max_size, 160, 16384])
                record_min_max = rng.random() > 0.3
                ingest_values(
                    archive,
                    minute,
                    values,
                    max_size=collector_max_size,
                    max_scale=collector_max_scale,
                    record_min_max=record_min_max,
                )
                points_by_minute[minute] = archive._minutes[minute]

            result = archive.window(0, minute_count).result

            if isinstance(result, DeterminateWindow):
                self.assert_matches_replay(result, all_values, max_size, max_scale)
            else:
                self.assertIsInstance(result, AmbiguousWindow)
                self.assertEqual(
                    result.zero_count,
                    sum(point.zero_count for point in points_by_minute.values()),
                )
                self.assertEqual(
                    result.count,
                    sum(point.count for point in points_by_minute.values()),
                )
                self.assert_example_consistent(result.example_a, points_by_minute, max_scale)
                self.assert_example_consistent(result.example_b, points_by_minute, max_scale)
                self.assert_examples_separated(result, max_size, max_scale)
