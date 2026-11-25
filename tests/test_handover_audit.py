import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import h3  # noqa: E402
import pytest  # noqa: E402

import h3_handover as hh  # noqa: E402


HEX_PARENT = '85283083fffffff'
HEX_NEIGHBOR = '85283093fffffff'


def test_narrative_outer_loss_and_inner_gain_do_not_cancel():
    resolution = 8
    version1 = [
        {'id': 'left-full', 'parent': HEX_PARENT, 'group': 'LEFT'},
        {'id': 'right-full', 'parent': HEX_NEIGHBOR, 'group': 'RIGHT'},
    ]
    version2 = [
        *version1,
        {
            'id': 'cancel-outer-sliver',
            'parent': HEX_PARENT,
            'start': 226,
            'end': 227,
            'cancel': True,
        },
        {
            'id': 'new-inner-strip',
            'parent': HEX_NEIGHBOR,
            'start': 0,
            'end': 49,
            'group': 'LEFT',
        },
    ]

    result = hh.compare(version1, version2, 'LEFT', 'RIGHT', resolution)
    replay = hh.replay_counts(version1, version2, resolution, 'LEFT', 'RIGHT')

    assert result.counts == (
        replay['continuing'],
        replay['new'],
        replay['disappeared'],
    )
    assert result.disappeared == 1
    assert result.new > result.disappeared
    assert result.as_dict()['继续有效的边'] == replay['continuing']

    canceled_edge = next(iter(replay['disappeared_edges']))
    trace = result.trace_edge(*canceled_edge)
    assert trace.status == 'disappeared'
    assert trace.in_v1 is True
    assert trace.in_v2 is False
    assert any(item.row_id == 'cancel-outer-sliver' for item in trace.v2)
    assert all(item.row is not None for item in trace.v1)


def test_same_edge_in_both_versions_is_one_continuing_pair():
    resolution = 7
    rows = [
        {'parent': HEX_PARENT, 'group': 'A'},
        {'parent': HEX_NEIGHBOR, 'group': 'B'},
    ]
    result = hh.compare(
        rows,
        [
            *rows,
            {'id': 'noop', 'parent': HEX_PARENT, 'start': 0, 'end': 1, 'group': 'A'},
        ],
        'A',
        'B',
        resolution,
    )
    replay = hh.replay_counts(rows, rows, resolution, 'A', 'B')

    assert result.counts == (replay['continuing'], 0, 0)
    edge = next(iter(replay['continuing_edges']))
    trace = result.trace_edge(*edge)
    assert trace.status == 'continuing'
    assert trace.in_v1 and trace.in_v2


@pytest.mark.parametrize('seed', range(20))
def test_random_hex_intervals_match_h3_adjacency_replay(seed):
    parents = _nearby_parents(hexagon=True)
    rows1 = _random_rows(seed, parents, resolution=5)
    rows2 = _random_rows(seed + 1000, parents, resolution=5)

    compressed = hh.compare(rows1, rows2, 'A', 'B', 5).counts
    replay = hh.replay_counts(rows1, rows2, 5, 'A', 'B')
    expected = replay['continuing'], replay['new'], replay['disappeared']

    assert compressed == expected


@pytest.mark.parametrize('seed', range(10))
def test_pentagon_child_position_segments_match_replay(seed):
    pentagon = next(cell for cell in h3.get_res0_cells() if h3.is_pentagon(cell))
    parents = list(h3.cell_to_children(pentagon, 3))[:3]
    for parent in list(parents):
        parents.extend(h3.grid_ring(parent, 1))
    parents = list(dict.fromkeys(parents))[:6]

    rows1 = _random_rows(seed + 2000, parents, resolution=6)
    rows2 = _random_rows(seed + 3000, parents, resolution=6)
    compressed = hh.compare(rows1, rows2, 'A', 'B', 6).counts
    replay = hh.replay_counts(rows1, rows2, 6, 'A', 'B')

    assert compressed == (replay['continuing'], replay['new'], replay['disappeared'])


def test_pentagon_special_position_block_decomposes_exactly():
    pentagon = '8009fffffffffff'
    blocks = hh.decompose_interval(pentagon, 3, 0, 55)
    covered = set()
    for block in blocks:
        covered.update(h3.cell_to_children(block, 3))

    expected = {h3.child_pos_to_cell(pentagon, 3, position) for position in range(55)}
    assert covered == expected


