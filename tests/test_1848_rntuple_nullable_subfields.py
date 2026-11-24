# BSD 3-Clause License; see https://github.com/scikit-hep/uproot/blob/main/LICENSE

from __future__ import annotations

import awkward as ak
import numpy as np
import pytest

import uproot

pytest.importorskip("awkward")


def _make(rows, float_fields=None):
    """
    Builds ``option[var * ?record]`` data that preserves both optional wrappers.

    Awkward drops an option wrapper when no value is missing, so the schema a
    later batch has to match cannot be rebuilt from a plain list of lists.
    """
    float_fields = dict(float_fields or {})
    flat = [d for row in rows if row is not None for d in row if d is not None]
    record = ak.Array(flat) if flat else ak.Array([{}] * 0)
    for name, dtype in float_fields.items():
        if name in record.fields:
            record = ak.with_field(record, ak.values_astype(record[name], dtype), name)

    index = []
    content_i = 0
    for row in rows:
        if row is None:
            continue
        for d in row:
            if d is None:
                index.append(-1)
            else:
                index.append(content_i)
                content_i += 1
    inner = ak.contents.IndexedOptionArray(
        ak.index.Index64(np.array(index, dtype=np.int64)), record.layout
    )
    offsets = [0]
    valid = []
    for row in rows:
        valid.append(row is not None)
        offsets.append(offsets[-1] + (0 if row is None else len(row)))
    lists = ak.contents.ListOffsetArray(
        ak.index.Index64(np.array(offsets, dtype=np.int64)), inner
    )
    outer = ak.contents.IndexedOptionArray(
        ak.index.Index64(np.where(np.array(valid), np.arange(len(rows)), -1)), lists
    )
    return ak.Array(outer)


FIRST = [[], None, [None, {"pt": 2.0, "eta": 3.0}, {"pt": 4.0, "eta": 5.0}]]
SECOND = [
    [],
    [{"pt": 6.0, "eta": 7.0, "qc": 0.0}, {"pt": 8.0, "eta": 9.0, "qc": None}],
    [{"pt": 10.0, "eta": 11.0, "qc": 2.5}],
]
THIRD = [
    [{"pt": 12.0, "eta": 13.0, "qc": 3.5, "calib": 0.25}],
    None,
    [None, {"pt": 14.0, "eta": 15.0, "qc": None, "calib": None}],
]


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "hits.root"
    with uproot.recreate(path) as file:
        file.mkrntuple("n", {"hits": _make(FIRST), "device_qc": np.arange(3)})
    return path


def test_add_nullable_subfield_backfills_only_existing_hits(path):
    with uproot.update(path) as file:
        file["n"].add_fields({"hits.qc": "?float32"})

    with uproot.open(path) as file:
        ntuple = file["n"]
        assert "hits.qc" in ntuple.keys()
        arrays = ntuple.arrays()
        assert arrays["hits"].to_list() == [
            [],
            None,
            [
                None,
                {"pt": 2.0, "eta": 3.0, "qc": None},
                {"pt": 4.0, "eta": 5.0, "qc": None},
            ],
        ]
        assert arrays["hits", "qc"].to_list() == [[], None, [None, None, None]]
        # the same-named field in the device struct is untouched and readable
        assert ntuple["device_qc"].array().tolist() == [0, 1, 2]

    # rc: 0x00 is a real value, so a filled-in zero must stay distinguishable
    # from a never-filled None
    with uproot.update(path) as file:
        file["n"].extend(
            {"hits": _make(SECOND, {"qc": np.float32}), "device_qc": np.arange(3, 6)}
        )

    with uproot.open(path) as file:
        ntuple = file["n"]
        arrays = ntuple.arrays()
        assert arrays["hits", "qc"].to_list() == [
            [],
            None,
            [None, None, None],
            [],
            [0.0, None],
            [2.5],
        ]


