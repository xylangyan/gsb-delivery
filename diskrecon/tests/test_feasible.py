"""Feasible-set arithmetic: APs, CRT intersection, Minkowski sums."""


from diskrecon.feasible import AP
from diskrecon.feasible import FeasibleSet
from diskrecon.feasible import pick_sum


class TestAP:
    def test_last_and_contains(self):
        ap = AP(11, 256, 3)  # 11, 267, 523
        assert ap.last == 523
        assert ap.contains(267)
        assert not ap.contains(268)
        assert not ap.contains(524)

    def test_intersect_same_step(self):
        a = AP(0, 4, 10)  # 0..36 step 4
        b = AP(8, 4, 5)  # 8..24 step 4
        hit = a.intersect(b)
        assert hit == AP(8, 4, 5)

    def test_intersect_crt(self):
        a = AP(1, 4, 20)  # 1, 5, 9, ...
        b = AP(2, 6, 20)  # 2, 8, 14, ...
        hit = a.intersect(b)
        # solutions of x=1 mod 4, x=2 mod 6 -> x=... no: gcd=2, 1%2 != 2%2? 1%2=1, 2%2=0
        assert hit is None

    def test_intersect_crt_solvable(self):
        a = AP(2, 4, 20)  # 2, 6, 10, 14, ...
        b = AP(2, 6, 20)  # 2, 8, 14, ...
        hit = a.intersect(b)
        assert hit is not None
        assert hit.step == 12
        assert hit.start == 2
        assert hit.contains(14)

    def test_intersect_disjoint_ranges(self):
        assert AP(0, 2, 3).intersect(AP(100, 2, 3)) is None


class TestFeasibleSet:
    def test_wrapped_deltas(self):
        fs = FeasibleSet.wrapped_deltas(11, 256, 12)
        assert fs.singleton == 11

    def test_wrapped_deltas_multi_turnover(self):
        # An unmeasured window may legally pass several periods.
        fs = FeasibleSet.wrapped_deltas(11, 256, 600)
        assert fs.min == 11
        assert fs.max == 523
        assert fs.contains(267)
        assert not fs.contains(523 + 256)

    def test_wrapped_deltas_cap_below_base(self):
        assert FeasibleSet.wrapped_deltas(11, 256, 10).is_empty

    def test_interval(self):
        fs = FeasibleSet.interval(5, 65)
        assert fs.min == 5 and fs.max == 65
        assert fs.contains(42)
        assert not fs.contains(66)

    def test_union_merges_touching_progressions(self):
        a = FeasibleSet.progression(0, 256, 2)  # 0, 256
        b = FeasibleSet.progression(512, 256, 1)
        u = a.union(b)
        assert u.pieces == (AP(0, 256, 3),)

    def test_intersect(self):
        a = FeasibleSet.wrapped_deltas(11, 256, 600)
        b = FeasibleSet.interval(200, 300)
        hit = a.intersect(b)
        assert hit.singleton == 267

    def test_minkowski_equal_steps(self):
        a = FeasibleSet.wrapped_deltas(11, 256, 300)  # {11, 267}
        s = a.minkowski(a)
        assert s.pieces == (AP(22, 256, 3),)  # 22, 278, 534
        assert s.contains(278)

    def test_minkowski_interval_plus_progression(self):
        a = FeasibleSet.interval(0, 60)
        b = FeasibleSet.wrapped_deltas(5, 256, 30)  # {5}
        s = a.minkowski(b)
        assert s.pieces == (AP(5, 1, 61),)  # [5, 65]

    def test_minkowski_sparse(self):
        a = FeasibleSet.progression(0, 3, 2)  # {0, 3}
        b = FeasibleSet.progression(0, 5, 2)  # {0, 5}
        s = a.minkowski(b)  # {0, 3, 5, 8}
        for v in (0, 3, 5, 8):
            assert s.contains(v)
        assert not s.contains(4)

    def test_singleton(self):
        assert FeasibleSet.interval(4, 4).singleton == 4
        assert FeasibleSet.interval(4, 5).singleton is None


class TestPickSum:
    def test_exact_witness(self):
        a = FeasibleSet.wrapped_deltas(11, 256, 300)
        values = pick_sum([a, a], 278)
        assert values is not None
        assert sum(values) == 278
        assert sorted(values) == [11, 267]

    def test_impossible(self):
        a = FeasibleSet.wrapped_deltas(11, 256, 300)
        assert pick_sum([a, a], 100) is None

    def test_with_interval(self):
        a = FeasibleSet.interval(0, 60)
        b = FeasibleSet.wrapped_deltas(5, 256, 30)
        values = pick_sum([a, b], 42)
        assert values == [37, 5]
