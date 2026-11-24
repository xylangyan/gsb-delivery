""" CRC derivation from historical ledger records and assembly manifests.

    This module solves the following problem without ever materialising the
    (potentially hundreds of megabytes long) source volume:

    * A ledger record states that a certain source byte interval was checked
      with a fully parametrised CRC algorithm (width, polynomial, initial
      value, input/output reflection and output XOR) and gives the resulting
      final CRC value.
    * A small number of literal source bytes may be known from the work order.
    * An assembly manifest describes the finished product as an ordered
      sequence of pieces, each piece either a reference to a source interval
      or a literal fill block. The same source interval may be referenced more
      than once, in any order, possibly interleaved with fill blocks.
    * The unique CRC of the finished product must be derived. When the
      available evidence is insufficient the cheapest set of permitted
      re-read intervals has to be selected so that the result becomes unique,
      counting overlapping physical bytes only once. If even a full re-read
      cannot settle the result the still undetermined check bits are reported.
    * Ledger records which cannot hold at the same time must be identified as
      contradictions.

    All algebra happens in the internal register space of
    :class:`crccheck.crc.Crc` (the value of ``_value`` after ``process`` and
    before ``final``).  Processing ``n`` bytes is an affine GF(2) map on the
    register state::

        S(X || Y) = A(|Y|) * S(X) ^ L(Y)

    where ``A(n)`` is the linear map obtained by clocking ``n`` zero bytes
    through the register and ``L(data)`` is the data contribution obtained by
    processing ``data`` from the zero state.  ``A(n)`` is a square matrix of
    the CRC width only, so its cost is O(log n) matrix multiplications and is
    independent of the volume size.  No per-byte state is ever created for
    bytes which have not been retrieved.

    Work orders are JSON documents of the following form::

        {
          "target": {"width": 32, "poly": 79764919, "initvalue": 4294967295,
                     "reflect_input": true, "reflect_output": true,
                     "xor_output": 4294967295},
          "ledger": [{"id": "r1", "volume": "vA",
                      "params": {"width": 16, "poly": 4129, ...},
                      "start": 0, "end": 64, "result": "0x1234"}],
          "known": [{"start": 10, "end": 14, "data": "deadbeef"}],
          "assembly": [{"type": "source", "start": 32, "end": 48},
                       {"type": "fill", "data": "00ff"},
                       {"type": "source", "start": 48, "end": 64},
                       {"type": "source", "start": 32, "end": 48}],
          "reread": [{"id": "p1", "start": 48, "end": 96}]
        }

    ``result`` accepts either an integer or a hexadecimal string.  Intervals
    are half-open byte ranges ``[start, end)`` addressed on the physical
    source byte axis, independently of the volume label a record may carry.
"""
from collections import deque
import json

from crccheck.base import reflectbitorder
from crccheck.crc import Crc


# ---------------------------------------------------------------------------
# GF(2) square matrices.
#
# A width x width matrix is stored as a list of its *columns*: element i is
# the integer column vector M * e_i. This makes multiplying by a state vector
# a cheap XOR of the columns whose input bit is set.
# ---------------------------------------------------------------------------

def _mat_zero_matrix(width):
    return [0] * width


def _mat_xor(left, right, width):
    """Return left XOR right over GF(2)."""
    return [left[i] ^ right[i] for i in range(width)]


def _mat_identity(width):
    return [1 << i for i in range(width)]


def _mat_mul(left, right, width):
    """Return left * right over GF(2)."""
    # Column j of the product is left * (column j of right).
    return [_mat_vec(left, right[col]) for col in range(width)]


def _mat_pow(matrix, exponent, width):
    """Fast exponentiation of a GF(2) square matrix."""
    result = _mat_identity(width)
    base = matrix
    while exponent:
        if exponent & 1:
            result = _mat_mul(result, base, width)
        exponent >>= 1
        if exponent:
            base = _mat_mul(base, base, width)
    return result


def _mat_vec(matrix, vector):
    """Multiply a GF(2) matrix by a column vector given as an integer."""
    result = 0
    column_index = 0
    while vector:
        if vector & 1:
            result ^= matrix[column_index]
        vector >>= 1
        column_index += 1
    return result


