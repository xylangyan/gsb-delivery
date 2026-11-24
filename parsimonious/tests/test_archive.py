"""Tests for parsimonious.archive: random-access record queries over
lazily expanded byte volumes."""

import random
from collections import defaultdict

import pytest

from parsimonious import Grammar, ParseError
from parsimonious.archive import (Archive, Concat, Leaf, Repeat, SourceStore,
                                  Volume, bytes_grammar)
from parsimonious.expressions import OneOf

# The meter grammar: a mode marker may be written flush against the first
# reading.  The calibrated branch "MH0" is taken only when a complete
# "H01" can be looked ahead right after "M"; otherwise the plain branch
# consumes just "MH" or "ML".  "01" and "10" are both valid readings;
# "1Z" and "Z" are both valid segment ends.
METER_RULES = '''
record = calibrated / marker / reading / seg_end
calibrated = "M" &"H01" "H0"
marker = "MH" / "ML"
reading = "01" / "10"
seg_end = "1Z" / "Z"
'''

METER_RULES_BYTES = '''
record = calibrated / marker / reading / seg_end
calibrated = b"M" &b"H01" b"H0"
marker = b"MH" / b"ML"
reading = b"01" / b"10"
seg_end = b"1Z" / b"Z"
'''


def meter_store():
    store = SourceStore()
    store.add('mark1', b'MH')
    store.add('mark2', b'ML')
    store.add('corr', b'MH')
    store.add('zeros', b'0')
    store.add('ones', b'1')
    store.add('end', b'Z')
    store.add('bang', b'!')
    return store


def meter_volume(n, store=None, corrected=False):
    """The meter volume: MH, N x "01", Z, ML, N x "01", Z, "!".

    With ``corrected``, the correction note swaps the second segment's
    "L" leaf for an "H" leaf; everything else is shared.
    """
    store = store or meter_store()
    group = Concat([Leaf('zeros'), Leaf('ones')], name='group01')
    rep1 = Repeat(group, n, name='rep1')
    rep2 = Repeat(group, n, name='rep2')
    root = Concat([Leaf('mark1'), rep1, Leaf('end'),
                   Leaf('mark2'), rep2, Leaf('end'),
                   Leaf('bang')])
    volume = Volume(root, store)
    if corrected:
        volume = volume.correct((3,), Leaf('corr'))
    return volume


def stock_records(grammar, data):
    """Sequentially match the default rule over plain bytes."""
    expr = grammar.default_rule
    nodes = []
    pos = 0
    while pos < len(data):
        try:
            node = expr.match(data, pos=pos)
        except ParseError:
            break
        assert node.end > pos
        nodes.append(node)
        pos = node.end
    return nodes, pos


def stock_branch(expr, node):
    if isinstance(expr, OneOf):
        chosen = node.children[0]
        return next(i for i, m in enumerate(expr.members)
                    if m is chosen.expr)
    return None


def stock_fields(node, root):
    out = []

    def walk(n):
        if n.expr.name and n is not root:
            out.append((n.expr.name, n.start, n.end))
        for child in n.children:
            walk(child)

    walk(node)
    return out


def check_against_stock(archive, volume):
    """Every archive record must agree with the plain grammar applied
    sequentially to the expanded bytes."""
    grammar = archive.grammar
    data = volume.expand()
    nodes, tail_pos = stock_records(grammar, data)
    assert len(archive) == len(nodes)
    assert archive.tail == (tail_pos, len(nodes), tail_pos == len(data))
    for index, node in enumerate(nodes):
        record = archive.record(index)
        assert (record.start, record.end) == (node.start, node.end)
        assert record.bytes == node.text
        branch = stock_branch(grammar.default_rule, node)
        assert (record.branch.index if record.branch else None) == branch
        assert ([(f.name, f.start, f.end) for f in record.fields]
                == stock_fields(node, node))
        # match_at from the previous record's end agrees too:
        again = archive.match_at(node.start)
        assert (again.start, again.end) == (node.start, node.end)
        assert (again.branch.index if again.branch else None) == branch
    assert archive.match_at(tail_pos) is None


# ---------------------------------------------------------------------------
# Short volumes: full agreement with the stock sequential grammar


