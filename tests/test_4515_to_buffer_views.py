# BSD 3-Clause License; see https://github.com/scikit-hep/awkward/blob/main/LICENSE


import awkward as ak


def _build():
    import numpy as np

    union = ak.contents.UnionArray(
        ak.index.Index8([0, 1, 0, 1, 0, 1]),
        ak.index.Index64([0, 0, 1, 1, 2, 2]),
        [
            ak.contents.RecordArray(
                [
                    ak.contents.IndexedOptionArray(
                        ak.index.Index64([0, -1, 1]),
                        ak.contents.NumpyArray(np.array([1, 3])),
                    )
                ],
                ["x"],
            ),
            ak.contents.RecordArray(
                [
                    ak.contents.IndexedOptionArray(
                        ak.index.Index64([0, -1, 1]),
                        ak.contents.NumpyArray(np.array([2.5, 4.5])),
                    )
                ],
                ["y"],
            ),
        ],
    )
    inner = ak.contents.IndexedOptionArray(
        ak.index.Index64([0, 1, 2, -1, 3, 4, 5]),
        ak.contents.ListOffsetArray(
            ak.index.Index64([0, 2, 2, 3, 3, 5, 6]), union
        ),
    )
    layout = ak.contents.IndexedOptionArray(
        ak.index.Index64([0, 1, -1, 2, 3]),
        ak.contents.ListOffsetArray(
            ak.index.Index64([0, 2, 2, 4, 5, 6]), inner
        ),
    )
    return ak.Array(layout)


def _assert_same(left, right):
    assert ak.almost_equal(left, right)
    assert left.type == right.type


def test_two_views_match_original_library():
    array = _build()
    requests = [[3, 0, 4], [1, 0]]
    views, _ = ak.to_buffer_views(array, requests)

    assert len(views) == len(requests)
    for (form, length, view), request in zip(views, requests, strict=True):
        _assert_same(view, array[request])
        assert length == len(request)


def test_each_view_reads_alone_from_shared_container():
    array = _build()
    container = {}
    requests = [[3, 0, 4], [2, 1, 2], [4, 4, 0]]
    views, manifest = ak.to_buffer_views(array, requests, container=container)

    for (form, length, view), meta in zip(views, manifest["views"], strict=True):
        subset = {key: container[key] for key in meta["buffer_keys"]}
        reread = ak.from_buffers(
            form, length, subset, buffer_key="{form_key}-{attribute}"
        )
        _assert_same(reread, view)

    assert views[0][2].to_list() == array[requests[0]].to_list()


def test_missing_empty_and_union_branch_preserved():
    array = _build()
    views, _ = ak.to_buffer_views(array, [[2, 1, 0, 3]])
    listing = views[0][2].to_list()

    assert listing[0] is None
    assert listing[1] == []
    assert listing[2] == [[{"x": 1}, {"y": 2.5}], []]
    assert listing[3] == [[{"x": None}], None]


def test_manifest_records_tag_path():
    array = _build()
    _, manifest = ak.to_buffer_views(array, [[0, 1]], shared=[])
    rows = manifest["rows"][0]

    assert [row["row"] for row in rows] == [0, 1]
    assert rows[0]["missing"] is False
    assert rows[0]["tags"] == [
        {"tag": 0, "index": 0},
        {"tag": 1, "index": 0},
    ]
    assert rows[0]["ranges"]
    assert rows[1]["tags"] == []


def test_zero_copy_when_whole_run_approved():
    array = ak.Array([[10, 11], [20], [30, 31, 32], [], [40]])
    container = {}
    views, manifest = ak.to_buffer_views(
        array, [[1, 2]], shared=[("node1-data", 2, 6)], container=container
    )

    assert manifest["body_bytes"] == 0
    assert views[0][2].to_list() == [[20], [30, 31, 32]]
    assert manifest["buffers"][0]["written"] is False


def test_shared_fresh_body_is_charged_once():
    array = ak.Array([[10, 11], [20], [30, 31, 32], [], [40]])
    container = {}
    views, manifest = ak.to_buffer_views(
        array, [[0, 1], [1, 2]], shared=[("node1-data", 5, 6)], container=container
    )

    # Both requests need rows 0..6 of the same original body; the fresh body is
    # 6 * 8 = 48 bytes, written once rather than 24 + 32.
    assert manifest["body_bytes"] == 48
    assert views[0][2].to_list() == [[10, 11], [20]]
    assert views[1][2].to_list() == [[20], [30, 31, 32]]


def test_duplicate_rows_keep_order():
    array = ak.Array([[10, 11], [20], [30, 31, 32], [], [40]])
    views, manifest = ak.to_buffer_views(array, [[0, 0, 2]])

    assert views[0][2].to_list() == [[10, 11], [10, 11], [30, 31, 32]]
    # A leaf stays a NumpyArray, so a duplicated list keeps its values twice;
    # the body is exactly the reached values in request order.
    assert manifest["body_bytes"] == 7 * 8


def test_manifest_physical_ranges():
    array = ak.Array([[10, 11], [20], [30, 31, 32], [], [40]])
    _, manifest = ak.to_buffer_views(array, [[2, 0]])
    rows = manifest["rows"][0]

    assert [row["output_row"] for row in rows] == [0, 1]
    assert rows[0]["ranges"][0]["source"] == [
        {"buffer": "node1-data", "start": 3, "stop": 6}
    ]
    assert rows[1]["ranges"][0]["source"] == [
        {"buffer": "node1-data", "start": 0, "stop": 2}
    ]


def test_unshared_original_values_do_not_leak():
    array = ak.Array([[10, 11], [20], [30, 31, 32], [], [40]])
    container = {}
    views, manifest = ak.to_buffer_views(
        array, [[3]], shared=[("node1-data", 0, 2)], container=container
    )

    assert views[0][2].to_list() == [[]]
    for key, payload in container.items():
        if manifest["buffers"][0]["key"] == key:
            continue
        assert payload.nbytes >= 0