def _mat_inverse(matrix, width):
    """Gauss-Jordan inversion over GF(2) for column-stored matrices."""
    rows = [0] * width
    for col_index, column in enumerate(matrix):
        for row in range(width):
            if column & (1 << row):
                rows[row] |= 1 << col_index
    inverse_rows = _mat_identity(width)
    for col in range(width):
        pivot = col
        mask = 1 << col
        while pivot < width and not (rows[pivot] & mask):
            pivot += 1
        if pivot == width:
            raise ValueError("CRC state transition matrix is not invertible")
        if pivot != col:
            rows[col], rows[pivot] = rows[pivot], rows[col]
            inverse_rows[col], inverse_rows[pivot] = inverse_rows[pivot], inverse_rows[col]
        for row in range(width):
            if row != col and (rows[row] & mask):
                rows[row] ^= rows[col]
                inverse_rows[row] ^= inverse_rows[col]
    # Transpose the row-stored inverse back into column storage.
    inverse_columns = [0] * width
    for row_index, row in enumerate(inverse_rows):
        for col in range(width):
            if row & (1 << col):
                inverse_columns[col] |= 1 << row_index
    return inverse_columns


def _mat_zero(matrix):
    return not any(matrix)


# ---------------------------------------------------------------------------
# CRC algebra for one fixed parameter set.
# ---------------------------------------------------------------------------

class CrcAlgebra(object):
    """Linear CRC operators in the internal register space of one algorithm.

    Args:
        params (dict): ``width``, ``poly``, ``initvalue``,
            ``reflect_input``, ``reflect_output``, ``xor_output``.
    """

    def __init__(self, params):
        self.width = int(params["width"])
        self.poly = int(params["poly"])
        self.initvalue = int(params.get("initvalue", 0))
        self.reflect_input = bool(params.get("reflect_input", False))
        self.reflect_output = bool(params.get("reflect_output", False))
        self.xor_output = int(params.get("xor_output", 0))
        self.params = (
            self.width, self.poly, self.initvalue,
            self.reflect_input, self.reflect_output, self.xor_output,
        )
        self._advance_cache = {}
        self._inverse_cache = {}
        self._injection_cache = {}
        self._build_transition()

    def _new_crc(self, initvalue=None):
        return Crc(
            self.width, self.poly,
            self.initvalue if initvalue is None else initvalue,
            self.reflect_input, self.reflect_output, self.xor_output,
        )

    def _build_transition(self):
        """Build A(1), the one-zero-byte transition on the register state."""
        transition = []
        for bit in range(self.width):
            probe = self._new_crc(initvalue=0)
            probe.reset(1 << bit)
            probe.process(b"\x00")
            transition.append(probe.value())
        self._advance_cache[0] = _mat_identity(self.width)
        self._advance_cache[1] = transition

    def advance(self, length):
        """Return A(length): the state transition of ``length`` zero bytes."""
        cached = self._advance_cache.get(length)
        if cached is None:
            cached = _mat_pow(self._advance_cache[1], length, self.width)
            self._advance_cache[length] = cached
        return cached

    def advance_inverse(self, length):
        """Return the inverse of :meth:`advance`."""
        if length == 0:
            return _mat_identity(self.width)
        cached = self._inverse_cache.get(length)
        if cached is None:
            inverse_one = _mat_inverse(self._advance_cache[1], self.width)
            cached = _mat_pow(inverse_one, length, self.width)
            self._inverse_cache[length] = cached
        return cached

    def data_contribution(self, data):
        """Return L(data): internal state of processing ``data`` from zero."""
        probe = self._new_crc(initvalue=0)
        probe.process(data)
        return probe.value()

    def final_to_inner(self, result):
        """Translate a recorded final CRC into the internal register state."""
        value = int(result) ^ self.xor_output
        if self.reflect_output:
            value = reflectbitorder(self.width, value)
        return value

    def inner_to_final(self, inner):
        """Translate an internal register state into the final CRC value."""
        value = reflectbitorder(self.width, inner) if self.reflect_output else inner
        return value ^ self.xor_output

    def record_to_contribution(self, result, length):
        """Turn a ledger result into L(interval) for an interval of ``length``.

        The internal state over the interval is
        ``S = A(length) * initvalue ^ L(data)``.
        """
        inner = self.final_to_inner(result)
        return inner ^ _mat_vec(self.advance(length), self.initvalue)

    def injection_space(self, length):
        """Return the span of L(segment) as GF(2) register vectors.

        ``L`` maps the ``8 * length`` input bits of a segment onto the
        register state.  Its image is span(A^k * v) over the eight
        single-byte injection vectors ``v`` for ``0 <= k < length``. The
        span is grown one A-application per round until it becomes
        A-invariant; each round either raises the rank or terminates, so
        there are at most ``width`` rounds independently of ``length`` and
        the basis never exceeds ``width`` vectors.
        """
        cached = self._injection_cache.get(length)
        if cached is not None:
            return cached
        basis = []
        for seed in self._byte_injection():
            self._extend_basis(basis, seed)
        advance_one = self._advance_cache[1]
        for _ in range(min(length - 1, self.width)):
            rank_before = len(basis)
            for vector in list(basis):
                self._extend_basis(basis, _mat_vec(advance_one, vector))
            if len(basis) == rank_before:
                break
        self._injection_cache[length] = basis
        return basis

    def _byte_injection(self):
        probe = self._new_crc(initvalue=0)
        vectors = []
        for input_bit in range(8):
            probe.reset(0)
            probe.process(bytes([1 << input_bit]))
            vectors.append(probe.value())
        return vectors

    @staticmethod
    def _extend_basis(basis, vector):
        """Insert ``vector`` into an XOR linear basis with unique pivots.

        Every basis vector owns a distinct lowest-set bit (its pivot) and the
        list is kept in descending pivot order, which makes membership
        reduction a single ordered pass.
        """
        reduced = vector
        for pivot_vector in basis:
            pivot_bit = pivot_vector & -pivot_vector
            if reduced & pivot_bit:
                reduced ^= pivot_vector
        if not reduced:
            return
        pivot = reduced & -reduced
        for index, existing in enumerate(basis):
            if existing & pivot:
                basis[index] = existing ^ reduced
        basis.append(reduced)
        basis.sort(key=lambda value: value & -value, reverse=True)


