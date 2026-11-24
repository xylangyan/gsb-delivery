"""Run-length aware RePair grammar and position queries.

The SAX strings this package compresses are often *runs*: the same word repeated
for a long stretch.  Storing one token per repetition is wasteful at production
scale (a run can repeat millions of times), and so is the occurrence table that
:func:`saxpy.repair.str_to_repair_grammar` builds for it.

This module accepts a run-length archive -- a sequence of ``(word, count)``
records -- and builds the *same* RePair grammar the reference implementation
builds from the fully expanded token string: same rule ids, same rule parts,
same substituted positions.  It never expands the archive: the work string is a
small list of segments ``symbol x count``, and occurrence tables store
arithmetic ranges ``(first, step, count)`` instead of position lists.  A unary
run of ten million tokens therefore costs a handful of segments and ranges, not
ten million Python objects.

Entry points:

``rle_to_repair_grammar(runs)``
    Build a :class:`RepairGrammar` from ``[(word, count), ...]``.

``RepairGrammar``
    Rule queries: :meth:`~RepairGrammar.rule_length`,
    :meth:`~RepairGrammar.rule_parts`,
    :meth:`~RepairGrammar.rule_intervals`,
    :meth:`~RepairGrammar.rule_occurrences`,
    :meth:`~RepairGrammar.word_slice`.

Equivalence with the reference entry point is exact, not approximate: the
selection order (highest digram frequency, LIFO within a frequency bucket) and
the replacement order (occurrence list popped from its tail) are inherited from
:mod:`saxpy.repair`.  For a short archive, expanding it and calling
``str_to_repair_grammar`` yields the identical grammar.
"""

import bisect
import heapq
from dataclasses import dataclass, field


