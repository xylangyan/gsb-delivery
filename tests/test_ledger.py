"""Tests for crccheck.ledger: CRC derivation from ledger records.

  License:

    MIT License

    Copyright (c) 2015-2025 by Martin Scharrer <martin.scharrer@web.de>

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

"""
import itertools
import random
import time

from crccheck.crc import Crc
from tests import TestCase
from crccheck.ledger import (
    AssemblyPiece,
    Closure,
    CrcAlgebra,
    LedgerRecord,
    RereadOption,
    Solver,
    _mat_identity,
    _mat_inverse,
    _mat_mul,
    _mat_pow,
    _mat_vec,
    _mat_xor,
    solve_work_order,
)

ARC16 = dict(width=16, poly=0x8005, initvalue=0x0000, reflect_input=True,
             reflect_output=True, xor_output=0x0000)
CCITT16 = dict(width=16, poly=0x1021, initvalue=0xFFFF, reflect_input=False,
               reflect_output=False, xor_output=0x0000)
CRC32 = dict(width=32, poly=0x04C11DB7, initvalue=0xFFFFFFFF,
             reflect_input=True, reflect_output=True, xor_output=0xFFFFFFFF)
CRC8 = dict(width=8, poly=0x07, initvalue=0x00, reflect_input=False,
            reflect_output=False, xor_output=0x00)
CRC5USB = dict(width=5, poly=0x05, initvalue=0x1F, reflect_input=True,
               reflect_output=True, xor_output=0x1F)


def calc(params, data):
    return Crc(**params).calc(data)


class TestMatrix(TestCase):

    def test_identity_and_zero(self):
        for width in (3, 5, 8, 16, 32):
            identity = _mat_identity(width)
            vector = (1 << width) - 1
            self.assertEqual(_mat_vec(identity, vector), vector)
            probe = 0b101101 & ((1 << width) - 1)
            self.assertEqual(_mat_vec(identity, probe), probe)
            self.assertEqual(_mat_xor(identity, identity, width), [0] * width)

    def test_multiplication_matches_per_column_map(self):
        random.seed(101)
        for width in (8, 16):
            left = [random.randrange(1 << width) for _ in range(width)]
            right = [random.randrange(1 << width) for _ in range(width)]
            product = _mat_mul(left, right, width)
            for col in range(width):
                self.assertEqual(product[col], _mat_vec(left, right[col]))

    def test_pow_and_inverse_against_crccheck(self):
        random.seed(102)
        for params in (ARC16, CCITT16, CRC8, CRC5USB, CRC32):
            algebra = CrcAlgebra(params)
            width = params["width"]
            identity = _mat_identity(width)
            for exponent in (0, 1, 2, 7, 13, 100):
                self.assertEqual(_mat_pow(algebra.advance(1), exponent, width),
                                 algebra.advance(exponent))
            inverse = algebra.advance_inverse(13)
            self.assertEqual(_mat_mul(inverse, algebra.advance(13), width), identity)
            for _ in range(8):
                state = random.randrange(1 << width)
                probe = algebra._new_crc(initvalue=0)
                probe.reset(state)
                probe.process(b"\x00" * 17)
                self.assertEqual(_mat_vec(algebra.advance(17), state), probe.value())
                self.assertEqual(_mat_vec(algebra.advance_inverse(17), probe.value()), state)

    def test_inverse_of_singular_matrix_raises(self):
        with self.assertRaises(ValueError):
            _mat_inverse([0, 0, 0, 0], 4)