# ---------------------------------------------------------------------------
# Input data model.
# ---------------------------------------------------------------------------

class LedgerRecord(object):
    """One historical CRC check over a physical source interval.

    Args:
        record_id (str): identifier used in contradiction reports.
        params (dict): CRC algorithm parameters.
        start (int), end (int): half-open physical byte interval.
        result (int or str): recorded final CRC.
        volume (str or None): volume label, informational only.
    """

    def __init__(self, record_id, params, start, end, result, volume=None):
        self.id = str(record_id)
        self.volume = volume
        self.params = dict(params)
        self.start = int(start)
        self.end = int(end)
        self.result = result
        if self.end <= self.start:
            raise ValueError("record %s covers an empty interval" % self.id)

    @property
    def length(self):
        return self.end - self.start


class AssemblyPiece(object):
    """One piece of an assembly manifest.

    A source piece references the physical interval ``[start, end)``.
    A fill piece carries literal bytes.
    """

    def __init__(self, kind, start=None, end=None, data=None):
        self.kind = kind
        self.start = None if start is None else int(start)
        self.end = None if end is None else int(end)
        self.data = data
        if kind == "source":
            if self.end <= self.start:
                raise ValueError("source piece covers an empty interval")
        elif kind == "fill":
            if not data:
                raise ValueError("fill piece carries no data")
        else:
            raise ValueError("unknown assembly piece kind %r" % kind)

    @property
    def length(self):
        if self.kind == "source":
            return self.end - self.start
        return len(self.data)


class RereadOption(object):
    """One work-order permitted re-read interval."""

    def __init__(self, option_id, start, end):
        self.id = str(option_id)
        self.start = int(start)
        self.end = int(end)
        if self.end <= self.start:
            raise ValueError("re-read option %s covers an empty interval" % self.id)

    @property
    def length(self):
        return self.end - self.start


# ---------------------------------------------------------------------------
# Interval closure: derive every L(interval) the evidence can settle.
# ---------------------------------------------------------------------------