def test_bbox_candidates_are_filtered_by_exact_h3_relations():
    parents = [
        HEX_PARENT,
        HEX_NEIGHBOR,
        '85283087fffffff',
        '8528308bfffffff',
    ]
    candidates = hh.bbox_parent_candidates(parents)
    verified = hh.verified_parent_pairs(parents, 8)

    assert candidates
    assert verified
    assert all(h3.are_neighbor_cells(*pair) for pair in verified)
    assert set(verified).issubset(set(candidates))


def test_billion_child_interval_does_not_expand_cells(monkeypatch):
    parent = h3.latlng_to_cell(37.775, -122.418, 4)
    resolution = 15
    assert h3.cell_to_children_size(parent, resolution) > 1_000_000_000

    original_children = h3.cell_to_children
    expansions = []

    def guarded_children(cell, child_resolution):
        parent_resolution = h3.get_resolution(cell)
        if child_resolution != parent_resolution + 1:
            expansions.append((cell, parent_resolution, child_resolution))
        children = original_children(cell, child_resolution)
        if len(children) > 7:
            expansions.append(
                (cell, parent_resolution, child_resolution, len(children))
            )
        return children

    monkeypatch.setattr(hh.h3, 'cell_to_children', guarded_children)

    rows1 = [
        {'id': 'one', 'parent': parent, 'group': 'A'},
        {
            'id': 'long-b',
            'parent': parent,
            'start': 12345,
            'end': 900_000_000,
            'group': 'B',
        },
    ]
    rows2 = [
        {'id': 'two', 'parent': parent, 'group': 'B'},
        {
            'id': 'long-a',
            'parent': parent,
            'start': 50_000,
            'end': 1_200_000_000,
            'group': 'A',
        },
        {
            'id': 'cancel',
            'parent': parent,
            'start': 50_000,
            'end': 50_100,
            'cancel': True,
        },
    ]

    index = hh.HandoverIndex(rows1, rows2, resolution).prepare()
    result = index.query('A', 'B')

    assert result.v1_edges > 0
    assert result.v2_edges > 0
    assert result.unique_pairs == sum(result.counts)
    assert expansions == []


def test_row_order_and_inclusive_end_are_supported():
    parent = HEX_PARENT
    rows = [
        {'parent': parent, 'group': 'A'},
        {'parent': parent, 'start': 0, 'end_inclusive': 6, 'group': 'B'},
        {'parent': parent, 'start': 2, 'end': 4, 'cancel': True},
    ]
    index = hh.VersionIndex(rows, 6)
    positions = {
        position: index.trace(h3.child_pos_to_cell(parent, 6, position)).group
        for position in range(7)
    }

    assert positions == {0: 'B', 1: 'B', 2: None, 3: None, 4: 'B', 5: 'B', 6: 'B'}


def _nearby_parents(*, hexagon: bool) -> list[str]:
    root = next(cell for cell in h3.get_res0_cells() if not h3.is_pentagon(cell))
    parents = h3.cell_to_children(root, 3)[:4]
    for parent in list(parents):
        parents.extend(h3.grid_ring(parent, 1))
    return list(dict.fromkeys(parents))[:6]


def _random_rows(seed: int, parents: list[str], resolution: int) -> list[dict]:
    import random

    rng = random.Random(seed)
    rows = []
    for parent_index, parent in enumerate(parents):
        size = h3.cell_to_children_size(parent, resolution)
        if rng.random() < 0.3:
            rows.append(
                {
                    'id': f'{seed}-full-{parent_index}',
                    'parent': parent,
                    'group': rng.choice(['A', 'B']),
                }
            )
        for segment_index in range(rng.randrange(1, 4)):
            start = rng.randrange(0, max(1, size - 31))
            end = min(size, start + rng.randrange(1, 30))
            row = {
                'id': f'{seed}-{parent_index}-{segment_index}',
                'parent': parent,
                'start': start,
                'end': end,
            }
            if rng.random() < 0.15:
                row['cancel'] = True
            else:
                row['group'] = rng.choice(['A', 'B'])
            rows.append(row)
    return rows
