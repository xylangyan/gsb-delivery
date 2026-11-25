from solution import Archive, Clip, Correction
from boltons.cacheutils import ThresholdCounter


def brute_counter(seq, width):
    counter = ThresholdCounter(threshold=0.5)
    counter._thresh_count = width
    clears = {}
    for receipt, code in enumerate(seq, 1):
        counter.total += 1
        try:
            counter._count_map[code][0] += 1
        except KeyError:
            counter._count_map[code] = [1, counter._cur_bucket - 1]

        if counter.total % width == 0:
            for key, value in list(counter._count_map.items()):
                if sum(value) <= counter._cur_bucket:
                    clears[key] = clears.get(key, 0) + 1
            counter._count_map = {
                key: value
                for key, value in counter._count_map.items()
                if sum(value) > counter._cur_bucket
            }
            counter._cur_bucket += 1
    return counter, clears


def test_giant_shift_archive_example():
    archive = Archive(
        [Clip("aaab", 1_000_000_001), Clip("abcc", 5_000_000_006)],
        width=3,
    )
    revised = archive.publish([Correction(receipt=1, value="b")])
    receipt = 24_000_000_012
    end = 24_000_000_024

    assert archive.original.leaderboard(receipt) == [
        ("c", 10_000_000_004),
        ("a", 8_000_000_005),
    ]
    assert revised.leaderboard(receipt) == [("c", 10_000_000_004)]

    old_clears = archive.original.query_clears("a", receipt, end)
    assert old_clears.total == 1
    assert old_clears.nth(1).receipt == 24_000_000_024
    location = old_clears.nth(1).location
    assert (location.clip_number, location.round, location.position) == (
        2,
        5_000_000_005,
        4,
    )

    new_clears = revised.query_clears("a", receipt, end)
    assert new_clears.total == 4
    assert [new_clears.nth(index).receipt for index in range(1, 5)] == [
        24_000_000_012,
        24_000_000_015,
        24_000_000_018,
        24_000_000_021,
    ]
    third = new_clears.nth(3)
    assert third.receipt == 24_000_000_018
    assert (third.location.clip_number, third.location.round, third.location.position) == (
        2,
        5_000_000_004,
        2,
    )


def test_compressed_queries_match_brute_force():
    clips = [Clip("aabc", 5), Clip("bbcd", 7)]
    version = Archive(clips, width=3).publish(
        [Correction(clip=0, round=2, position=1, value="d")]
    )
    seq = [version.symbol_at(index) for index in range(version.total)]
    brute, clears = brute_counter(seq, 3)

    assert version.counter_after(version.total).items() == brute.items()
    assert version.leaderboard(version.total) == brute.most_common()
    for code, total in clears.items():
        assert version.clear_count(code) == total