class Closure(object):
    """Known ``L(interval)`` values for one CRC parameter set.

    The engine only stores values on intervals bounded by atomic segment
    boundaries, so the number of intervals is quadratic in the (small) number
    of records and manifest references, never in the volume size.

    A structural closure only records *which* intervals are knowable. It is
    used for re-read feasibility probes where the retrieved bytes do not exist
    yet, so placeholder values must never trigger a spurious contradiction.
    """

    def __init__(self, algebra, structural=False):
        self.algebra = algebra
        self.structural = structural
        self.values = {}
        self.provenance = {}
        self.by_start = {}
        self.by_end = {}
        self.contradictions = []
        self._queue = deque()

    def add(self, start, end, value, source):
        """Offer an L value; return False if it contradicts a known one."""
        key = (start, end)
        if key in self.values:
            if self.structural:
                return True
            existing = self.values[key]
            if existing != value:
                self.contradictions.append({
                    "interval": [start, end],
                    "params": self.algebra.params,
                    "value_a": existing,
                    "value_b": value,
                    "sources_a": sorted(self.provenance[key]),
                    "sources_b": sorted(source),
                })
                return False
            return True
        self.values[key] = value
        self.provenance[key] = frozenset(source)
        self.by_start.setdefault(start, set()).add(end)
        self.by_end.setdefault(end, set()).add(start)
        self._queue.append(key)
        return True

    def run(self):
        """Apply prefix/suffix/adjacency rules until no interval is new."""
        while self._queue:
            start, end = self._queue.popleft()
            value = self.values[(start, end)]
            source = self.provenance[(start, end)]
            self._merge_adjacent(start, end, value, source)
            self._split_shared_start(start, end, value, source)
            self._split_shared_end(start, end, value, source)

    def _merge_adjacent(self, start, end, value, source):
        # J = [other_start, start) immediately before this interval.
        for other_start in self.by_end.get(start, ()):
            if other_start == start:
                continue
            other_value = self.values[(other_start, start)]
            merged = _mat_vec(self.algebra.advance(end - start), other_value) ^ value
            self.add(other_start, end, merged,
                     source | self.provenance[(other_start, start)])
        # J = [end, other_end) immediately after this interval.
        for other_end in self.by_start.get(end, ()):
            if other_end == end:
                continue
            other_value = self.values[(end, other_end)]
            merged = _mat_vec(self.algebra.advance(other_end - end), value) ^ other_value
            self.add(start, other_end, merged,
                     source | self.provenance[(end, other_end)])

    def _split_shared_start(self, start, end, value, source):
        for other_end in self.by_start.get(start, ()):
            if other_end == end:
                continue
            if other_end < end:
                short_end, long_end = other_end, end
                short_value = self.values[(start, short_end)]
                long_value = value
            else:
                short_end, long_end = end, other_end
                short_value = value
                long_value = self.values[(start, long_end)]
            suffix = long_value ^ _mat_vec(
                self.algebra.advance(long_end - short_end), short_value)
            self.add(short_end, long_end, suffix,
                     source | self.provenance[(start, other_end)])

    def _split_shared_end(self, start, end, value, source):
        for other_start in self.by_end.get(end, ()):
            if other_start == start:
                continue
            if other_start < start:
                long_start, short_start = other_start, start
                long_value = self.values[(long_start, end)]
                short_value = value
            else:
                long_start, short_start = start, other_start
                long_value = value
                short_value = self.values[(short_start, end)]
            prefix = _mat_vec(
                self.algebra.advance_inverse(end - short_start),
                long_value ^ short_value)
            self.add(long_start, short_start, prefix,
                     source | self.provenance[(other_start, end)])

    def longest_known_from(self, start, limit):
        """Return the largest known end ``e`` with ``start < e <= limit``.

        Returns ``None`` when no known interval starts at ``start``.
        """
        candidates = [end for end in self.by_start.get(start, ()) if end <= limit]
        if not candidates:
            return None
        return max(candidates)


# ---------------------------------------------------------------------------
# Solver.
# ---------------------------------------------------------------------------