class TestCrcAlgebra(TestCase):

    def test_concatenation_identity(self):
        random.seed(201)
        for params in (ARC16, CCITT16, CRC32, CRC5USB):
            algebra = CrcAlgebra(params)
            left = bytes(random.randrange(256) for _ in range(11))
            right = bytes(random.randrange(256) for _ in range(7))
            expected = algebra.data_contribution(left + right)
            got = _mat_vec(algebra.advance(len(right)),
                           algebra.data_contribution(left)) ^ algebra.data_contribution(right)
            self.assertEqual(got, expected)

    def test_record_round_trip_with_output_stage(self):
        random.seed(202)
        for params in (ARC16, CRC32, CRC5USB):
            algebra = CrcAlgebra(params)
            data = bytes(random.randrange(256) for _ in range(23))
            final = calc(params, data)
            self.assertEqual(algebra.record_to_contribution(final, len(data)),
                             algebra.data_contribution(data))
            inner = algebra.data_contribution(data) ^ _mat_vec(
                algebra.advance(len(data)), algebra.initvalue)
            self.assertEqual(algebra.inner_to_final(inner), final)

    def test_injection_space_spans_actual_contributions(self):
        random.seed(203)
        for params in (CRC8, ARC16, CCITT16):
            algebra = CrcAlgebra(params)
            for length in (1, 3, 9):
                basis = algebra.injection_space(length)
                seen = set()
                # Every single-bit input contribution must lie in the span.
                for byte_position in range(length):
                    for bit in range(8):
                        probe = algebra._new_crc(initvalue=0)
                        payload = bytearray(length)
                        payload[byte_position] = 1 << bit
                        probe.process(bytes(payload))
                        vector = probe.value()
                        reduced = vector
                        for pivot in basis:
                            pivot_bit = pivot & -pivot
                            if reduced & pivot_bit:
                                reduced ^= pivot
                        self.assertEqual(reduced, 0)
                        seen.add(vector)
                # The span genuinely contains non-zero vectors for real data.
                self.assertTrue(any(seen))


class TestClosure(TestCase):

    def setUp(self):
        random.seed(301)
        self.source = bytes(random.randrange(256) for _ in range(96))
        self.algebra = CrcAlgebra(ARC16)

    def test_overlap_derives_sub_intervals(self):
        records = [
            LedgerRecord("whole", ARC16, 0, 80, calc(ARC16, self.source[0:80])),
            LedgerRecord("tail", ARC16, 48, 80, calc(ARC16, self.source[48:80])),
        ]
        closure = Closure(self.algebra)
        for record in records:
            closure.add(record.start, record.end,
                        self.algebra.record_to_contribution(record.result, record.length),
                        {record.id})
        closure.run()
        self.assertEqual(closure.values[(0, 48)],
                         self.algebra.data_contribution(self.source[0:48]))
        self.assertEqual(closure.values[(48, 80)],
                         self.algebra.data_contribution(self.source[48:80]))
        self.assertEqual(closure.values[(0, 80)],
                         self.algebra.data_contribution(self.source[0:80]))
        self.assertEqual(closure.contradictions, [])

    def test_adjacency_merge(self):
        closure = Closure(self.algebra)
        left = self.source[0:32]
        right = self.source[32:64]
        closure.add(0, 32, self.algebra.data_contribution(left), {"left"})
        closure.add(32, 64, self.algebra.data_contribution(right), {"right"})
        closure.run()
        self.assertEqual(closure.values[(0, 64)],
                         self.algebra.data_contribution(self.source[0:64]))

    def test_contradiction_carries_provenance(self):
        closure = Closure(self.algebra)
        closure.add(0, 16, 0x1234, {"a"})
        closure.add(0, 16, 0x4321, {"b"})
        self.assertEqual(len(closure.contradictions), 1)
        contradiction = closure.contradictions[0]
        self.assertEqual(contradiction["sources_a"], ["a"])
        self.assertEqual(contradiction["sources_b"], ["b"])

    def test_structural_mode_never_contradicts(self):
        closure = Closure(self.algebra, structural=True)
        self.assertTrue(closure.add(0, 8, 1, {"probe"}))
        self.assertTrue(closure.add(0, 8, 2, {"record"}))
        self.assertEqual(closure.contradictions, [])
        self.assertIn((0, 8), closure.values)