def test_nullable_subfield_respects_hit_positions_across_batches(path):
    with uproot.update(path) as file:
        file["n"].add_fields({"hits.qc": "?float32"})
    with uproot.update(path) as file:
        file["n"].extend(
            {"hits": _make(SECOND, {"qc": np.float32}), "device_qc": np.arange(3, 6)}
        )
    with uproot.update(path) as file:
        file["n"].add_fields({"hits.calib": "?float64"})
    with uproot.update(path) as file:
        file["n"].extend(
            {
                "hits": _make(THIRD, {"qc": np.float32, "calib": np.float64}),
                "device_qc": np.arange(6, 9),
            }
        )

    with uproot.open(path) as file:
        ntuple = file["n"]
        expected_qc = [
            [],
            None,
            [None, None, None],
            [],
            [0.0, None],
            [2.5],
            [3.5],
            None,
            [None, None],
        ]
        expected_calib = [
            [],
            None,
            [None, None, None],
            [],
            [None, None],
            [None],
            [0.25],
            None,
            [None, None],
        ]
        assert ntuple["hits.qc"].array().to_list() == expected_qc
        assert ntuple["hits.calib"].array().to_list() == expected_calib
        # the existing fields did not move or change value
        assert ntuple["hits.pt"].array().to_list() == [
            [],
            None,
            [None, 2.0, 4.0],
            [],
            [6.0, 8.0],
            [10.0],
            [12.0],
            None,
            [None, 14.0],
        ]


@pytest.mark.parametrize(
    "entry_start, entry_stop",
    [(0, 3), (0, 6), (3, 6), (2, 5), (0, 9), (6, 9), (5, 6), (2, 9)],
)
def test_partial_reads_align_with_original_hit_order(path, entry_start, entry_stop):
    with uproot.update(path) as file:
        file["n"].add_fields({"hits.qc": "?float32"})
    with uproot.update(path) as file:
        file["n"].extend(
            {"hits": _make(SECOND, {"qc": np.float32}), "device_qc": np.arange(3, 6)}
        )
    with uproot.update(path) as file:
        file["n"].add_fields({"hits.calib": "?float64"})
    with uproot.update(path) as file:
        file["n"].extend(
            {
                "hits": _make(THIRD, {"qc": np.float32, "calib": np.float64}),
                "device_qc": np.arange(6, 9),
            }
        )

    qc = [
        [],
        None,
        [None, None, None],
        [],
        [0.0, None],
        [2.5],
        [3.5],
        None,
        [None, None],
    ]
    calib = [
        [],
        None,
        [None, None, None],
        [],
        [None, None],
        [None],
        [0.25],
        None,
        [None, None],
    ]
    with uproot.open(path) as file:
        arrays = file["n"].arrays(entry_start=entry_start, entry_stop=entry_stop)
        assert arrays["hits", "qc"].to_list() == qc[entry_start:entry_stop]
        assert arrays["hits", "calib"].to_list() == calib[entry_start:entry_stop]


def test_add_nullable_top_level_field(path):
    with uproot.update(path) as file:
        file["n"].add_fields({"qc": "?float32"})

    with uproot.open(path) as file:
        ntuple = file["n"]
        assert ntuple["qc"].array().to_list() == [None, None, None]

    with uproot.update(path) as file:
        file["n"].extend(
            {
                "hits": _make([[], [], []]),
                "device_qc": np.arange(3),
                "qc": ak.values_astype(
                    ak.Array([np.float32(0.0), np.float32(1.5), None]), np.float32
                ),
            }
        )

    with uproot.open(path) as file:
        ntuple = file["n"]
        assert ntuple["qc"].array().to_list() == [None, None, None, 0.0, 1.5, None]
        assert ntuple.arrays(entry_start=3, entry_stop=5)["qc"].to_list() == [0.0, 1.5]


def test_existing_column_pages_stay_in_place(path):
    with uproot.open(path) as file:
        ntuple = file["n"]
        before = [
            [(page.num_elements, page.locator.offset) for page in col.pages]
            for col in ntuple.page_link_list[0]
        ]
        fields_before = [
            (r.field_name, r.parent_field_id) for r in ntuple.ntuple.field_records
        ]

    with uproot.update(path) as file:
        file["n"].add_fields({"hits.qc": "?float32"})

    with uproot.open(path) as file:
        ntuple = file["n"]
        after = [
            [(page.num_elements, page.locator.offset) for page in col.pages]
            for col in ntuple.page_link_list[0]
        ]
        # the original compressed pages are still where they were
        assert after[: len(before)] == before
        assert after == before  # no page was added to the old cluster
        fields_after = [
            (r.field_name, r.parent_field_id) for r in ntuple.ntuple.field_records
        ]
        assert fields_after[: len(fields_before)] == fields_before


def test_nullable_field_requires_simple_content(path):
    with uproot.update(path) as file:
        with pytest.raises(TypeError, match="simple numeric types"):
            file["n"].add_fields({"hits.qc": "?var * int32"})
        with pytest.raises(TypeError, match="simple numeric types"):
            file["n"].add_fields({"hits.qc": "var * int32"})