class Solver(object):
    """Derives the uniquely confirmable CRC of an assembly manifest.

    Args:
        target_params (dict): CRC algorithm of the finished product.
        records (iterable of LedgerRecord): historical ledger entries.
        known_blocks (iterable of dict): literal source bytes as
            ``{"start": int, "end": int, "data": bytes}``.
        assembly (iterable of AssemblyPiece): ordered manifest pieces.
        reread_options (iterable of RereadOption): permitted re-reads.
    """

    def __init__(self, target_params, records, known_blocks=(),
                 assembly=(), reread_options=()):
        self.target = CrcAlgebra(target_params)
        self.records = list(records)
        self.known_blocks = [
            (int(block["start"]), int(block["end"]), bytes(block["data"]))
            for block in known_blocks
        ]
        for start, end, data in self.known_blocks:
            if end - start != len(data):
                raise ValueError("known block %d..%d has %d bytes" % (start, end, len(data)))
        self.assembly = list(assembly)
        self.reread_options = list(reread_options)
        self._build_axes()
        self._build_algebras()
        self.contradictions = []

    # -- axis construction -------------------------------------------------

    def _build_axes(self):
        boundaries = set()
        for record in self.records:
            boundaries.update((record.start, record.end))
        for start, end, _ in self.known_blocks:
            boundaries.update((start, end))
        for piece in self.assembly:
            if piece.kind == "source":
                boundaries.update((piece.start, piece.end))
        for option in self.reread_options:
            boundaries.update((option.start, option.end))
        self.boundaries = sorted(boundaries)
        self.segments = list(zip(self.boundaries[:-1], self.boundaries[1:]))
        self._index_by_start = {start: index for index, (start, _) in enumerate(self.segments)}
        self.known_segment_data = {}
        for start, end, data in self.known_blocks:
            offset = 0
            cursor = start
            while cursor < end:
                seg_index = self._index_by_start[cursor]
                seg_start, seg_end = self.segments[seg_index]
                take = min(seg_end, end) - cursor
                self.known_segment_data[seg_index] = data[offset:offset + take]
                offset += take
                cursor += take
        self.option_segments = {}
        for option in self.reread_options:
            indices = []
            cursor = option.start
            while cursor < option.end:
                indices.append(self._index_by_start[cursor])
                cursor = self.segments[indices[-1]][1]
            self.option_segments[option.id] = indices

    def _build_algebras(self):
        groups = {self.target.params: self.target}
        for record in self.records:
            algebra = CrcAlgebra(record.params)
            groups.setdefault(algebra.params, algebra)
        self.algebras = list(groups.values())
        self.records_by_params = {}
        for record in self.records:
            algebra = self._algebra_for(record.params)
            self.records_by_params.setdefault(algebra.params, []).append(record)

    def _algebra_for(self, params):
        key = CrcAlgebra(params).params
        for algebra in self.algebras:
            if algebra.params == key:
                return algebra
        raise KeyError("algebra not registered")

    # -- closure construction ---------------------------------------------

    def _run_closure(self, algebra, extra_known_segments=(), extra_data=None,
                     structural=False):
        """Build the interval closure for one parameter set.

        ``extra_known_segments`` are treated as retrieved bytes for a
        feasibility probe. A structural probe only propagates interval
        knowability and never checks values, because the retrieved bytes do
        not exist yet. When ``extra_data`` supplies real bytes for a segment
        a numeric closure is built with the true L values and contradictions
        are detected.
        """
        closure = Closure(algebra, structural=structural)
        placeholder = set(extra_known_segments)
        extra_data = extra_data or {}
        for seg_index, (start, end) in enumerate(self.segments):
            if seg_index in self.known_segment_data:
                value = algebra.data_contribution(self.known_segment_data[seg_index])
                closure.add(start, end, value, {"known:%d" % seg_index})
            elif seg_index in extra_data:
                value = algebra.data_contribution(extra_data[seg_index])
                closure.add(start, end, value, {"provided:%d" % seg_index})
            elif seg_index in placeholder:
                closure.add(start, end, 0, {"reread-probe"})
        for record in self.records_by_params.get(algebra.params, ()):
            value = algebra.record_to_contribution(record.result, record.length)
            closure.add(record.start, record.end, value, {record.id})
        closure.run()
        return closure

    def run(self):
        """Run the full analysis and return a result dictionary.

        Status values:

        * ``unique``: every product CRC bit is fixed by the evidence;
          ``crc`` is the confirmable value.
        * ``needs_reread``: a re-read plan settles every bit; the actual CRC
          can only be computed once those bytes come back.
        * ``indeterminate``: even reading every permitted interval leaves the
          bits in ``undetermined_mask`` open; ``determined_crc_bits`` carries
          the fixed value on every other bit.

        ``has_contradiction`` is reported independently of determinability:
        contradictory ledger records cannot all be true regardless of whether
        the product CRC itself is already fixed.
        """
        self.closures = {}
        for algebra in self.algebras:
            closure = self._run_closure(algebra)
            self.closures[algebra.params] = closure
            self.contradictions.extend(closure.contradictions)
        const, terms = self._symbolic_assembly(set())
        result = {
            "status": "unique",
            "crc": None,
            "undetermined_mask": "0x0",
            "has_contradiction": bool(self.contradictions),
            "contradictions": self.contradictions,
            "reread_plan": None,
            "reread_bytes": 0,
        }
        active_terms = self._active_terms(terms)
        if not active_terms:
            result["crc"] = self.target.inner_to_final(const)
            return result
        plan = self._cheapest_reread(active_terms)
        if plan is None:
            mask = self._final_mask(active_terms)
            result["status"] = "indeterminate"
            result["undetermined_mask"] = "0x%x" % mask
            result["determined_crc_bits"] = "0x%x" % (
                self.target.inner_to_final(const) & self._fixed_bit_mask(mask))
            return result
        chosen_ids, chosen_segments, read_bytes = plan
        _, probe_terms = self._symbolic_assembly(
            set(chosen_segments), structural=True)
        residual_active = self._active_terms(probe_terms)
        if residual_active:
            # Defensive: the plan search only accepts feasible sets, so this
            # branch should be unreachable. Report the residual mask using a
            # structural probe and the fixed bits from the current evidence.
            mask = self._final_mask(residual_active)
            result["status"] = "indeterminate"
            result["undetermined_mask"] = "0x%x" % mask
            result["determined_crc_bits"] = "0x%x" % (
                self.target.inner_to_final(const) & self._fixed_bit_mask(mask))
            result["reread_plan"] = chosen_ids
            result["reread_bytes"] = read_bytes
            result["reread_intervals"] = self._plan_intervals(chosen_segments)
            return result
        result["status"] = "needs_reread"
        result["reread_plan"] = chosen_ids
        result["reread_bytes"] = read_bytes
        result["reread_intervals"] = self._plan_intervals(chosen_segments)
        return result

    # -- symbolic execution of the manifest -------------------------------

    def _symbolic_assembly(self, extra_known_segments, extra_data=None,
                           structural=False):
        # Start from the target initial register state: every subsequent A(n)
        # transform carries it exactly as the real register would, so the
        # initial value needs no special handling at the end.
        const = self.target.initvalue
        terms = {}
        closure = self._run_closure(
            self.target, extra_known_segments, extra_data, structural)
        for piece in self.assembly:
            if piece.kind == "fill":
                length = len(piece.data)
                matrix = self.target.advance(length)
                const = _mat_vec(matrix, const) ^ self.target.data_contribution(piece.data)
                terms = {seg: _mat_mul(matrix, coefficient, self.target.width)
                         for seg, coefficient in terms.items()}
                continue
            cursor = piece.start
            while cursor < piece.end:
                known_end = closure.longest_known_from(cursor, piece.end)
                if known_end is not None:
                    # Consume the longest interval the evidence settles as a
                    # whole; its L is a constant even when it spans several
                    # atomic segments.
                    length = known_end - cursor
                    matrix = self.target.advance(length)
                    const = _mat_vec(matrix, const)
                    terms = {seg: _mat_mul(matrix, coefficient, self.target.width)
                             for seg, coefficient in terms.items()}
                    const ^= closure.values[(cursor, known_end)]
                    cursor = known_end
                    continue
                seg_index = self._index_by_start[cursor]
                seg_start, seg_end = self.segments[seg_index]
                matrix = self.target.advance(seg_end - seg_start)
                const = _mat_vec(matrix, const)
                terms = {seg: _mat_mul(matrix, coefficient, self.target.width)
                         for seg, coefficient in terms.items()}
                # The segment's own L contributes through the identity map
                # here. Repeated manifest references to the same physical
                # segment accumulate their coefficient matrices over GF(2),
                # so the shared bytes are counted once.
                terms[seg_index] = _mat_xor(
                    terms.get(seg_index, _mat_zero_matrix(self.target.width)),
                    _mat_identity(self.target.width), self.target.width)
                cursor = seg_end
        return const, terms

    def _active_terms(self, terms):
        """Return ``{segment: coefficient}`` for genuinely dependent terms."""
        active = {}
        for seg_index, coefficient in terms.items():
            if _mat_zero(coefficient):
                continue
            start, end = self.segments[seg_index]
            if self._coefficient_is_live(coefficient, end - start):
                active[seg_index] = coefficient
        return active

    def _coefficient_is_live(self, coefficient, length):
        """Check whether coefficient * L(segment) can be a non-zero function."""
        for basis_vector in self.target.injection_space(length):
            if _mat_vec(coefficient, basis_vector):
                return True
        return False

    def _fixed_bit_mask(self, undetermined_mask):
        return ((1 << self.target.width) - 1) ^ undetermined_mask

    def _final_mask(self, active_terms):
        # With column storage, output state bit i is undetermined exactly
        # when bit i is set in any coefficient column.
        state_mask = 0
        for coefficient in active_terms.values():
            for column in coefficient:
                state_mask |= column
        if self.target.reflect_output:
            reflected = 0
            for bit in range(self.target.width):
                if state_mask & (1 << bit):
                    reflected |= 1 << (self.target.width - 1 - bit)
            return reflected
        return state_mask

    # -- cheapest re-read selection ---------------------------------------

    def _segment_byte_cost(self, segments):
        return sum(self.segments[index][1] - self.segments[index][0]
                   for index in segments)

    def _cheapest_reread(self, active_terms):
        if not self.reread_options:
            return None
        needed_segments = frozenset(active_terms)
        # Keep every option's full atomic-segment coverage: bytes outside the
        # currently dependent segments still cost reads and may let the
        # closure derive a dependent segment by difference instead of reading
        # it directly.
        options = []
        seen_coverage = set()
        for option in self.reread_options:
            covered = frozenset(self.option_segments[option.id])
            if not covered or covered in seen_coverage:
                continue
            seen_coverage.add(covered)
            options.append((option.id, covered))
        options.sort(key=lambda item: self._segment_byte_cost(item[1]))

        memo = {}

        def feasible(selected_segments):
            key = frozenset(selected_segments)
            cached = memo.get(key)
            if cached is not None:
                return cached
            _, probe_terms = self._symbolic_assembly(set(key), structural=True)
            cached = not self._active_terms(probe_terms)
            memo[key] = cached
            return cached

        greedy = self._greedy_solution(options)
        if greedy is None:
            # Greedy can miss a plan whose effectiveness only appears through
            # closure differences. Run the exact search with an effectively
            # unlimited upper bound instead of declaring impossibility.
            all_option_cost = sum(
                self._segment_byte_cost(covered) for _, covered in options)
            best_cost, best_selection = all_option_cost + 1, []
        else:
            best_cost, best_selection = greedy

        def lower_bound(position, selected_segments):
            # Any feasible selection must include at least one remaining
            # option covering each still-dependent segment, so its union cost
            # is at least the largest per-segment cheapest-option cost.
            # Taking the maximum keeps the bound admissible when one option
            # covers several segments. Segments which no remaining option
            # contains are skipped: the structural closure may still derive
            # them by difference from neighbouring reads.
            remaining_needed = needed_segments - selected_segments
            bound = 0
            for segment in remaining_needed:
                cheapest = None
                for idx in range(position, len(options)):
                    if segment in options[idx][1]:
                        cost = self._segment_byte_cost(options[idx][1])
                        if cheapest is None or cost < cheapest:
                            cheapest = cost
                if cheapest is not None and cheapest > bound:
                    bound = cheapest
            return bound

        def dfs(position, selected, selected_segments, cost):
            if cost >= best_holder[0][0]:
                return
            if feasible(selected_segments):
                best_holder[0] = (cost, list(selected))
                return
            if position == len(options):
                return
            bound = lower_bound(position, selected_segments)
            if bound is None or cost + bound >= best_holder[0][0]:
                return
            option_id, covered = options[position]
            added = covered - selected_segments
            added_cost = self._segment_byte_cost(added)
            # Take the option first; cheaper options were sorted upfront.
            dfs(position + 1, selected + [option_id],
                selected_segments | covered, cost + added_cost)
            dfs(position + 1, selected, selected_segments, cost)

        best_holder = [(best_cost, best_selection)]
        dfs(0, [], frozenset(), 0)
        final_cost, final_selection = best_holder[0]
        if not final_selection:
            return None
        chosen_segments = set()
        for option_id in final_selection:
            chosen_segments.update(self.option_segments[option_id])
        return sorted(final_selection), sorted(chosen_segments), final_cost

    def _greedy_solution(self, options):
        """Upper-bound plan: pick the most effective option per marginal byte."""
        selected = []
        covered = frozenset()
        cost = 0
        remaining = list(options)
        while True:
            _, probe_terms = self._symbolic_assembly(set(covered), structural=True)
            if not self._active_terms(probe_terms):
                return cost, selected
            if not remaining:
                return None
            best_option = None
            best_ratio = None
            for position, (option_id, option_covered) in enumerate(remaining):
                added = option_covered - covered
                if not added:
                    continue
                added_cost = self._segment_byte_cost(added)
                _, trial_terms = self._symbolic_assembly(
                    set(covered | option_covered), structural=True)
                active_after = len(self._active_terms(trial_terms))
                ratio = (-active_after, -added_cost)
                if best_ratio is None or ratio > best_ratio:
                    best_ratio = ratio
                    best_option = position
            if best_option is None:
                return None
            option_id, option_covered = remaining.pop(best_option)
            selected.append(option_id)
            cost += self._segment_byte_cost(option_covered - covered)
            covered |= option_covered

    def _plan_intervals(self, chosen_segments):
        if not chosen_segments:
            return []
        intervals = []
        indices = sorted(chosen_segments)
        run_start, run_end = self.segments[indices[0]]
        for seg_index in indices[1:]:
            start, end = self.segments[seg_index]
            if start == run_end:
                run_end = end
            else:
                intervals.append([run_start, run_end])
                run_start, run_end = start, end
        intervals.append([run_start, run_end])
        return intervals

    # -- full verification with crccheck ----------------------------------

    def verify_with_crccheck(self, segment_bytes):
        """Final cross-check by materialising the whole product.

        This mirrors reading the finished product end to end and running
        crccheck over it, so actual bytes must be available for every source
        segment the manifest references. Work-order literal blocks are merged
        automatically and need not be repeated.

        Args:
            segment_bytes (dict): literal bytes keyed by atomic segment
                index for every referenced segment not already supplied as a
                work-order known block.

        Returns:
            dict with the symbolic result, the direct crccheck result, the
            comparison outcome and any contradictions exposed by the real
            data across every ledger parameter set.
        """
        provided = dict(self.known_segment_data)
        provided.update(segment_bytes)
        product = bytearray()
        missing = []
        for piece in self.assembly:
            if piece.kind == "fill":
                product.extend(piece.data)
                continue
            cursor = piece.start
            while cursor < piece.end:
                seg_index = self._index_by_start[cursor]
                seg_start, seg_end = self.segments[seg_index]
                if seg_index not in provided:
                    missing.append(seg_index)
                else:
                    product.extend(provided[seg_index])
                cursor = seg_end
        if missing:
            raise ValueError(
                "verify_with_crccheck needs actual bytes for segments %s"
                % sorted(set(missing)))
        direct = self.target._new_crc()
        direct.process(bytes(product))
        direct_value = direct.final()
        const, terms = self._symbolic_assembly(set(segment_bytes), segment_bytes)
        active = self._active_terms(terms)
        symbolic = None if active else self.target.inner_to_final(const)
        contradictions = []
        for algebra in self.algebras:
            closure = self._run_closure(algebra, set(segment_bytes), segment_bytes)
            contradictions.extend(closure.contradictions)
        return {
            "product_length": len(product),
            "crccheck_result": direct_value,
            "symbolic_result": symbolic,
            "matches": symbolic == direct_value,
            "undetermined": bool(active),
            "contradictions": contradictions,
        }