class TestSolverEndToEnd(TestCase):

    def setUp(self):
        random.seed(401)
        self.source = bytes(random.randrange(256) for _ in range(160))

    def _records(self):
        return [
            LedgerRecord("arc-whole", ARC16, 0, 96, calc(ARC16, self.source[0:96])),
            LedgerRecord("arc-tail", ARC16, 48, 96, calc(ARC16, self.source[48:96])),
            LedgerRecord("arc-mid", ARC16, 32, 64, calc(ARC16, self.source[32:64])),
            LedgerRecord("arc-end", ARC16, 64, 96, calc(ARC16, self.source[64:96])),
            LedgerRecord("ccitt", CCITT16, 20, 120, calc(CCITT16, self.source[20:120])),
            LedgerRecord("crc8", CRC8, 0, 40, calc(CRC8, self.source[0:40])),
        ]

    def _assembly(self):
        fill = bytes([0xAA, 0xBB, 0xCC])
        return [
            AssemblyPiece("source", 64, 96),
            AssemblyPiece("fill", data=fill),
            AssemblyPiece("source", 0, 32),
            AssemblyPiece("source", 64, 96),
            AssemblyPiece("source", 32, 64),
        ]

    def _product(self):
        pieces = self._assembly()
        product = bytearray()
        for piece in pieces:
            if piece.kind == "fill":
                product.extend(piece.data)
            else:
                product.extend(self.source[piece.start:piece.end])
        return bytes(product)

    def test_unique_crc_matches_crccheck(self):
        solver = Solver(ARC16, self._records(), assembly=self._assembly())
        report = solver.run()
        expected = calc(ARC16, self._product())
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["crc"], expected)
        self.assertFalse(report["has_contradiction"])
        segment_bytes = {
            index: self.source[start:end]
            for index, (start, end) in enumerate(solver.segments)
        }
        verification = solver.verify_with_crccheck(segment_bytes)
        self.assertTrue(verification["matches"])
        self.assertFalse(verification["undetermined"])
        self.assertEqual(verification["crccheck_result"], expected)

    def test_target_with_full_output_stage(self):
        records = [LedgerRecord("c32", CRC32, 0, 96, calc(CRC32, self.source[0:96]))]
        assembly = [AssemblyPiece("source", 0, 96)]
        solver = Solver(CRC32, records, assembly=assembly)
        report = solver.run()
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["crc"], calc(CRC32, self.source[0:96]))

    def test_known_blocks_replace_reread(self):
        records = [LedgerRecord("head", ARC16, 0, 32, calc(ARC16, self.source[0:32]))]
        known = [{"start": 32, "end": 96, "data": self.source[32:96]}]
        solver = Solver(ARC16, records, known_blocks=known,
                        assembly=[AssemblyPiece("source", 0, 96)])
        report = solver.run()
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["crc"], calc(ARC16, self.source[0:96]))

    def test_contradiction_is_reported_while_crc_stays_derivable(self):
        records = self._records() + [
            LedgerRecord("tampered", ARC16, 0, 96,
                         calc(ARC16, self.source[0:96]) ^ 0x0001),
        ]
        solver = Solver(ARC16, records, assembly=self._assembly())
        report = solver.run()
        self.assertTrue(report["has_contradiction"])
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["crc"], calc(ARC16, self._product()))
        ids = {source for contradiction in report["contradictions"]
               for source in contradiction["sources_a"] + contradiction["sources_b"]}
        self.assertIn("tampered", ids)