@pytest.mark.parametrize('rules', [METER_RULES, METER_RULES_BYTES])
def test_meter_short_volume_original(rules):
    volume = meter_volume(3)
    archive = Archive(rules, volume)
    expected = [  # (start, end, bytes, branch index)
        (0, 3, b'MH0', 0),
        (3, 5, b'10', 2),
        (5, 7, b'10', 2),
        (7, 9, b'1Z', 3),
        (9, 11, b'ML', 1),
        (11, 13, b'01', 2),
        (13, 15, b'01', 2),
        (15, 17, b'01', 2),
        (17, 18, b'Z', 3),
    ]
    assert len(archive) == len(expected)
    for index, (start, end, data, branch) in enumerate(expected):
        record = archive.record(index)
        assert (record.start, record.end) == (start, end)
        assert record.bytes == data
        assert record.branch.index == branch
    tail = archive.tail
    assert tail.offset == 18 and tail.total == 9 and not tail.exhausted
    assert volume.read(tail.offset, 1) == b'!'
    check_against_stock(archive, volume)


@pytest.mark.parametrize('rules', [METER_RULES, METER_RULES_BYTES])
def test_meter_short_volume_corrected(rules):
    volume = meter_volume(3, corrected=True)
    archive = Archive(rules, volume)
    expected = [
        (0, 3, b'MH0', 0),
        (3, 5, b'10', 2),
        (5, 7, b'10', 2),
        (7, 9, b'1Z', 3),
        (9, 12, b'MH0', 0),
        (12, 14, b'10', 2),
        (14, 16, b'10', 2),
        (16, 18, b'1Z', 3),
    ]
    assert len(archive) == len(expected)
    for index, (start, end, data, branch) in enumerate(expected):
        record = archive.record(index)
        assert (record.start, record.end) == (start, end)
        assert record.bytes == data
        assert record.branch.index == branch
    tail = archive.tail
    assert tail.offset == 18 and tail.total == 8 and not tail.exhausted
    check_against_stock(archive, volume)


def test_meter_short_provenance():
    volume = meter_volume(3)
    archive = Archive(METER_RULES, volume)
    rep1, rep2 = volume.root.children[1], volume.root.children[4]

    # The calibrated record MH0: "MH" from mark1, "0" from rep1's ref 0.
    (field,) = archive.record(0).fields
    assert field.name == 'calibrated'
    assert [(s.source, s.start, s.end) for s in field.segments] == [
        ('mark1', 0, 2), ('zeros', 0, 1)]
    assert field.segments[1].refs == ((rep1, 0),)

    # The reading "10" straddles two references of the group.
    (field,) = archive.record(1).fields
    assert field.name == 'reading'
    assert [(s.source, s.start, s.end) for s in field.segments] == [
        ('ones', 0, 1), ('zeros', 0, 1)]
    assert field.segments[0].refs == ((rep1, 0),)
    assert field.segments[1].refs == ((rep1, 1),)

    # The segment end "1Z": the "1" comes from the group, the "Z" does not.
    (field,) = archive.record(3).fields
    assert field.name == 'seg_end'
    assert [(s.source, s.start, s.end) for s in field.segments] == [
        ('ones', 0, 1), ('end', 0, 1)]
    assert field.segments[0].refs == ((rep1, 2),)
    assert field.segments[1].refs == ()

    # A plain "01" reading inside the second segment.
    (field,) = archive.record(5).fields
    assert [(s.source, s.start, s.end) for s in field.segments] == [
        ('zeros', 0, 1), ('ones', 0, 1)]
    assert field.segments[0].refs == ((rep2, 0),)
    assert field.segments[1].refs == ((rep2, 0),)


def test_named_field_filter():
    volume = meter_volume(3)
    archive = Archive(METER_RULES, volume, fields=['reading'])
    assert [f.name for f in archive.record(0).fields] == []
    assert [f.name for f in archive.record(1).fields] == ['reading']


# ---------------------------------------------------------------------------
# Huge repetitions: direct queries without expansion