# ---------------------------------------------------------------------------
# JSON work order parsing and command line entry point.
# ---------------------------------------------------------------------------

def _parse_int(value):
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16 if text.lower().startswith("0x") else 10)


def _parse_data(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    text = str(value)
    if text.lower().startswith("0x"):
        text = text[2:]
    return bytes.fromhex(text)


def solve_work_order(order):
    """Run a solver from a JSON-style work order dictionary."""
    target_params = order["target"]
    records = [
        LedgerRecord(
            record.get("id", "r%d" % index),
            record["params"],
            _parse_int(record["start"]),
            _parse_int(record["end"]),
            _parse_int(record["result"]),
            record.get("volume"),
        )
        for index, record in enumerate(order.get("ledger", []))
    ]
    known_blocks = [
        {
            "start": _parse_int(block["start"]),
            "end": _parse_int(block["end"]),
            "data": _parse_data(block["data"]),
        }
        for block in order.get("known", [])
    ]
    assembly = []
    for piece in order.get("assembly", []):
        if piece["type"] == "source":
            assembly.append(AssemblyPiece(
                "source", _parse_int(piece["start"]), _parse_int(piece["end"])))
        else:
            assembly.append(AssemblyPiece("fill", data=_parse_data(piece["data"])))
    reread = [
        RereadOption(
            option.get("id", "p%d" % index),
            _parse_int(option["start"]),
            _parse_int(option["end"]),
        )
        for index, option in enumerate(order.get("reread", []))
    ]
    solver = Solver(target_params, records, known_blocks, assembly, reread)
    return solver.run()


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("Usage: python -m crccheck.ledger WORK_ORDER.json")
        return 2
    with open(argv[0], "r", encoding="utf-8") as handle:
        order = json.load(handle)
    report = solve_work_order(order)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