class TestRereadPlanning(TestCase):

    def setUp(self):
        random.seed(501)
        self.source = bytes(random.randrange(256) for _ in range(128))

    def test_cheapest_plan_with_overlap_counted_once(self):
        records = [LedgerRecord("head", ARC16, 0, 24, calc(ARC16, self.source[0:24]))]
        assembly = [AssemblyPiece("source", 0, 88)]
        options = [
            RereadOption("p1", 24, 60),
            RereadOption("p2", 48, 88),
            RereadOption("p3", 8, 40),
            RereadOption("p4", 100, 110),
        ]
        solver = Solver(ARC16, records, assembly=assembly, reread_options=options)
        report = solver.run()
        self.assertEqual(report["status"], "needs_reread")
        option_by_id = {option.id: option for option in options}

        def feasible(option_ids):
            covered = set()
            for option_id in option_ids:
                option = option_by_id[option_id]
                cursor = option.start
                while cursor < option.end:
                    covered.add(solver._index_by_start[cursor])
                    cursor = solver.segments[solver._index_by_start[cursor]][1]
            _, terms = solver._symbolic_assembly(covered, structural=True)
            return not solver._active_terms(terms)

        def cost(option_ids):
            covered = set()
            for option_id in option_ids:
                option = option_by_id[option_id]
                cursor = option.start
                while cursor < option.end:
                    covered.add(solver._index_by_start[cursor])
                    cursor = solver.segments[solver._index_by_start[cursor]][1]
            return sum(solver.segments[index][1] - solver.segments[index][0]
                       for index in covered)

        all_ids = [option.id for option in options]
        subset_costs = {}
        for size in range(1, len(all_ids) + 1):
            for subset in itertools.combinations(all_ids, size):
                if feasible(subset):
                    subset_costs[frozenset(subset)] = cost(subset)
        self.assertTrue(subset_costs)
        minimum = min(subset_costs.values())
        self.assertEqual(report["reread_bytes"], minimum)
        self.assertEqual(cost(report["reread_plan"]), minimum)
        # p4 covers no dependent segment and must never enter the plan.
        self.assertNotIn("p4", report["reread_plan"])

        # Executing the plan with real bytes settles the product CRC.
        chosen_segments = set()
        for option_id in report["reread_plan"]:
            chosen_segments.update(solver.option_segments[option_id])
        retrieved = {
            index: self.source[start:end]
            for index, (start, end) in enumerate(solver.segments)
            if index in chosen_segments
        }
        all_bytes = {
            index: self.source[start:end]
            for index, (start, end) in enumerate(solver.segments)
            if end <= 88
        }
        self.assertTrue(solver.verify_with_crccheck(all_bytes)["matches"])
        self.assertEqual(solver.verify_with_crccheck(all_bytes)["crccheck_result"],
                         calc(ARC16, self.source[0:88]))
        self.assertTrue(retrieved)

    def test_plan_charges_incidental_bytes_and_uses_closure_difference(self):
        # Only [0,32) is on record while the manifest needs [0,64). Option p0
        # re-reads [0,48), overlapping 32 already-known bytes; its extra
        # [32,48) is derived from L[0,48) and the record by the suffix rule.
        # p1 supplies [48,64). The plan must charge p0's full 48 bytes, not
        # just its 16 unknown ones, and must be recognised as sufficient even
        # though no single option contains the whole unknown [32,64).
        records = [LedgerRecord("head", ARC16, 0, 32, calc(ARC16, self.source[0:32]))]
        assembly = [AssemblyPiece("source", 0, 64)]
        options = [
            RereadOption("p0", 0, 48),
            RereadOption("p1", 48, 64),
        ]
        solver = Solver(ARC16, records, assembly=assembly, reread_options=options)
        report = solver.run()
        self.assertEqual(report["status"], "needs_reread")
        self.assertEqual(sorted(report["reread_plan"]), ["p0", "p1"])
        # Union of [0,48) and [48,64) is 64 distinct physical bytes.
        self.assertEqual(report["reread_bytes"], 64)
        segment_bytes = {
            index: self.source[start:end]
            for index, (start, end) in enumerate(solver.segments)
            if end <= 64
        }
        verification = solver.verify_with_crccheck(segment_bytes)
        self.assertTrue(verification["matches"])
        self.assertEqual(verification["crccheck_result"],
                         calc(ARC16, self.source[0:64]))

    def test_indeterminate_when_no_option_covers_dependency(self):
        records = [LedgerRecord("head", ARC16, 0, 16, calc(ARC16, self.source[0:16]))]
        assembly = [AssemblyPiece("source", 0, 64)]
        options = [RereadOption("far", 96, 112)]
        solver = Solver(ARC16, records, assembly=assembly, reread_options=options)
        report = solver.run()
        self.assertEqual(report["status"], "indeterminate")
        self.assertNotEqual(int(report["undetermined_mask"], 16), 0)
        self.assertIn("determined_crc_bits", report)

    def test_partial_reread_still_indeterminate(self):
        records = [LedgerRecord("head", ARC16, 0, 16, calc(ARC16, self.source[0:16]))]
        assembly = [AssemblyPiece("source", 0, 80)]
        options = [RereadOption("partial", 16, 48)]
        solver = Solver(ARC16, records, assembly=assembly, reread_options=options)
        report = solver.run()
        self.assertEqual(report["status"], "indeterminate")
        self.assertIsNone(report["reread_plan"])
        self.assertNotEqual(int(report["undetermined_mask"], 16), 0)

    def test_repeated_segment_shares_one_variable(self):
        # The same physical segment referenced twice is one unknown, not two
        # independent ones: the two occurrences merge into a single symbolic
        # term with coefficient A(n) XOR I rather than two separate terms.
        records = [LedgerRecord("head", ARC16, 0, 16, calc(ARC16, self.source[0:16]))]
        assembly = [
            AssemblyPiece("source", 16, 48),
            AssemblyPiece("source", 16, 48),
        ]
        solver = Solver(ARC16, records, assembly=assembly)
        _, terms = solver._symbolic_assembly(set())
        active = solver._active_terms(terms)
        self.assertEqual(len(active), 1)
        report = solver.run()
        self.assertEqual(report["status"], "indeterminate")

        options = [RereadOption("p1", 16, 48)]
        solver = Solver(ARC16, records, assembly=assembly, reread_options=options)
        report = solver.run()
        self.assertEqual(report["status"], "needs_reread")
        self.assertEqual(report["reread_plan"], ["p1"])
        self.assertEqual(report["reread_bytes"], 32)
        segment_bytes = {
            index: self.source[start:end]
            for index, (start, end) in enumerate(solver.segments)
            if end <= 48
        }
        verification = solver.verify_with_crccheck(segment_bytes)
        self.assertTrue(verification["matches"])
        self.assertEqual(verification["crccheck_result"],
                         calc(ARC16, self.source[16:48] * 2))