def check_meter_huge(n):
    store = meter_store()
    original = Archive(METER_RULES, meter_volume(n, store=store))
    corrected = Archive(METER_RULES,
                        meter_volume(n, store=store, corrected=True))

    # Tail states: the first unconsumed byte is the trailing "!" at 4N+6.
    assert original.tail == (4 * n + 6, 2 * n + 3, False)
    assert corrected.tail == (4 * n + 6, 2 * n + 2, False)
    assert original.volume.read(4 * n + 6, 1) == b'!'
    assert corrected.volume.read(4 * n + 6, 1) == b'!'

    # First-segment records are identical in both volumes.
    for archive in (original, corrected):
        rec0 = archive.record(0)
        assert (rec0.start, rec0.end, rec0.bytes) == (0, 3, b'MH0')
        rec1 = archive.record(1)
        assert (rec1.start, rec1.end, rec1.bytes) == (3, 5, b'10')
        seg1_end = archive.record(n)
        assert (seg1_end.start, seg1_end.end, seg1_end.bytes) == (
            2 * n + 1, 2 * n + 3, b'1Z')

    # Original second segment: plain ML marker, N readings "01", then "Z".
    marker = original.record(n + 1)
    assert (marker.start, marker.end, marker.bytes) == (
        2 * n + 3, 2 * n + 5, b'ML')
    first_reading = original.record(n + 2)
    assert (first_reading.start, first_reading.end,
            first_reading.bytes) == (2 * n + 5, 2 * n + 7, b'01')
    last_reading = original.record(2 * n + 1)
    assert (last_reading.start, last_reading.end,
            last_reading.bytes) == (4 * n + 3, 4 * n + 5, b'01')
    # The segment end is record number 2N+3 (1-based) before correction...
    seg2_end = original.record(2 * n + 2)
    assert (seg2_end.start, seg2_end.end, seg2_end.bytes) == (
        4 * n + 5, 4 * n + 6, b'Z')

    # Corrected second segment: calibrated MH0, N-1 readings "10", "1Z".
    c_marker = corrected.record(n + 1)
    assert (c_marker.start, c_marker.end, c_marker.bytes) == (
        2 * n + 3, 2 * n + 6, b'MH0')
    # ...record number 2N+1 (1-based) is a "10" whose bytes come from the
    # (N-1)th reference's "1" and the Nth reference's "0" (1-based).
    rep2 = corrected.volume.root.children[4]
    crossing = corrected.record(2 * n)
    assert (crossing.start, crossing.end, crossing.bytes) == (
        4 * n + 2, 4 * n + 4, b'10')
    (field,) = crossing.fields
    assert field.name == 'reading'
    assert [(s.source, s.start, s.end) for s in field.segments] == [
        ('ones', 0, 1), ('zeros', 0, 1)]
    assert field.segments[0].refs == ((rep2, n - 2),)
    assert field.segments[1].refs == ((rep2, n - 1),)
    # ...and the segment end moves from 2N+3 to 2N+2 (1-based).
    c_seg2_end = corrected.record(2 * n + 1)
    assert (c_seg2_end.start, c_seg2_end.end, c_seg2_end.bytes) == (
        4 * n + 4, 4 * n + 6, b'1Z')

    # One more spot check deep inside each repeat region.
    mid = original.record(n // 2)
    assert mid.bytes == b'10'
    assert mid.start == 2 * (n // 2) + 1

    # The whole process stays far below the 1e6 attempt budget.
    assert original.attempts < 100000
    assert corrected.attempts < 100000


def test_meter_huge_3e9():
    check_meter_huge(3_000_000_000)


def test_meter_huge_3e10():
    check_meter_huge(30_000_000_000)


def test_correction_reuses_source_data():
    class CountingStore(SourceStore):
        def __init__(self):
            super().__init__()
            self.fetches = defaultdict(int)

        def _fetch(self, name):
            self.fetches[name] += 1
            return super()._fetch(name)

    store = CountingStore()
    for name, data in [('mark1', b'MH'), ('mark2', b'ML'), ('corr', b'MH'),
                       ('zeros', b'0'), ('ones', b'1'), ('end', b'Z'),
                       ('bang', b'!')]:
        store.add(name, data)
    n = 3_000_000_000
    original = Archive(METER_RULES, meter_volume(n, store=store))
    assert len(original) == 2 * n + 3
    corrected = Archive(METER_RULES, meter_volume(n, store=store,
                                                  corrected=True))
    assert len(corrected) == 2 * n + 2
    # Every source -- the unchanged ones included -- was read at most once.
    assert set(store.fetches) == {'mark1', 'mark2', 'corr', 'zeros', 'ones',
                                  'end', 'bang'}
    assert all(count == 1 for count in store.fetches.values())
    # The old volume still answers by its old numbering afterwards.
    assert original.record(2 * n + 2).bytes == b'Z'
    assert original.tail == (4 * n + 6, 2 * n + 3, False)


# ---------------------------------------------------------------------------
# Structural cases


def test_nested_repeats():
    store = SourceStore()
    store.add('ab', b'AB')
    inner = Repeat(Concat([Leaf('ab', 0, 1), Leaf('ab', 1, 1)]), 100,
                   name='inner')
    outer = Repeat(inner, 10 ** 9, name='outer')
    volume = Volume(outer, store)
    archive = Archive('record = pair "Z"?\npair = "AB"', volume)

    total = 100 * 10 ** 9
    assert len(archive) == total
    tail = archive.tail
    assert tail.exhausted
    assert tail.offset == 2 * total
    assert tail.total == total

    index = 55 * 10 ** 9 + 37
    record = archive.record(index)
    assert (record.start, record.end, record.bytes) == (
        2 * index, 2 * index + 2, b'AB')
    (field,) = record.fields
    assert field.name == 'pair'
    q, r = divmod(index, 100)
    assert [(s.source, s.start, s.end) for s in field.segments] == [
        ('ab', 0, 1), ('ab', 1, 2)]
    for segment in field.segments:
        assert segment.refs == ((outer, q), (inner, r))
    assert archive.attempts < 100000


def test_exact_consumption_total():
    store = SourceStore()
    store.add('zero', b'0')
    store.add('one', b'1')
    group = Concat([Leaf('zero'), Leaf('one')])
    volume = Volume(Repeat(group, 3_000_000_000), store)
    archive = Archive('record = "01" / "10"', volume)
    assert len(archive) == 3_000_000_000
    tail = archive.tail
    assert tail.exhausted and tail.offset == 6_000_000_000
    assert tail.total == 3_000_000_000
    assert archive.record(-1).start == 6_000_000_000 - 2


def test_empty_volume():
    volume = Volume(Concat([]), SourceStore())
    archive = Archive('record = "A"', volume)
    assert len(archive) == 0
    assert archive.tail == (0, 0, True)
    with pytest.raises(IndexError):
        archive.record(0)


def test_quantifiers_classes_and_lookaheads():
    rules = '''
        rec = number / word / mark / pair
        number = ~"[0-9]"+
        word = ~"[a-z]"+
        mark = "!" ~"[?]"?
        pair = "A" &"BC" "B" / "AB" !"C"
    '''
    store = SourceStore()
    store.add('num', b'123')
    store.add('w', b'abc')
    store.add('m', b'!?')
    store.add('ab', b'AB')
    store.add('c', b'C')
    volume = Volume(Concat([Leaf('num'), Leaf('w'), Leaf('m'), Leaf('ab'),
                            Leaf('c')]), store)
    archive = Archive(rules, volume)
    assert len(archive) == 4
    assert archive.record(0).bytes == b'123'
    assert archive.record(0).branch.index == 0
    assert archive.record(1).bytes == b'abc'
    assert archive.record(2).bytes == b'!?'
    pair = archive.record(3)
    assert pair.bytes == b'AB'
    assert pair.branch.index == 3
    # The calibrated-looking first alternative of pair won:
    assert pair.decisions[-1].index == 0
    (field,) = pair.fields
    assert field.name == 'pair'
    assert [(s.source, s.start, s.end) for s in field.segments] == [
        ('ab', 0, 2)]
    # The trailing "C" is the first unconsumed byte:
    assert archive.tail == (10, 4, False)
    check_against_stock(archive, volume)


def test_optional_and_star_quantifiers():
    rules = '''
        rec = hdr / item
        hdr = "#" ~"[0-9]"*
        item = ~"[a-z]" ~"[0-9]"?
    '''
    store = SourceStore()
    store.add('a', b'#')
    store.add('b', b'42')
    store.add('c', b'x7')
    store.add('d', b'y')
    volume = Volume(Concat([Leaf('a'), Leaf('b'), Leaf('c'), Leaf('d')]),
                    store)
    archive = Archive(rules, volume)
    assert [r.bytes for r in
            (archive.record(i) for i in range(3))] == [b'#42', b'x7', b'y']
    assert archive.tail == (6, 3, True)
    check_against_stock(archive, volume)


def test_bytes_grammar_roundtrip():
    grammar = bytes_grammar(METER_RULES)
    assert isinstance(grammar, Grammar)
    node = grammar.parse(b'ML')
    assert node.expr.name == 'record'
    # Str and bytes spellings of the same grammar behave identically:
    assert (Archive(METER_RULES, meter_volume(2)).tail
            == Archive(METER_RULES_BYTES, meter_volume(2)).tail)


def test_leaf_ranges_and_files(tmp_path):
    path = tmp_path / 'seg.bin'
    path.write_bytes(b'xxAByy')
    store = SourceStore()
    store.add_file('f', str(path))
    volume = Volume(Concat([Leaf('f', 2, 2), Leaf('f', 2, 2)]), store)
    archive = Archive('record = seg "C"?\nseg = "AB"', volume)
    assert len(archive) == 2
    assert archive.record(0).bytes == b'AB'
    assert archive.record(1).start == 2
    (field,) = archive.record(1).fields
    assert [(s.source, s.start, s.end) for s in field.segments] == [('f', 2, 4)]
    assert archive.tail == (4, 2, True)


# ---------------------------------------------------------------------------
# Fuzz: random small volumes must agree with sequential stock matching

FUZZ_GRAMMARS = [
    'record = "AB" / "BA" / "A" / "B"',
    'record = "A" &"BC" "B" / "AB" / "B" "C"? / "C"',
    'record = pair / solo\npair = "AB" / "BA"\nsolo = "A" / "B" / "C"',
    'record = ~"[AB]" ~"[BC]" / "A" / "B" / "C"',
    'record = "A" !"B" / "AB"{1,3} / "B" / "C"',
    'record = "AA" / "A" "A"? / "B"',
]


def _fuzz_volume(rng, store):
    for i in range(rng.randint(1, 4)):
        store.add('s%d' % i,
                  bytes(rng.choice(b'ABC') for _ in range(rng.randint(1, 3))))
    sources = list(store._providers)

    def group(depth):
        if depth <= 0 or rng.random() < 0.4:
            return Leaf(rng.choice(sources))
        if rng.random() < 0.5:
            return Concat([group(depth - 1)
                           for _ in range(rng.randint(1, 3))])
        count = rng.choice([1, 2, 3, 5, 127, 128, 129, 300])
        return Repeat(group(depth - 1), count)

    children = []
    for _ in range(rng.randint(1, 4)):
        child = group(rng.randint(0, 2))
        children.append(child)
        if rng.random() < 0.3:
            children.append(child)  # reference the same node again
    return Volume(Concat(children), store)


@pytest.mark.parametrize('seed', [0, 7, 42])
def test_fuzz_against_stock(seed):
    rng = random.Random(seed)
    for _ in range(40):
        volume = _fuzz_volume(rng, SourceStore())
        if volume.length > 200000:
            continue
        archive = Archive(rng.choice(FUZZ_GRAMMARS), volume)
        data = volume.expand()
        nodes, tail_pos = stock_records(archive.grammar, data)
        assert len(archive) == len(nodes)
        assert archive.tail == (tail_pos, len(nodes), tail_pos == len(data))
        indices = (range(len(nodes)) if len(nodes) <= 100 else
                   sorted(rng.sample(range(len(nodes)), 100)))
        for index in indices:
            node = nodes[index]
            record = archive.record(index)
            assert (record.start, record.end) == (node.start, node.end)
            assert record.bytes == node.text
            branch = stock_branch(archive.grammar.default_rule, node)
            assert (record.branch.index if record.branch else None) == branch
            assert ([(f.name, f.start, f.end) for f in record.fields]
                    == stock_fields(node, node))