class _Occ:
    """Append-ordered occurrence positions, stored as arithmetic ranges.

    Each range is ``[first, step, count]``; ``step`` is 0 for a singleton.
    :meth:`append` extends the last range when the new position continues it,
    otherwise starts a new range, so range order is exactly append order.  That
    order is what RePair's tail-pop processing depends on, and it is why a long
    self-pairing run becomes one range instead of one record per position.
    """

    __slots__ = ("ranges", "count")

    def __init__(self):
        self.ranges = []
        self.count = 0

    def append(self, pos):
        self.count += 1
        if self.ranges:
            last = self.ranges[-1]
            if last[2] == 1:
                if last[0] == pos:
                    return
                last[1] = pos - last[0]
                last[2] = 2
                return
            if pos == last[0] + last[1] * last[2]:
                last[2] += 1
                return
        self.ranges.append([pos, 0, 1])

    def append_many(self, first, step, n):
        if n <= 0:
            return
        self.count += n
        if self.ranges:
            last = self.ranges[-1]
            if last[1] == step and last[0] + step * last[2] == first:
                last[2] += n
                return
        self.ranges.append([first, step, n])

    def remove(self, pos):
        ranges = self.ranges
        for k in range(len(ranges) - 1, -1, -1):
            first, step, n = ranges[k]
            if n == 1:
                if pos == first:
                    del ranges[k]
                    self.count -= 1
                    return True
                continue
            d = pos - first
            if d % step:
                continue
            i = d // step
            if 0 <= i < n:
                repl = []
                if i:
                    repl.append([first, step, i])
                if i + 1 < n:
                    repl.append([pos + step, step, n - i - 1])
                ranges[k : k + 1] = repl
                self.count -= 1
                return True
        return False

    def pop_last(self):
        r = self.ranges[-1]
        pos = r[0] + r[1] * (r[2] - 1)
        r[2] -= 1
        if r[2] == 0:
            self.ranges.pop()
        self.count -= 1
        return pos

    def first(self):
        return self.ranges[0][0]

    def last(self):
        r = self.ranges[-1]
        return r[0] + r[1] * (r[2] - 1)

    def positions(self, lo=None, hi=None):
        """Yield positions in append order, restricted to ``[lo, hi)``.

        The index span is computed per range, so a window over a million-long
        range touches only the positions inside it.
        """
        for first, step, n in self.ranges:
            if n == 1:
                if (lo is None or first >= lo) and (hi is None or first < hi):
                    yield first
                continue
            if step > 0:
                i0 = 0 if lo is None else max(0, _ceil_div(lo - first, step))
                i1 = n if hi is None else min(n, _ceil_div(hi - first, step))
            else:
                i0 = 0 if hi is None else max(0, (hi - first) // step + 1)
                i1 = n if lo is None else min(n, (lo - first) // step + 1)
            for i in range(i0, i1):
                yield first + step * i

    def overlapping_positions(self, lo, hi, length):
        """Occurrences whose covered span ``[p, p+length)`` intersects ``[lo, hi)``."""
        yield from self.positions(lo - length + 1, hi)


def _ceil_div(a, b):
    """Ceiling division that also works for negative ``b``."""
    return -((-a) // b)


class _PQ:
    """Count-bucketed digram queue with LIFO order within a bucket.

    Mirrors :class:`saxpy.repair._PriorityQueue`: buckets are keyed by frequency,
    each bucket is a dict used as an insertion-ordered stack (push-front, pop-
    from-front).  Frequencies are also kept in a max-heap so dequeue does not
    scan down past empty buckets.
    """

    __slots__ = ("buckets", "heap", "nodes")

    def __init__(self):
        self.buckets = {}
        self.heap = []
        self.nodes = {}

    def _push_front(self, key, freq):
        bucket = self.buckets.get(freq)
        if bucket is None:
            bucket = self.buckets[freq] = {}
            heapq.heappush(self.heap, -freq)
        bucket[key] = None

    def enqueue(self, key, freq):
        if key in self.nodes:
            raise ValueError(f"digram already queued: {key}")
        self.nodes[key] = freq
        self._push_front(key, freq)

    def contains(self, key):
        return key in self.nodes

    def update(self, key, freq):
        if key not in self.nodes:
            return
        old = self.nodes[key]
        if old == freq:
            return
        del self.buckets[old][key]
        if freq < 2:
            del self.nodes[key]
            return
        self.nodes[key] = freq
        self._push_front(key, freq)

    def dequeue(self):
        while self.heap:
            freq = -self.heap[0]
            bucket = self.buckets.get(freq)
            if bucket:
                key, _ = bucket.popitem()
                del self.nodes[key]
                return key
            heapq.heappop(self.heap)
        return None


class _Seg:
    """``count`` adjacent copies of ``sym`` starting at ``start``.

    Node ``j`` starts at ``start + j * length``; the segment's expansion spans
    ``[start, end)``.  Adjacent segments never share a symbol.
    """

    __slots__ = ("sym", "start", "count", "length")

    def __init__(self, sym, start, count, length):
        self.sym = sym
        self.start = start
        self.count = count
        self.length = length

    @property
    def end(self):
        return self.start + self.count * self.length

    def __repr__(self):
        return f"_Seg({self.sym!r}, {self.start}, {self.count}, {self.length})"


@dataclass
class _Rule:
    rid: int
    left: object
    right: object
    length: int
    occ: _Occ = field(default_factory=_Occ)


@dataclass
class RuleView:
    """A rule as seen through :class:`RepairGrammar`.

    ``rule_id`` 0 is R0.  ``length`` is the number of original words the rule
    expands to.  ``occ`` holds occurrence left positions as arithmetic ranges.

    ``expanded_rule_string`` / ``occurrences`` / ``intervals`` materialize the
    rule and therefore raise :class:`ValueError` when the rule is too large for
    the configured materialization limit -- use the grammar's ``rule_length``
    and ``rule_intervals`` queries instead for production archives.
    """

    rule_id: int
    grammar: "RepairGrammar"
    left: object = None
    right: object = None
    length: int = 0
    _occ: _Occ = None

    @property
    def rule_string(self):
        if self.rule_id == 0:
            return self.grammar._r0_rule_string()
        return f"R{self.rule_id}"

    @property
    def expanded_rule_string(self):
        if self.rule_id == 0:
            return self.grammar._r0_expanded_string()
        return " ".join(self.grammar._expand_rule(self.rule_id))

    @property
    def occurrences(self):
        if self._occ is None:
            return []
        if self._occ.count > self.grammar.materialize_limit:
            raise ValueError(
                f"rule R{self.rule_id} has {self._occ.count} occurrences; use "
                "RepairGrammar.rule_occurrences(rule_id, start, end) for a window"
            )
        return list(self._occ.positions())

    @property
    def intervals(self):
        if self.rule_id == 0:
            return [(0, self.length)]
        if self._occ is None:
            return []
        if self._occ.count > self.grammar.materialize_limit:
            raise ValueError(
                f"rule R{self.rule_id} has {self._occ.count} intervals; use "
                "RepairGrammar.rule_intervals(rule_id, start, end) for a window"
            )
        return [(p, p + self.length - 1) for p in self._occ.positions()]

    @property
    def occurrence_count(self):
        return 0 if self._occ is None else self._occ.count


class RepairGrammar:
    """A RePair grammar built from a run-length archive.

    Behaves like the reference ``dict`` of :class:`saxpy.repair.RepairRule`
    (``g[rid]``, ``len(g)``, iteration, ``keys``/``values``/``items``) while
    also offering windowed queries that never materialize the whole string.

    ``materialize_limit`` caps how many tokens/occurrences an accessor such as
    ``expanded_rule_string`` may expand; the windowed methods are always
    streaming and are unaffected.
    """

    def __init__(self, segments, rules, n, materialize_limit=200_000):
        self._segments = segments
        self._seg_ends = [seg.end for seg in segments]
        self._rules = rules
        self.n = n
        self.materialize_limit = materialize_limit
        self._cache = {}

    # -- dict-like surface -------------------------------------------------
    def __getitem__(self, rid):
        if rid == 0:
            if 0 not in self._cache:
                self._cache[0] = RuleView(0, self, length=self.n)
            return self._cache[0]
        if rid not in self._rules:
            raise KeyError(rid)
        if rid not in self._cache:
            rule = self._rules[rid]
            self._cache[rid] = RuleView(rid, self, rule.left, rule.right, rule.length, rule.occ)
        return self._cache[rid]

    def __contains__(self, rid):
        return rid == 0 or rid in self._rules

    def __len__(self):
        return len(self._rules) + 1

    def __iter__(self):
        yield 0
        yield from sorted(self._rules)

    def keys(self):
        return list(self.__iter__())

    def values(self):
        return [self[rid] for rid in self]

    def items(self):
        return [(rid, self[rid]) for rid in self]

    # -- rule queries ------------------------------------------------------
    def rule_length(self, rule_id):
        """Number of original words the rule expands to."""
        if rule_id == 0:
            return self.n
        return self._rules[rule_id].length

    expansion_length = rule_length

    def rule_parts(self, rule_id):
        """The two symbols ``(left, right)`` the rule stands for.

        Rule symbols are returned as their integer ids; terminal words as
        strings.
        """
        rule = self._rules[rule_id]
        return rule.left, rule.right

    def rule_occurrences(self, rule_id, start=0, end=None):
        """Occurrence left positions of ``rule_id`` inside ``[start, end)``."""
        if end is None:
            end = self.n
        if rule_id == 0:
            return [0] if start <= 0 < end else []
        return list(self._rules[rule_id].occ.positions(start, end))

    def rule_intervals(self, rule_id, start=0, end=None):
        """Intervals ``(lo, hi)`` the rule actually covers, overlapping a window.

        ``lo == hi == 0`` for an empty grammar.  ``hi - lo == length - 1``,
        matching :attr:`saxpy.repair.RepairRule.intervals`: ``lo`` is the first
        original word position the rule covers and ``hi`` the last one, so the
        rule spans original positions ``[lo, hi]``.

        Only occurrences whose covered span intersects ``[start, end)`` are
        returned; window edges are inclusive of partly overlapping rules.
        """
        if rule_id == 0:
            return [(0, self.n)] if start < self.n and end > 0 else []
        rule = self._rules[rule_id]
        length = rule.length
        if end is None:
            end = self.n
        return [(p, p + length - 1) for p in rule.occ.overlapping_positions(start, end, length)]

    # -- original-word queries --------------------------------------------
    def word_slice(self, start, end):
        """Return the original words at positions ``[start, end)``.

        The archive is only expanded within the requested window, so a huge run
        costs ``end - start`` words, not the run length.
        """
        if start < 0 or end < start:
            raise ValueError("invalid slice bounds")
        end = min(end, self.n)
        if start >= end:
            return []
        words = []
        segs = self._segments
        # First segment whose end is past ``start``.
        i = bisect.bisect_right(self._seg_ends, start)
        while i < len(segs):
            seg = segs[i]
            if seg.start >= end:
                break
            lo = max(start, seg.start)
            hi = min(end, seg.end)
            first_node = (lo - seg.start) // seg.length
            last_node = (hi - seg.start - 1) // seg.length
            for j in range(first_node, last_node + 1):
                node_start = seg.start + j * seg.length
                a = max(lo, node_start) - node_start
                b = min(hi, node_start + seg.length) - node_start
                words.extend(self._expand_symbol(seg.sym, a, b))
            i += 1
        return words

    def word_at(self, position):
        """The original word at one position."""
        return self.word_slice(position, position + 1)[0]

    # -- expansion helpers -------------------------------------------------
    def _sym_length(self, sym):
        return 1 if isinstance(sym, str) else self._rules[sym].length

    def _expand_symbol(self, sym, start, end):
        if start >= end:
            return []
        if isinstance(sym, str):
            return [sym] * (end - start)
        rule = self._rules[sym]
        out = []
        left_len = self._sym_length(rule.left)
        if start < left_len:
            out.extend(self._expand_symbol(rule.left, start, min(end, left_len)))
        if end > left_len:
            out.extend(self._expand_symbol(rule.right, max(0, start - left_len), end - left_len))
        return out

    def _expand_rule(self, rid):
        rule = self._rules[rid]
        if rule.length > self.materialize_limit:
            raise ValueError(
                f"rule R{rid} expands to {rule.length} words; use rule_length() and "
                "word_slice()/rule_intervals() instead"
            )
        left = [rule.left] if isinstance(rule.left, str) else self._expand_rule(rule.left)
        right = [rule.right] if isinstance(rule.right, str) else self._expand_rule(rule.right)
        return left + right

    def _r0_rule_string(self):
        symbols = sum(seg.count for seg in self._segments)
        if symbols > self.materialize_limit:
            raise ValueError(
                f"R0 has {symbols} symbols; use rule_length()/word_slice() instead"
            )
        out = []
        for seg in self._segments:
            name = seg.sym if isinstance(seg.sym, str) else f"R{seg.sym}"
            out.extend([name] * seg.count)
        return " ".join(out)

    def _r0_expanded_string(self):
        if self.n > self.materialize_limit:
            raise ValueError(
                f"R0 expands to {self.n} words; use word_slice()/rule_length() instead"
            )
        # The reference stores the input string plus its trailing delimiter.
        return " ".join(self.word_slice(0, self.n)) + (" " if self.n else " ")

    def __repr__(self):
        return f"RepairGrammar(n={self.n}, rules={len(self._rules)})"


class _Engine:
    """Segment-level RePair builder matching :mod:`saxpy.repair` exactly."""

    _BULK_THRESHOLD = 8

    def __init__(self, runs):
        self.segments = []
        total = 0
        prev_tok = None
        prev_seg = None
        for item in runs:
            try:
                tok, cnt = item
            except (TypeError, ValueError) as exc:
                raise TypeError("each run must be a (word, count) pair") from exc
            if not isinstance(tok, str):
                raise TypeError("run words must be strings")
            if not isinstance(cnt, int) or isinstance(cnt, bool):
                raise TypeError("run counts must be integers")
            if cnt <= 0:
                raise ValueError("run counts must be positive")
            if prev_tok == tok:
                prev_seg.count += cnt
            else:
                prev_seg = _Seg(tok, total, cnt, 1)
                self.segments.append(prev_seg)
                prev_tok = tok
            total += cnt
        self.n = total
        self.rules = {}
        self.next_rid = 1
        self.table = {}
        self.pq = _PQ()
        self._seed()

    def _occ_for(self, key):
        return _Occ()

    def _get(self, key):
        occ = self.table.get(key)
        if occ is None:
            occ = self.table[key] = _Occ()
        return occ

    def _seed(self):
        prev_tok = None
        for seg in self.segments:
            tok = seg.sym
            if prev_tok is not None:
                self._get((prev_tok, tok)).append(seg.start - 1)
            if seg.count >= 2:
                occ = self._get((tok, tok))
                occ.append_many(seg.start, 1, seg.count - 1)
            prev_tok = tok
        for key, occ in self.table.items():
            if occ.count > 1:
                self.pq.enqueue(key, occ.count)

    # -- segment lookup ----------------------------------------------------
    def _find_seg(self, pos):
        lo, hi = 0, len(self.segments)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.segments[mid].end <= pos:
                lo = mid + 1
            else:
                hi = mid
        seg = self.segments[lo]
        off = pos - seg.start
        if off % seg.length:
            raise AssertionError("position is not a node start")
        return lo, off // seg.length

    def _node_at(self, pos):
        i, idx = self._find_seg(pos)
        seg = self.segments[i]
        return i, idx, seg

    def _prev_node(self, i, idx):
        seg = self.segments[i]
        if idx > 0:
            return seg.sym, seg.start + (idx - 1) * seg.length
        if i > 0:
            prev = self.segments[i - 1]
            return prev.sym, prev.start + (prev.count - 1) * prev.length
        return None

    def _next_node(self, i, idx):
        seg = self.segments[i]
        if idx + 1 < seg.count:
            return seg.sym, seg.start + (idx + 1) * seg.length
        if i + 1 < len(self.segments):
            nxt = self.segments[i + 1]
            return nxt.sym, nxt.start
        return None

    def _merge_around(self, start):
        """Re-join equal adjacent segments left by a splice at ``start``.

        A splice only changes the symbol mix at the replaced block's two
        boundaries, so merging is local; only a same-symbol pair straddling the
        boundary can require a merge, and the merge can cascade rightward.
        """
        if start > 0 and start < len(self.segments):
            a, b = self.segments[start - 1], self.segments[start]
            if a.sym == b.sym and a.length == b.length and a.end == b.start:
                a.count += b.count
                del self.segments[start]
                start -= 1
        while start < len(self.segments) - 1:
            a, b = self.segments[start], self.segments[start + 1]
            if a.sym == b.sym and a.length == b.length and a.end == b.start:
                a.count += b.count
                del self.segments[start + 1]
            else:
                break

    def _splice(self, rid, p, left_len, right_len):
        i, idx, seg = self._node_at(p)
        q = p + left_len
        if idx + 1 < seg.count and seg.start + (idx + 1) * seg.length == q:
            repl = []
            if idx:
                repl.append(_Seg(seg.sym, seg.start, idx, seg.length))
            repl.append(_Seg(rid, p, 1, left_len + right_len))
            tail = seg.count - idx - 2
            if tail:
                repl.append(_Seg(seg.sym, q + right_len, tail, seg.length))
            self.segments[i : i + 1] = repl
            start = i
        else:
            nxt = self.segments[i + 1]
            if idx != seg.count - 1 or nxt.start != q:
                raise AssertionError("inconsistent digram application")
            repl = []
            if seg.count > 1:
                repl.append(_Seg(seg.sym, seg.start, seg.count - 1, seg.length))
            repl.append(_Seg(rid, p, 1, left_len + right_len))
            if nxt.count > 1:
                repl.append(_Seg(nxt.sym, q + right_len, nxt.count - 1, nxt.length))
            self.segments[i : i + 2] = repl
            start = i
        self._merge_around(start)

    # -- digram bookkeeping ------------------------------------------------
    def _apply_single(self, rid, rule, key, p, new_dig, cancelled, record=True):
        i, idx, seg = self._node_at(p)
        left_len = seg.length
        q = p + left_len
        nxt = self._next_node(i, idx)
        if nxt is None or nxt[1] != q:
            raise AssertionError("invalid occurrence")
        right_len = self._node_length(nxt[0])
        if record:
            rule.occ.append(p)

        prev = self._prev_node(i, idx)
        next_next = self._node_global(i, idx + 2)

        if p > 0 and prev is not None:
            old = (prev[0], seg.sym)
            occ = self._get(old)
            occ.remove(prev[1])
            if old == key:
                cancelled.add(prev[1])
            self.pq.update(old, occ.count)
            if occ.count == 0:
                self.table.pop(old, None)
                new_dig.pop(old, None)
            nk = (prev[0], rid)
            self._get(nk).append(prev[1])
            new_dig[nk] = None

        if p < self.n - 2 and next_next is not None:
            old = (nxt[0], next_next[0])
            occ = self._get(old)
            occ.remove(nxt[1])
            if old == key:
                cancelled.add(nxt[1])
            self.pq.update(old, occ.count)
            if occ.count == 0:
                self.table.pop(old, None)
                new_dig.pop(old, None)
            nk = (rid, next_next[0])
            self._get(nk).append(p)
            new_dig[nk] = None

        self._splice(rid, p, left_len, right_len)

    def _node_length(self, sym):
        return 1 if isinstance(sym, str) else self.rules[sym].length

    def _bulk_eligible(self, key, first, step, count):
        if key[0] != key[1] or count < self._BULK_THRESHOLD:
            return False
        try:
            seg_i, idx, seg = self._node_at(first)
        except (AssertionError, IndexError):
            return False
        if seg.sym != key[0] or abs(step) != seg.length:
            return False
        tail = first + step * (count - 1)
        return seg.start <= tail < seg.end

    def _bulk_drop(self, key, pos, entry, new_dig):
        """Remove one occurrence while a bulk range is being consumed."""
        if key == self._entry_key:
            entry.remove(pos)
            return
        occ = self._get(key)
        occ.remove(pos)
        self.pq.update(key, occ.count)
        if occ.count == 0:
            self.table.pop(key, None)
            new_dig.pop(key, None)

    def _bulk_self(self, rid, rule, key, entry, first, step, count, new_dig):
        """Apply a self-pair occurrence range without visiting each position.

        All occurrences sit on one ``X x n`` segment, one node apart.  RePair
        pops them from the tail, so every other one is applied, consuming two
        ``X`` nodes each; ``k = ceil(count / 2)`` rule nodes tile a contiguous
        stretch ``[lo, hi + 2)``.  Only that stretch's two outer edges touch
        foreign digrams, and the interior ``(R, R)`` chain is one arithmetic
        range in processing order.  ``step`` may be positive or negative
        (occurrence ranges are kept in append order).
        """
        processed_first = first + step * (count - 1)
        seg_i, i0, seg = self._node_at(processed_first)
        k = (count + 1) // 2
        direction = -2 if step > 0 else 2
        starts = [i0 + direction * j for j in range(k)]
        lo = min(starts)
        hi = max(starts)
        length = seg.length

        entry.ranges.pop()
        entry.count -= count

        below = self._node_global(seg_i, lo - 1)
        above = self._node_global(seg_i, hi + 2)

        if below is not None:
            self._bulk_drop((below[0], seg.sym), below[1], entry, new_dig)
            nk = (below[0], rid)
            self._get(nk).append(below[1])
            new_dig[nk] = None

        if k > 1:
            rr_key = (rid, rid)
            first_rr = lo if direction > 0 else hi - 2
            self._get(rr_key).append_many(
                seg.start + first_rr * length, direction * length, k - 1
            )
            new_dig[rr_key] = None

        if above is not None:
            self._bulk_drop(
                (seg.sym, above[0]), seg.start + (hi + 1) * length, entry, new_dig
            )
            nk = (rid, above[0])
            self._get(nk).append(seg.start + hi * length)
            new_dig[nk] = None

        rule.occ.append_many(seg.start + i0 * length, direction * length, k)

        prefix = lo
        suffix = seg.count - (hi + 2)
        repl = []
        if prefix:
            repl.append(_Seg(seg.sym, seg.start, prefix, length))
        repl.append(_Seg(rid, seg.start + lo * length, k, 2 * length))
        if suffix:
            repl.append(_Seg(seg.sym, seg.start + (hi + 2) * length, suffix, length))
        self.segments[seg_i : seg_i + 1] = repl
        self._merge_around(seg_i)

    def _node_global(self, seg_i, idx):
        """Node at local index ``idx`` from segment ``seg_i``, or ``None``.

        ``idx`` may be negative (walk into preceding segments) or past the
        segment's end (walk into following segments).
        """
        while seg_i < 0:
            return None
        while seg_i < len(self.segments):
            seg = self.segments[seg_i]
            if idx < 0:
                if seg_i == 0:
                    return None
                seg_i -= 1
                idx += self.segments[seg_i].count
                continue
            if idx < seg.count:
                return seg.sym, seg.start + idx * seg.length
            idx -= seg.count
            seg_i += 1
        return None

    def _process(self, key):
        self._entry_key = key
        entry = self.table[key]
        first = entry.first()
        i, idx, seg = self._node_at(first)
        nxt = self._next_node(i, idx)
        if nxt is None:
            raise AssertionError("queued digram has no second node")
        rid = self.next_rid
        self.next_rid += 1
        rule = _Rule(rid, seg.sym, nxt[0], seg.length + self._node_length(nxt[0]))
        self.rules[rid] = rule

        new_dig = {}
        cancelled = set()

        while entry.count:
            first, step, count = entry.ranges[-1]
            if self._bulk_eligible(key, first, step, count):
                self._bulk_self(rid, rule, key, entry, first, step, count, new_dig)
                continue
            p = entry.pop_last()
            if p in cancelled:
                continue
            self._apply_single(rid, rule, key, p, new_dig, cancelled)

        self.table.pop(key, None)
        for nk in new_dig:
            occ = self.table.get(nk)
            if occ is not None and occ.count > 1:
                if self.pq.contains(nk):
                    self.pq.update(nk, occ.count)
                else:
                    self.pq.enqueue(nk, occ.count)

    def run(self, materialize_limit=200_000):
        while True:
            key = self.pq.dequeue()
            if key is None:
                break
            self._process(key)
        return RepairGrammar(self.segments, self.rules, self.n, materialize_limit)


def rle_to_repair_grammar(runs, materialize_limit=200_000):
    """Build a RePair grammar from a run-length archive.

    Args:
        runs: iterable of ``(word, count)`` pairs, in original time order.
            Adjacent runs with the same word are merged, exactly as the
            expanded token string would be.
        materialize_limit: largest number of tokens/occurrences the
            convenience accessors (``expanded_rule_string``, ``occurrences``,
            ``intervals``, ``R0`` expansion) may expand.  Windowed queries are
            never limited.

    Returns:
        :class:`RepairGrammar`.  For a short archive, expanding the runs and
        calling :func:`saxpy.repair.str_to_repair_grammar` produces the same
        rule ids, expansions and substituted intervals.
    """
    return _Engine(runs).run(materialize_limit=materialize_limit)


#: Alias naming the input encoding explicitly.
str_to_repair_grammar_rle = rle_to_repair_grammar
#: Alias for callers that describe the encoded archive rather than the runs.
encoded_str_to_repair_grammar = rle_to_repair_grammar
#: Alias describing the input by its records.
repair_grammar_from_runs = rle_to_repair_grammar


def rule_expansion_length(grammar, rule_id):
    """Expansion length of one rule (same as ``grammar.rule_length``)."""
    return grammar.rule_length(rule_id)


def rule_intervals_in_window(grammar, rule_id, start, end):
    """Intervals a rule covers that intersect original positions ``[start, end)``."""
    return grammar.rule_intervals(rule_id, start, end)


def rule_occurrences_in_window(grammar, rule_id, start, end):
    """Occurrence left positions of a rule inside original positions ``[start, end)``."""
    return grammar.rule_occurrences(rule_id, start, end)


def words_in_window(grammar, start, end):
    """The original words at positions ``[start, end)``."""
    return grammar.word_slice(start, end)