class TestWorkOrderParsing(TestCase):

    def test_solve_work_order_round_trip(self):
        random.seed(601)
        source = bytes(random.randrange(256) for _ in range(64))
        order = {
            "target": ARC16,
            "ledger": [
                {"id": "r1", "volume": "vA", "params": ARC16,
                 "start": 0, "end": 64, "result": "0x%x" % calc(ARC16, source)},
                {"id": "r2", "volume": "vA", "params": ARC16,
                 "start": 32, "end": 64,
                 "result": "0x%x" % calc(ARC16, source[32:64])},
            ],
            "known": [{"start": 0, "end": 4, "data": source[0:4].hex()}],
            "assembly": [
                {"type": "source", "start": 32, "end": 64},
                {"type": "fill", "data": "ff00"},
                {"type": "source", "start": 0, "end": 32},
            ],
            "reread": [],
        }
        report = solve_work_order(order)
        product = source[32:64] + bytes([0xFF, 0x00]) + source[0:32]
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["crc"], calc(ARC16, product))


class TestEdgeCases(TestCase):

    def test_empty_product_uses_initial_and_output_stage(self):
        params = dict(width=16, poly=0x8005, initvalue=0x1234,
                      reflect_input=True, reflect_output=True,
                      xor_output=0xABCD)
        report = Solver(params, [], assembly=[]).run()
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["crc"], calc(params, b""))

    def test_fill_only_product(self):
        report = Solver(ARC16, [],
                        assembly=[AssemblyPiece("fill", data=b"hello")]).run()
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["crc"], calc(ARC16, b"hello"))

    def test_sub_byte_width_round_trip(self):
        data = bytes(range(20))
        solver = Solver(
            CRC5USB,
            [LedgerRecord("r5", CRC5USB, 0, 20, calc(CRC5USB, data))],
            assembly=[AssemblyPiece("source", 0, 20)])
        report = solver.run()
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["crc"], calc(CRC5USB, data))

    def test_reflected_mask_is_in_final_bit_space_and_fixed_bits_cleared(self):
        solver = Solver(
            ARC16,
            [LedgerRecord("r", ARC16, 0, 4, calc(ARC16, b"\x01" * 4))],
            assembly=[AssemblyPiece("source", 0, 12)],
            reread_options=[RereadOption("p", 8, 10)])
        report = solver.run()
        self.assertEqual(report["status"], "indeterminate")
        mask = int(report["undetermined_mask"], 16)
        self.assertEqual(mask, 0xFFFF)
        self.assertEqual(int(report["determined_crc_bits"], 16) & mask, 0)

    def test_invalid_constructors_reject_bad_inputs(self):
        with self.assertRaises(ValueError):
            LedgerRecord("x", ARC16, 10, 10, 0)
        with self.assertRaises(ValueError):
            AssemblyPiece("source", 5, 5)
        with self.assertRaises(ValueError):
            AssemblyPiece("fill", data=b"")
        with self.assertRaises(ValueError):
            RereadOption("p", 7, 7)
        with self.assertRaises(ValueError):
            Solver(ARC16, [],
                   known_blocks=[{"start": 0, "end": 4, "data": b"\x01\x02"}])


class TestLargeVolume(TestCase):

    def test_huge_known_interval_is_logarithmic(self):
        huge = 400_000_000
        records = [LedgerRecord("big", ARC16, 0, huge, 0x1234)]
        assembly = [AssemblyPiece("source", 0, huge)]
        solver = Solver(ARC16, records, assembly=assembly)
        started = time.time()
        report = solver.run()
        self.assertLess(time.time() - started, 5.0)
        self.assertEqual(report["status"], "unique")
        # Only width-sized matrix state exists, never one entry per byte.
        self.assertLessEqual(len(solver.segments), 2)

    def test_huge_unknown_run_uses_matrix_power_not_per_byte_state(self):
        huge = 400_000_000
        # Only a 16-byte head record exists; the remaining ~400M bytes are
        # unknown. Advancing over them must use A(huge) via fast exponentiation.
        records = [LedgerRecord("head", ARC16, 0, 16,
                                calc(ARC16, b"\x07" * 16))]
        assembly = [AssemblyPiece("source", 0, huge)]
        solver = Solver(ARC16, records, assembly=assembly)
        started = time.time()
        report = solver.run()
        self.assertLess(time.time() - started, 5.0)
        self.assertEqual(report["status"], "indeterminate")
        self.assertNotEqual(int(report["undetermined_mask"], 16), 0)

    def test_huge_gap_between_known_pieces(self):
        huge = 300_000_000
        records = [
            LedgerRecord("a", CCITT16, 0, 16, calc(CCITT16, b"\x01" * 16)),
            LedgerRecord("b", CCITT16, huge, huge + 16,
                         calc(CCITT16, b"\x02" * 16)),
        ]
        assembly = [
            AssemblyPiece("source", 0, 16),
            AssemblyPiece("source", huge, huge + 16),
        ]
        solver = Solver(CCITT16, records, assembly=assembly)
        started = time.time()
        report = solver.run()
        self.assertLess(time.time() - started, 5.0)
        self.assertEqual(report["status"], "unique")
        expected = calc(CCITT16, b"\x01" * 16 + b"\x02" * 16)
        self.assertEqual(report["crc"], expected)
