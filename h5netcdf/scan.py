"""Versioned, paginated scan-angle windows over a radar archive.

Reflectivity is stored once, in acquisition order, and is always addressed by
the physical scan number.  Angle calibrations and quality-control (QC)
receipts arrive in releases which may correct old scans.  This module keeps an
append-only, persistent index next to the archive so that:

* every release is an immutable snapshot;
* a continuation token issued for one release keeps reading that snapshot,
  even after later releases change angles or QC values;
* a release only appends the search paths it modifies, reusing every
  untouched index node and every reflectivity chunk from the previous release;
* a page is located by rank in the snapshot, so resuming does not walk the
  preceding pages.

The on-disk layout lives in a private group::

    /_h5netcdf_scan_index/
        meta       one row: schema version, release/scan counts
        releases   one row per release: window/scan roots, sizes, node range
        nodes      append-only persistent treap nodes

Only the ``h5py`` backend is supported.
"""

import base64
import json
import math
import uuid
from collections.abc import Mapping

import numpy as np

try:
    import h5py
except ImportError:  # pragma: no cover - h5py is required to build the index
    h5py = None

from .utils import CompatibilityError


INDEX_GROUP = "_h5netcdf_scan_index"
META_NAME = "meta"
RELEASES_NAME = "releases"
NODES_NAME = "nodes"

SCHEMA_NAME = "h5netcdf.scan-window"
SCHEMA_VERSION = 1

TOKEN_VERSION = 1
TOKEN_PREFIX = "sw1."

NO_QC = -9999.0
ABSENT = -1

NODE_DTYPE = np.dtype(
    [
        ("angle", "<f8"),
        ("scan", "<i8"),
        ("position", "<i8"),
        ("qc", "<f8"),
        ("present", "u1"),
        ("left", "<i8"),
        ("right", "<i8"),
        ("priority", "<u8"),
        ("count", "<i8"),
    ]
)

RELEASE_DTYPE = np.dtype(
    [
        ("release", "<i8"),
        ("root_window", "<i8"),
        ("root_scan", "<i8"),
        ("count", "<i8"),
        ("scan_count", "<i8"),
        ("node_start", "<i8"),
        ("node_end", "<i8"),
    ]
)

META_DTYPE = np.dtype(
    [
        ("schema_version", "<i8"),
        ("release_count", "<i8"),
        ("scan_count", "<i8"),
    ]
)

_ANGLE_CANDIDATES = (
    "scan_angle",
    "scan_angles",
    "angle",
    "angles",
    "corrected_angle",
    "elevation_angle",
    "elevation",
    "scan_angle_degrees",
    "angle_degrees",
)
_QC_CANDIDATES = (
    "qc",
    "qcs",
    "quality_control",
    "quality_control_value",
    "qc_value",
    "quality",
)
_REFLECTIVITY_CANDIDATES = (
    "reflectivity",
    "reflectivities",
    "reflectivity_dbz",
    "reflectance",
    "dbz",
    "data",
)
_SCAN_CANDIDATES = (
    "scan",
    "scans",
    "scan_number",
    "scan_numbers",
    "scan_id",
    "physical_scan",
)


class ScanWindowError(ValueError):
    """Raised for malformed scan-window requests, tokens or indexes."""


class ScanRecord(tuple):
    """One scan in a page or lookup.

    Behaves like a tuple ``(scan, angle, reflectivity, qc, missing,
    has_receipt, release)`` and also supports attribute and mapping access.
    """

    __slots__ = ()

    _fields = (
        "scan",
        "angle",
        "reflectivity",
        "qc",
        "missing",
        "has_receipt",
        "release",
    )

    def __new__(cls, scan, angle, reflectivity, qc, missing, has_receipt, release):
        return tuple.__new__(
            cls,
            (
                int(scan),
                float(angle),
                reflectivity,
                float(qc),
                bool(missing),
                bool(has_receipt),
                int(release),
            ),
        )

    @property
    def scan(self):
        return self[0]

    @property
    def angle(self):
        return self[1]

    @property
    def reflectivity(self):
        return self[2]

    @property
    def qc(self):
        return self[3]

    @property
    def missing(self):
        return self[4]

    @property
    def has_receipt(self):
        return self[5]

    @property
    def release(self):
        return self[6]

    @property
    def confirmed_missing(self):
        return self.missing and self.has_receipt

    @property
    def status(self):
        if not self.has_receipt:
            return "no_receipt"
        return "confirmed_missing" if self.missing else "present"

    def as_dict(self):
        return dict(zip(self._fields, self))

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in self._fields:
                return getattr(self, key)
            raise KeyError(key)
        return tuple.__getitem__(self, key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __repr__(self):
        return (
            f"ScanRecord(scan={self.scan}, angle={self.angle!r}, "
            f"reflectivity={self.reflectivity!r}, qc={self.qc!r}, "
            f"status={self.status!r}, release={self.release})"
        )


class ScanPage:
    """One page of a scan-angle window."""

    __slots__ = ("records", "next_token", "release", "low", "high", "page_size")

    def __init__(self, records, next_token, release, low, high, page_size):
        self.records = tuple(records)
        self.next_token = next_token
        self.release = int(release)
        self.low = float(low)
        self.high = float(high)
        self.page_size = int(page_size)

    @property
    def has_more(self):
        return self.next_token is not None

    @property
    def token(self):
        return self.next_token

    @property
    def next_cursor(self):
        return self.next_token

    @property
    def cursor(self):
        return self.next_token

    @property
    def items(self):
        return self.records

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)

    def __getitem__(self, item):
        return self.records[item]

    def __bool__(self):
        return bool(self.records)

    def __repr__(self):
        return (
            f"ScanPage(release={self.release}, "
            f"records={len(self.records)}, has_more={self.has_more})"
        )


Page = ScanPage


# ---------------------------------------------------------------------------
# Persistent treap
# ---------------------------------------------------------------------------


class _Row:
    __slots__ = (
        "angle",
        "scan",
        "position",
        "qc",
        "present",
        "left",
        "right",
        "priority",
        "count",
    )

    def __init__(
        self, angle, scan, position, qc, present, left, right, priority, count
    ):
        self.angle = float(angle)
        self.scan = int(scan)
        self.position = int(position)
        self.qc = float(qc)
        self.present = bool(present)
        self.left = int(left)
        self.right = int(right)
        self.priority = int(priority)
        self.count = int(count)

    @classmethod
    def from_void(cls, row):
        return cls(
            row["angle"],
            row["scan"],
            row["position"],
            row["qc"],
            row["present"],
            row["left"],
            row["right"],
            row["priority"],
            row["count"],
        )

    def as_tuple(self):
        return (
            self.angle,
            self.scan,
            self.position,
            self.qc,
            1 if self.present else 0,
            self.left,
            self.right,
            np.uint64(self.priority),
            self.count,
        )


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _splitmix64(value):
    value = (int(value) + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return value ^ (value >> 31)


class _NodeStore:
    """Shared append-only node storage for the two trees of one release."""

    def __init__(self, dataset):
        self._dataset = dataset
        self.base = len(dataset)
        self.rows = []
        self._cache = {}

    def node(self, node):
        node = int(node)
        if node < 0:
            return None
        if node >= self.base:
            return self.rows[node - self.base]
        try:
            return self._cache[node]
        except KeyError:
            row = _Row.from_void(self._dataset[node])
            self._cache[node] = row
            return row

    def alloc(self, angle, scan, position, qc, present, priority=None):
        node = self.base + len(self.rows)
        if priority is None:
            priority = _splitmix64(node)
        row = _Row(angle, scan, position, qc, present, ABSENT, ABSENT, priority, 1)
        self.rows.append(row)
        return node

    def flush(self):
        if not self.rows:
            return
        end = self.base + len(self.rows)
        values = np.zeros((len(self.rows),), dtype=NODE_DTYPE)
        for index, row in enumerate(self.rows):
            values[index] = row.as_tuple()
        self._dataset.resize((end,))
        self._dataset[self.base : end] = values
        self.rows = []
        self.base = end
        self._cache = {}


class _Treap:
    """One persistent treap view over a shared node store."""

    def __init__(self, store, tree):
        self._store = store
        self.tree = tree

    def row(self, node):
        return self._store.node(node)

    def key(self, node_or_row):
        row = (
            self.row(node_or_row)
            if isinstance(node_or_row, (int, np.integer))
            else node_or_row
        )
        if self.tree == "window":
            return (row.angle, row.scan)
        return row.scan

    def _bound(self, value):
        if self.tree == "window":
            return (float(value), -math.inf)
        return int(value)

    def left(self, node):
        row = self.row(node)
        return ABSENT if row is None else row.left

    def right(self, node):
        row = self.row(node)
        return ABSENT if row is None else row.right

    def count(self, node):
        row = self.row(node)
        return 0 if row is None else row.count

    def priority(self, node):
        return self.row(node).priority

    def _link(self, node, left, right):
        row = self.row(node)
        row.left = int(left)
        row.right = int(right)
        row.count = 1 + self.count(left) + self.count(right)

    def alloc(self, angle, scan, position, qc, present, priority=None):
        return self._store.alloc(
            angle, scan, position, qc, present, priority=priority
        )

    def copy_node(self, node, left=None, right=None):
        row = self.row(node)
        new = self.alloc(
            row.angle,
            row.scan,
            row.position,
            row.qc,
            row.present,
            priority=row.priority,
        )
        self._link(
            new,
            row.left if left is None else left,
            row.right if right is None else right,
        )
        return new

    def replace_node(self, node, angle, scan, position, qc, present):
        row = self.row(node)
        new = self.alloc(
            angle, scan, position, qc, present, priority=row.priority
        )
        self._link(new, row.left, row.right)
        return new

    def lower_bound(self, root, key):
        node = int(root)
        rank = 0
        while node >= 0:
            row = self.row(node)
            if self.key(row) < key:
                rank += self.count(row.left) + 1
                node = row.right
            else:
                node = row.left
        return rank

    def find(self, root, key):
        node = int(root)
        while node >= 0:
            row = self.row(node)
            row_key = self.key(row)
            if row_key == key:
                return node
            node = row.left if key < row_key else row.right
        return ABSENT

    def select(self, root, start, stop):
        if start >= stop:
            return []
        result = []

        def walk(current, base):
            if current < 0 or len(result) >= stop - start:
                return
            row = self.row(current)
            left_count = self.count(row.left)
            pivot = base + left_count
            if start < pivot:
                walk(row.left, base)
            if start <= pivot < stop:
                result.append(current)
            if pivot + 1 < stop:
                walk(row.right, pivot + 1)

        walk(int(root), 0)
        return result

    def split(self, root, key):
        if root < 0:
            return ABSENT, ABSENT
        row = self.row(root)
        if self.key(row) < key:
            left, right = self.split(row.right, key)
            return self.copy_node(root, right=left), right
        left, right = self.split(row.left, key)
        return left, self.copy_node(root, left=right)

    def merge(self, left, right):
        if left < 0:
            return right
        if right < 0:
            return left
        if self.priority(left) > self.priority(right):
            row = self.row(left)
            return self.copy_node(left, right=self.merge(row.right, right))
        row = self.row(right)
        return self.copy_node(right, left=self.merge(left, row.left))

    def insert(self, root, new_node):
        if root < 0:
            return new_node
        row = self.row(root)
        new_key = self.key(new_node)
        root_key = self.key(row)
        if self.priority(new_node) > self.priority(root):
            left, right = self.split(root, new_key)
            self._link(new_node, left, right)
            return new_node
        if new_key < root_key:
            return self.copy_node(root, left=self.insert(row.left, new_node))
        if new_key > root_key:
            return self.copy_node(root, right=self.insert(row.right, new_node))
        self._link(new_node, row.left, row.right)
        return new_node

    def delete(self, root, key):
        if root < 0:
            return ABSENT
        row = self.row(root)
        root_key = self.key(row)
        if key < root_key:
            return self.copy_node(root, left=self.delete(row.left, key))
        if key > root_key:
            return self.copy_node(root, right=self.delete(row.right, key))
        return self.merge(row.left, row.right)

    def replace(self, root, key, new_node):
        """Replace the node with ``key``, path-copying only the search path."""
        row = self.row(root)
        root_key = self.key(row)
        if key < root_key:
            return self.copy_node(root, left=self.replace(row.left, key, new_node))
        if key > root_key:
            return self.copy_node(root, right=self.replace(row.right, key, new_node))
        self._link(new_node, row.left, row.right)
        return new_node

    def bulk_build(self, entries):
        """Build a treap from entries sorted by this tree's key."""
        if not entries:
            return ABSENT
        ids = []
        for entry in entries:
            angle, scan, position, qc, present = entry
            ids.append(self.alloc(angle, scan, position, qc, present))
        size = len(ids)
        left = [ABSENT] * size
        right = [ABSENT] * size
        stack = []
        for index in range(size):
            node = ids[index]
            last = -1
            while stack and self.priority(ids[stack[-1]]) < self.priority(node):
                last = stack.pop()
            if stack:
                right[stack[-1]] = index
            left[index] = last
            stack.append(index)
        root_index = stack[0]
        counts = [1] * size
        order = []
        pending = [root_index]
        while pending:
            index = pending.pop()
            order.append(index)
            if right[index] >= 0:
                pending.append(right[index])
            if left[index] >= 0:
                pending.append(left[index])
        for index in reversed(order):
            total = 1
            if left[index] >= 0:
                total += counts[left[index]]
            if right[index] >= 0:
                total += counts[right[index]]
            counts[index] = total
        for index in range(size):
            row = self.row(ids[index])
            row.left = ids[left[index]] if left[index] >= 0 else ABSENT
            row.right = ids[right[index]] if right[index] >= 0 else ABSENT
            row.count = counts[index]
        return ids[root_index]


# ---------------------------------------------------------------------------
# Index datasets
# ---------------------------------------------------------------------------


class _Index:
    def __init__(self, h5file, group_path, writable):
        self._h5file = h5file
        self._group_path = group_path
        self._writable = writable
        self._group = None
        self._meta = None
        self._releases = None
        self._nodes = None

    @property
    def exists(self):
        return self._group_path in self._h5file

    def create(self):
        if self.exists:
            raise ScanWindowError(f"scan index already exists at {self._group_path!r}")
        group = self._h5file.create_group(self._group_path)
        meta = np.zeros(1, dtype=META_DTYPE)
        meta["schema_version"] = SCHEMA_VERSION
        self._group = group
        self._meta = group.create_dataset(META_NAME, data=meta)
        self._releases = group.create_dataset(
            RELEASES_NAME,
            shape=(0,),
            maxshape=(None,),
            dtype=RELEASE_DTYPE,
            chunks=(64,),
        )
        self._nodes = group.create_dataset(
            NODES_NAME,
            shape=(0,),
            maxshape=(None,),
            dtype=NODE_DTYPE,
            chunks=(256,),
            compression="gzip",
            compression_opts=4,
            shuffle=True,
        )
        group.attrs["schema"] = SCHEMA_NAME
        group.attrs["schema_version"] = SCHEMA_VERSION
        group.attrs["index_id"] = uuid.uuid4().hex
        return self

    @property
    def index_id(self):
        return _decode(self._group.attrs.get("index_id", ""))

    def open(self):
        if not self.exists:
            raise ScanWindowError(
                f"no scan index found at {self._group_path!r}; "
                "call create_scan_index() first"
            )
        group = self._h5file[self._group_path]
        schema = _decode(group.attrs.get("schema", ""))
        if schema != SCHEMA_NAME:
            raise ScanWindowError(f"{self._group_path!r} is not a scan-window index")
        self._group = group
        self._meta = group[META_NAME]
        self._releases = group[RELEASES_NAME]
        self._nodes = group[NODES_NAME]
        version = int(self._meta[0]["schema_version"])
        if version != SCHEMA_VERSION:
            raise ScanWindowError(
                f"unsupported scan index schema version {version}, "
                f"expected {SCHEMA_VERSION}"
            )
        return self

    def require_writable(self):
        if not self._writable:
            raise ScanWindowError("scan index is opened read-only")

    @property
    def meta(self):
        return self._meta[0]

    @property
    def release_count(self):
        return int(self.meta["release_count"])

    @property
    def scan_count(self):
        return int(self.meta["scan_count"])

    def set_meta(self, release_count, scan_count):
        meta = np.zeros(1, dtype=META_DTYPE)
        meta["schema_version"] = SCHEMA_VERSION
        meta["release_count"] = int(release_count)
        meta["scan_count"] = int(scan_count)
        self._meta[0] = meta

    def release(self, number):
        number = int(number)
        if number < 0:
            number += self.release_count
        if number < 0 or number >= self.release_count:
            raise ScanWindowError(f"release {number} does not exist")
        row = self._releases[number]
        if int(row["release"]) != number:
            raise ScanWindowError(f"release {number} is not stored contiguously")
        return row

    def append_release(
        self, root_window, root_scan, count, scan_count, node_start, node_end
    ):
        self.require_writable()
        number = self.release_count
        row = np.zeros(1, dtype=RELEASE_DTYPE)
        row["release"] = number
        row["root_window"] = int(root_window)
        row["root_scan"] = int(root_scan)
        row["count"] = int(count)
        row["scan_count"] = int(scan_count)
        row["node_start"] = int(node_start)
        row["node_end"] = int(node_end)
        start = len(self._releases)
        self._releases.resize((start + 1,))
        self._releases[start : start + 1] = row
        self.set_meta(number + 1, scan_count)
        return number

    def store(self):
        return _NodeStore(self._nodes)


# ---------------------------------------------------------------------------
# Dataset resolution
# ---------------------------------------------------------------------------


def _basename(path):
    return path.rstrip("/").split("/")[-1]


def _is_1d_dataset(obj):
    return h5py is not None and isinstance(obj, h5py.Dataset) and obj.ndim == 1


def _resolve_dataset(file, value, role, candidates):
    if value is None:
        found = []
        for path, obj in file._h5file.items():
            name = _basename(path)
            if name in candidates and _is_1d_dataset(obj):
                found.append((candidates.index(name), path))

        def visit(path, obj):
            name = _basename(path)
            if name in candidates and _is_1d_dataset(obj):
                found.append((candidates.index(name), path))

        file._h5file.visititems(visit)
        if not found:
            raise ScanWindowError(
                f"cannot find {role} variable; pass it explicitly "
                f"(looked for {', '.join(candidates)})"
            )
        found = sorted(set(found))
        value = found[0][1]
    if h5py is not None and isinstance(value, h5py.Dataset):
        dataset = value
    elif hasattr(value, "_h5ds"):
        dataset = value._h5ds
    elif isinstance(value, str):
        path = value if value.startswith("/") else "/" + value
        if path not in file._h5file:
            raise ScanWindowError(f"{role} dataset {value!r} does not exist")
        dataset = file._h5file[path]
    else:
        raise ScanWindowError(f"unsupported {role} specification {value!r}")
    if not _is_1d_dataset(dataset):
        raise ScanWindowError(
            f"{role} dataset {dataset.name!r} is not one-dimensional"
        )
    return dataset


def _find_dataset(file, candidates):
    try:
        return _resolve_dataset(file, None, candidates[0], candidates)
    except ScanWindowError:
        return None


def _dataset_fillvalue(dataset, default=NO_QC):
    value = dataset.attrs.get("_FillValue", dataset.attrs.get("fill_value", None))
    if value is None:
        return default
    try:
        return float(np.asarray(value).reshape(-1)[0])
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Public index object
# ---------------------------------------------------------------------------


class ScanWindowIndex:
    """Persistent scan index attached to one h5netcdf file."""

    def __init__(self, file, index_group=None, writable=None):
        _check_backend(file)
        self._file = file
        self._group_path = index_group or INDEX_GROUP
        if writable is None:
            writable = bool(getattr(file, "_writable", False))
        self._index = _Index(file._h5file, self._group_path, writable).open()
        group = self._index._group
        self._reflectivity_path = _decode(group.attrs["reflectivity_path"])
        self._qc_fillvalue = float(group.attrs.get("qc_fillvalue", NO_QC))
        self._index_id = self._index.index_id

    @property
    def file(self):
        return self._file

    @property
    def reflectivity_path(self):
        return self._reflectivity_path

    @property
    def qc_fillvalue(self):
        return self._qc_fillvalue

    @property
    def release_count(self):
        return self._index.release_count

    @property
    def latest_release(self):
        return self._index.release_count - 1

    @property
    def scan_count(self):
        return self._index.scan_count

    def _reflectivity(self):
        return self._file._h5file[self._reflectivity_path]

    def _read_reflectivity(self, positions):
        dataset = self._reflectivity()
        positions = [int(p) for p in positions]
        values = {}
        ordered = sorted(set(positions))
        start = 0
        while start < len(ordered):
            stop = start + 1
            while stop < len(ordered) and ordered[stop] == ordered[stop - 1] + 1:
                stop += 1
            block = dataset[ordered[start] : ordered[stop - 1] + 1]
            for offset, position in enumerate(ordered[start:stop]):
                item = block[offset]
                if hasattr(item, "item"):
                    item = item.item()
                values[position] = item
            start = stop
        return values

    def _record(self, row, release, reflectivity):
        present = bool(row.present)
        qc = float(row.qc)
        missing = present and (
            qc == self._qc_fillvalue or not math.isfinite(qc)
        )
        return ScanRecord(
            row.scan,
            row.angle,
            reflectivity,
            qc,
            missing,
            present,
            release,
        )

    # -- writing ---------------------------------------------------------

    def publish(
        self,
        scans=None,
        angles=None,
        qcs=None,
        *,
        present=None,
        positions=None,
        records=None,
        angle_calibrations=None,
        qc_receipts=None,
        release=None,
    ):
        """Publish a release containing corrections for existing scans.

        ``scans`` are physical scan numbers.  ``angles`` are corrected angles
        in degrees.  ``qcs`` are QC receipt values; ``-9999`` marks a
        confirmed missing receipt.  A scan omitted from ``qcs`` keeps its
        previous receipt.  New scans may be added this way too.

        ``records`` may replace the three parallel sequences with an iterable
        of mappings or tuples ``(scan, angle, qc)``.  ``angle_calibrations``
        and ``qc_receipts`` may be mappings from scan number to value; they
        are merged into the same release.  ``release`` is accepted for
        symmetry with the read side; releases are numbered sequentially.
        """
        self._index.require_writable()
        if records is not None:
            if scans is not None:
                raise ScanWindowError("pass either scans or records, not both")
            scans, angles, qcs, present, positions = _records_to_columns(
                records, present=present, positions=positions
            )
        if angle_calibrations is not None or qc_receipts is not None:
            scans, angles, qcs, present, positions = _merge_updates(
                scans,
                angles,
                qcs,
                present,
                positions,
                angle_calibrations,
                qc_receipts,
            )
        if scans is None:
            scans = []
        scans = np.asarray(list(scans), dtype=np.int64).reshape(-1)
        count = len(scans)
        angles = _broadcast(angles, count, "angles")
        qcs = _broadcast(qcs, count, "qcs")
        present = _broadcast(present, count, "present")
        positions = _broadcast(positions, count, "positions")
        if len(set(int(s) for s in scans)) != count:
            raise ScanWindowError("duplicate scan numbers in one release")

        previous = self._index.release(self.latest_release)
        store = self._index.store()
        scan_tree = _Treap(store, "scan")
        window_tree = _Treap(store, "window")
        root_scan = int(previous["root_scan"])
        root_window = int(previous["root_window"])
        window_count = int(previous["count"])
        scan_count = int(previous["scan_count"])
        node_start = store.base

        for i, scan_value in enumerate(scans):
            scan = int(scan_value)
            if scan < 0:
                raise ScanWindowError(f"negative scan number {scan}")
            old_id = scan_tree.find(root_scan, scan)
            old = scan_tree.row(old_id) if old_id >= 0 else None

            angle = angles[i]
            if angle is None:
                if old is None:
                    raise ScanWindowError(
                        f"scan {scan} has no stored angle; provide one"
                    )
                angle = old.angle
            angle = float(angle)
            if not math.isfinite(angle):
                raise ScanWindowError(f"scan {scan} has non-finite angle {angle!r}")

            qc = qcs[i]
            if qc is None:
                qc = old.qc if old is not None else self._qc_fillvalue
            else:
                qc = float(qc)

            explicit_present = present[i]
            if explicit_present is None:
                if qcs[i] is not None:
                    has_receipt = True
                elif old is not None:
                    has_receipt = old.present
                else:
                    has_receipt = False
            else:
                has_receipt = bool(explicit_present)

            position = positions[i]
            if position is None:
                position = old.position if old is not None else scan
            position = int(position)
            if position < 0:
                raise ScanWindowError(
                    f"negative reflectivity position {position} for scan {scan}"
                )
            if position >= len(self._reflectivity()):
                raise ScanWindowError(
                    f"scan {scan} points past the reflectivity column "
                    f"(position {position})"
                )

            if old is None:
                new_scan = scan_tree.alloc(
                    angle, scan, position, qc, has_receipt
                )
                root_scan = scan_tree.insert(root_scan, new_scan)
                scan_count = max(scan_count, scan + 1)
            else:
                new_scan = scan_tree.replace_node(
                    old_id, angle, scan, position, qc, has_receipt
                )
                root_scan = scan_tree.replace(root_scan, scan, new_scan)

            old_window_key = (
                (old.angle, scan) if old is not None and old.present else None
            )
            new_window_key = (angle, scan)
            if old_window_key is not None:
                if old_window_key == new_window_key and has_receipt:
                    old_window_id = window_tree.find(root_window, old_window_key)
                    new_window = window_tree.replace_node(
                        old_window_id,
                        angle,
                        scan,
                        position,
                        qc,
                        True,
                    )
                    root_window = window_tree.replace(
                        root_window, new_window_key, new_window
                    )
                else:
                    root_window = window_tree.delete(root_window, old_window_key)
                    window_count -= 1
                    if has_receipt:
                        new_window = window_tree.alloc(
                            angle, scan, position, qc, True
                        )
                        root_window = window_tree.insert(root_window, new_window)
                        window_count += 1
            elif has_receipt:
                new_window = window_tree.alloc(angle, scan, position, qc, True)
                root_window = window_tree.insert(root_window, new_window)
                window_count += 1

        store.flush()
        node_end = store.base
        release = self._index.append_release(
            root_window,
            root_scan,
            window_count,
            scan_count,
            node_start,
            node_end,
        )
        self._file.flush()
        return release

    # -- reading ---------------------------------------------------------

    def window(
        self,
        low=None,
        high=None,
        page_size=None,
        release=None,
        token=None,
        *,
        min_angle=None,
        max_angle=None,
    ):
        if min_angle is not None:
            low = min_angle
        if max_angle is not None:
            high = max_angle
        return ScanWindow(
            self,
            low=low,
            high=high,
            page_size=page_size,
            release=release,
            token=token,
        )

    def read_scan(self, scan, release=None):
        release_number = self._resolve_release(release)
        row = self._index.release(release_number)
        tree = _Treap(self._index.store(), "scan")
        node = tree.find(int(row["root_scan"]), int(scan))
        if node < 0:
            raise KeyError(f"scan {scan} is not in release {release_number}")
        window_row = tree.row(node)
        reflectivity = self._read_reflectivity([window_row.position])[
            window_row.position
        ]
        return self._record(window_row, release_number, reflectivity)

    def read_reflectivity(self, scan, release=None):
        return self.read_scan(scan, release=release).reflectivity

    def reflectivity(self, scan, release=None):
        return self.read_reflectivity(scan, release=release)

    def stats(self, release=None):
        number = self._resolve_release(release)
        row = self._index.release(number)
        return {
            "release": number,
            "scans": int(row["scan_count"]),
            "entries": int(row["count"]),
            "node_start": int(row["node_start"]),
            "node_end": int(row["node_end"]),
            "new_nodes": int(row["node_end"]) - int(row["node_start"]),
            "total_nodes": int(row["node_end"]),
        }

    def _resolve_release(self, release):
        if release is None:
            release = self.latest_release
        release = int(release)
        if release < 0:
            release += self.release_count
        if release < 0 or release >= self.release_count:
            raise ScanWindowError(f"release {release} does not exist")
        return release


class ScanWindow:
    """A paged, left-closed/right-open angle window on one release snapshot."""

    def __init__(
        self,
        index,
        low=None,
        high=None,
        page_size=None,
        release=None,
        token=None,
    ):
        self._index = index
        if token is not None:
            payload = _decode_token(token)
            if payload.get("index_id") not in (None, "", index._index_id):
                raise ScanWindowError(
                    "continuation token belongs to a rebuilt scan index"
                )
            if low is not None and not _same_angle(low, payload["low"]):
                raise ScanWindowError("token belongs to a different window")
            if high is not None and not _same_angle(high, payload["high"]):
                raise ScanWindowError("token belongs to a different window")
            release_number = payload["release"]
            low = payload["low"]
            high = payload["high"]
            if page_size is None:
                page_size = payload.get("page_size", 1)
            self._cursor = payload["next"]
            self._end = payload["end"]
        else:
            if low is None or high is None:
                raise ScanWindowError(
                    "a window needs low and high bounds (or a continuation token)"
                )
            release_number = index._resolve_release(release)
            low = float(low)
            high = float(high)
            if page_size is None:
                page_size = 1
            row = index._index.release(release_number)
            tree = _Treap(index._index.store(), "window")
            self._cursor = tree.lower_bound(
                int(row["root_window"]), tree._bound(low)
            )
            self._end = tree.lower_bound(int(row["root_window"]), tree._bound(high))
        if int(page_size) <= 0:
            raise ScanWindowError("page_size must be positive")
        self.release = int(release_number)
        self.low = float(low)
        self.high = float(high)
        self.page_size = int(page_size)
        self._row = index._index.release(self.release)
        self._root = int(self._row["root_window"])

    @property
    def next_token(self):
        if self._cursor >= self._end:
            return None
        return _encode_token(
            {
                "v": TOKEN_VERSION,
                "index_id": self._index._index_id,
                "release": self.release,
                "low": self.low,
                "high": self.high,
                "next": self._cursor,
                "end": self._end,
                "page_size": self.page_size,
            }
        )

    def page(self, token=None):
        if token is not None:
            payload = _decode_token(token)
            if payload.get("index_id") not in (None, "", self._index._index_id):
                raise ScanWindowError(
                    "continuation token belongs to a rebuilt scan index"
                )
            if payload["release"] != self.release:
                raise ScanWindowError("token belongs to a different release")
            if not _same_angle(payload["low"], self.low) or not _same_angle(
                payload["high"], self.high
            ):
                raise ScanWindowError("token belongs to a different window")
            self._cursor = payload["next"]
            self._end = payload["end"]
            if "page_size" in payload:
                self.page_size = int(payload["page_size"])
        start = self._cursor
        stop = min(start + self.page_size, self._end)
        tree = _Treap(self._index._index.store(), "window")
        nodes = tree.select(self._root, start, stop) if start < stop else []
        rows = [tree.row(node) for node in nodes]
        values = self._index._read_reflectivity(
            [row.position for row in rows]
        )
        records = [
            self._index._record(row, self.release, values[row.position])
            for row in rows
        ]
        self._cursor = stop
        return ScanPage(
            records,
            self.next_token,
            self.release,
            self.low,
            self.high,
            self.page_size,
        )

    def next_page(self):
        return self.page()

    def read_page(self):
        return self.page()

    def fetch(self):
        return self.page()

    def iter_records(self):
        """Yield every record of the window, paging through it."""
        while self._cursor < self._end:
            page = self.page()
            yield from page.records

    def records(self):
        """Return every record of the window as a tuple."""
        return tuple(self.iter_records())

    def pages(self):
        while self._cursor < self._end:
            page = self.page()
            yield page

    def __iter__(self):
        return self.pages()

    def __repr__(self):
        return (
            f"ScanWindow(release={self.release}, low={self.low}, "
            f"high={self.high}, page_size={self.page_size})"
        )


# ---------------------------------------------------------------------------
# Building and opening
# ---------------------------------------------------------------------------


def _merge_updates(
    scans,
    angles,
    qcs,
    present,
    positions,
    angle_calibrations,
    qc_receipts,
):
    """Merge angle and QC-only mappings into one set of parallel columns."""
    angle_calibrations = dict(angle_calibrations or {})
    qc_receipts = dict(qc_receipts or {})
    angle_by_scan = {int(k): v for k, v in angle_calibrations.items()}
    qc_by_scan = {int(k): v for k, v in qc_receipts.items()}

    if scans is None:
        scan_list = []
    else:
        scan_list = [int(s) for s in scans]
    known = set(scan_list)
    for scan in list(angle_by_scan) + list(qc_by_scan):
        if scan not in known:
            scan_list.append(scan)
            known.add(scan)

    angle_values = _broadcast(angles, len(scan_list), "angles")
    qc_values = _broadcast(qcs, len(scan_list), "qcs")
    present_values = _broadcast(present, len(scan_list), "present")
    position_values = _broadcast(positions, len(scan_list), "positions")
    angle_out = list(angle_values)
    qc_out = list(qc_values)
    present_out = list(present_values)
    position_out = list(position_values)
    for index, scan in enumerate(scan_list):
        if scan in angle_by_scan:
            angle_out[index] = angle_by_scan[scan]
        if scan in qc_by_scan:
            qc_out[index] = qc_by_scan[scan]
            present_out[index] = True
    return scan_list, angle_out, qc_out, present_out, position_out


def _records_to_columns(records, present=None, positions=None):
    scans, angles, qcs, presents, positions_out = [], [], [], [], []
    for entry in records:
        if isinstance(entry, Mapping):
            scans.append(entry.get("scan", entry.get("scan_number")))
            angles.append(entry.get("angle", entry.get("scan_angle")))
            qcs.append(entry.get("qc", entry.get("quality_control")))
            presents.append(entry.get("present", entry.get("has_receipt")))
            positions_out.append(entry.get("position", entry.get("reflectivity_index")))
        else:
            values = list(entry)
            if len(values) < 3:
                raise ScanWindowError(
                    "record tuples need at least (scan, angle, qc)"
                )
            scans.append(values[0])
            angles.append(values[1])
            qcs.append(values[2])
            presents.append(values[3] if len(values) > 3 else None)
            positions_out.append(values[4] if len(values) > 4 else None)
    if present is not None:
        presents = list(present)
    if positions is not None:
        positions_out = list(positions)
    return scans, angles, qcs, presents, positions_out


def _broadcast(value, count, name):
    if value is None:
        return [None] * count
    if isinstance(value, np.ndarray) and value.ndim == 0:
        return [value.item()] * count
    if isinstance(value, (str, bytes)) or np.isscalar(value):
        return [value] * count
    values = list(value)
    if len(values) != count:
        raise ScanWindowError(
            f"{name} must be a scalar or have length {count}, got {len(values)}"
        )
    return values


def _same_angle(left, right):
    return float(left) == float(right)


def _encode_token(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    return TOKEN_PREFIX + encoded.decode("ascii")


def _decode_token(token):
    if isinstance(token, bytes):
        token = token.decode("ascii")
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        raise ScanWindowError("invalid continuation token")
    body = token[len(TOKEN_PREFIX) :]
    padding = "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + padding))
    except Exception as exc:  # noqa: BLE001 - normalize all decode failures
        raise ScanWindowError("invalid continuation token") from exc
    if payload.get("v", TOKEN_VERSION) != TOKEN_VERSION:
        raise ScanWindowError("unsupported continuation token version")
    for key in ("release", "low", "high", "next", "end"):
        if key not in payload:
            raise ScanWindowError("invalid continuation token")
    payload["release"] = int(payload["release"])
    payload["next"] = int(payload["next"])
    payload["end"] = int(payload["end"])
    if payload["next"] < 0 or payload["end"] < payload["next"]:
        raise ScanWindowError("invalid continuation token position")
    return payload


def _check_backend(file):
    if h5py is None:
        raise ImportError("h5py is required to use scan-angle windows")
    if getattr(file, "backend", None) != "h5py":
        raise CompatibilityError(
            "scan-angle windows are only supported with the 'h5py' backend"
        )


def _build_index(
    file,
    angle=None,
    qc=None,
    reflectivity=None,
    scans=None,
    *,
    qc_fillvalue=None,
    index_group=None,
):
    if not getattr(file, "_writable", False):
        raise ScanWindowError("building a scan index requires a writable file")
    group_path = index_group or INDEX_GROUP
    if group_path in file._h5file:
        raise ScanWindowError(f"scan index already exists at {group_path!r}")

    angle_ds = _resolve_dataset(file, angle, "angle", _ANGLE_CANDIDATES)
    reflectivity_ds = _resolve_dataset(
        file, reflectivity, "reflectivity", _REFLECTIVITY_CANDIDATES
    )
    if qc is None:
        qc_ds = _find_dataset(file, _QC_CANDIDATES)
    else:
        qc_ds = _resolve_dataset(file, qc, "qc", _QC_CANDIDATES)
    if qc_fillvalue is None:
        qc_fillvalue = (
            _dataset_fillvalue(qc_ds) if qc_ds is not None else NO_QC
        )

    angles = np.asarray(angle_ds[()], dtype=np.float64)
    if not np.all(np.isfinite(angles)):
        raise ScanWindowError("angle column contains non-finite values")
    if qc_ds is not None:
        qcs = np.asarray(qc_ds[()], dtype=np.float64)
        if len(qcs) != len(angles):
            raise ScanWindowError("angle and qc columns have different lengths")
    else:
        qcs = np.full(len(angles), float(qc_fillvalue))

    if scans is not None:
        scan_ds = _resolve_dataset(file, scans, "scan", _SCAN_CANDIDATES)
    else:
        scan_ds = _find_dataset(file, _SCAN_CANDIDATES)

    if scan_ds is not None:
        scan_numbers = np.asarray(scan_ds[()], dtype=np.int64)
        if len(scan_numbers) != len(angles):
            raise ScanWindowError("scan and angle columns have different lengths")
        if len(np.unique(scan_numbers)) != len(scan_numbers):
            raise ScanWindowError("scan column contains duplicate numbers")
        if np.any(scan_numbers < 0):
            raise ScanWindowError("scan column contains negative numbers")
    else:
        scan_numbers = np.arange(len(angles), dtype=np.int64)
    positions = np.arange(len(angles), dtype=np.int64)

    if len(reflectivity_ds) < len(angles):
        raise ScanWindowError(
            "reflectivity column is shorter than the angle column; "
            "provide an explicit reflectivity variable"
        )

    present = np.isfinite(qcs) & (qcs != float(qc_fillvalue))

    index = _Index(file._h5file, group_path, True).create()
    group = index._group
    group.attrs["reflectivity_path"] = reflectivity_ds.name
    group.attrs["angle_path"] = angle_ds.name
    group.attrs["qc_path"] = qc_ds.name if qc_ds is not None else ""
    group.attrs["scan_path"] = scan_ds.name if scan_ds is not None else ""
    group.attrs["has_scan_variable"] = scan_ds is not None
    group.attrs["qc_fillvalue"] = float(qc_fillvalue)

    store = index.store()
    scan_tree = _Treap(store, "scan")
    window_tree = _Treap(store, "window")

    scan_entries = sorted(
        (
            (float(angle), int(scan), int(position), float(qc), bool(is_present))
            for angle, scan, position, qc, is_present in zip(
                angles, scan_numbers, positions, qcs, present
            )
        ),
        key=lambda item: item[1],
    )
    window_entries = sorted(
        (
            (float(angle), int(scan), int(position), float(qc), True)
            for angle, scan, position, qc, is_present in zip(
                angles, scan_numbers, positions, qcs, present
            )
            if is_present
        ),
        key=lambda item: (item[0], item[1]),
    )
    root_scan = scan_tree.bulk_build(scan_entries)
    root_window = window_tree.bulk_build(window_entries)
    store.flush()
    node_end = store.base
    index.append_release(
        root_window,
        root_scan,
        len(window_entries),
        int(scan_numbers.max()) + 1 if len(scan_numbers) else 0,
        0,
        node_end,
    )
    file.flush()
    return ScanWindowIndex(file, index_group=group_path, writable=True)


def _can_build(file):
    return (
        getattr(file, "_writable", False)
        and _find_dataset(file, _ANGLE_CANDIDATES) is not None
        and _find_dataset(file, _REFLECTIVITY_CANDIDATES) is not None
    )


def _ensure_index(file, index_group=None):
    group_path = index_group or INDEX_GROUP
    if group_path in file._h5file:
        return ScanWindowIndex(file, index_group=group_path)
    if _can_build(file):
        return _build_index(file, index_group=group_path)
    raise ScanWindowError(
        "no scan index found; call create_scan_index() first"
    )


def create_scan_index(
    file,
    angle=None,
    qc=None,
    reflectivity=None,
    scans=None,
    *,
    qc_fillvalue=None,
    index_group=None,
    replace=False,
):
    """Build the persistent index once from the archive's base columns."""
    _check_backend(file)
    group_path = index_group or INDEX_GROUP
    if group_path in file._h5file:
        if not replace:
            raise ScanWindowError(f"scan index already exists at {group_path!r}")
        del file._h5file[group_path]
    return _build_index(
        file,
        angle=angle,
        qc=qc,
        reflectivity=reflectivity,
        scans=scans,
        qc_fillvalue=qc_fillvalue,
        index_group=group_path,
    )


def open_scan_window(
    file,
    low=None,
    high=None,
    page_size=None,
    release=None,
    token=None,
    index_group=None,
    create=False,
    *,
    min_angle=None,
    max_angle=None,
):
    if min_angle is not None:
        low = min_angle
    if max_angle is not None:
        high = max_angle
    if token is not None:
        index = ScanWindowIndex(file, index_group=index_group)
    elif create and (index_group or INDEX_GROUP) not in file._h5file:
        index = _ensure_index(file, index_group=index_group)
    else:
        index = ScanWindowIndex(file, index_group=index_group)
    return index.window(
        low, high, page_size=page_size, release=release, token=token
    )


def read_scan_window(
    file,
    low=None,
    high=None,
    page_size=None,
    release=None,
    token=None,
    index_group=None,
):
    return open_scan_window(
        file,
        low,
        high,
        page_size=page_size,
        release=release,
        token=token,
        index_group=index_group,
    )


def publish_scan_release(
    file,
    scans=None,
    angles=None,
    qcs=None,
    *,
    present=None,
    positions=None,
    records=None,
    angle_calibrations=None,
    qc_receipts=None,
    index_group=None,
):
    group_path = index_group or INDEX_GROUP
    if group_path not in file._h5file:
        if not _can_build(file):
            index = _create_empty_index(file, index_group=group_path)
            return _publish_initial(
                index,
                scans,
                angles=angles,
                qcs=qcs,
                present=present,
                positions=positions,
                records=records,
                angle_calibrations=angle_calibrations,
                qc_receipts=qc_receipts,
            )
        index = _build_index(file, index_group=group_path)
    else:
        index = ScanWindowIndex(file, index_group=group_path)
    return index.publish(
        scans,
        angles=angles,
        qcs=qcs,
        present=present,
        positions=positions,
        records=records,
        angle_calibrations=angle_calibrations,
        qc_receipts=qc_receipts,
    )


def publish_scan_batch(file, scans=None, angles=None, qcs=None, **kwargs):
    """Alias for :func:`publish_scan_release`."""
    return publish_scan_release(file, scans, angles=angles, qcs=qcs, **kwargs)


def read_scan_page(file, *args, **kwargs):
    """Return the first page of a scan-angle window."""
    return open_scan_window(file, *args, **kwargs).page()


def page_scan_window(file, *args, **kwargs):
    """Return the first page of a scan-angle window."""
    return open_scan_window(file, *args, **kwargs).page()


def iter_scan_window(file, *args, **kwargs):
    """Iterate the pages of a scan-angle window."""
    return open_scan_window(file, *args, **kwargs).pages()


def _create_empty_index(file, index_group):
    if not getattr(file, "_writable", False):
        raise ScanWindowError("creating a scan index requires a writable file")
    reflectivity_ds = _resolve_dataset(
        file, None, "reflectivity", _REFLECTIVITY_CANDIDATES
    )
    index = _Index(file._h5file, index_group, True).create()
    group = index._group
    group.attrs["reflectivity_path"] = reflectivity_ds.name
    group.attrs["angle_path"] = ""
    group.attrs["qc_path"] = ""
    group.attrs["scan_path"] = ""
    group.attrs["has_scan_variable"] = False
    group.attrs["qc_fillvalue"] = NO_QC
    return ScanWindowIndex(file, index_group=index_group, writable=True)


def _publish_initial(
    index,
    scans=None,
    angles=None,
    qcs=None,
    present=None,
    positions=None,
    records=None,
    angle_calibrations=None,
    qc_receipts=None,
):
    if records is not None:
        scans, angles, qcs, present, positions = _records_to_columns(
            records, present=present, positions=positions
        )
    if angle_calibrations is not None or qc_receipts is not None:
        scans, angles, qcs, present, positions = _merge_updates(
            scans,
            angles,
            qcs,
            present,
            positions,
            angle_calibrations,
            qc_receipts,
        )
    if scans is None:
        raise ScanWindowError("a release must name at least one scan")
    scans = np.asarray(list(scans), dtype=np.int64).reshape(-1)
    count = len(scans)
    if count == 0:
        raise ScanWindowError("a release must name at least one scan")
    angles = _broadcast(angles, count, "angles")
    qcs = _broadcast(qcs, count, "qcs")
    present = _broadcast(present, count, "present")
    positions = _broadcast(positions, count, "positions")
    if len(set(int(s) for s in scans)) != count:
        raise ScanWindowError("duplicate scan numbers in one release")

    store = index._index.store()
    scan_tree = _Treap(store, "scan")
    window_tree = _Treap(store, "window")
    scan_entries = []
    window_entries = []
    for i, scan_value in enumerate(scans):
        scan = int(scan_value)
        if scan < 0:
            raise ScanWindowError(f"negative scan number {scan}")
        if angles[i] is None:
            raise ScanWindowError(f"scan {scan} needs an angle for a new index")
        angle = float(angles[i])
        if not math.isfinite(angle):
            raise ScanWindowError(f"scan {scan} has non-finite angle {angle!r}")
        qc = NO_QC if qcs[i] is None else float(qcs[i])
        if present[i] is None:
            has_receipt = qcs[i] is not None
        else:
            has_receipt = bool(present[i])
        position = scan if positions[i] is None else int(positions[i])
        if position < 0:
            raise ScanWindowError(f"negative reflectivity position {position}")
        scan_entries.append((angle, scan, position, qc, has_receipt))
        if has_receipt:
            window_entries.append((angle, scan, position, qc, True))
    scan_entries.sort(key=lambda item: item[1])
    window_entries.sort(key=lambda item: (item[0], item[1]))
    root_scan = scan_tree.bulk_build(scan_entries)
    root_window = window_tree.bulk_build(window_entries)
    store.flush()
    node_end = store.base
    index._index.append_release(
        root_window,
        root_scan,
        len(window_entries),
        int(scans.max()) + 1,
        0,
        node_end,
    )
    index._file.flush()
    return 0


ScanIndex = ScanWindowIndex
ScanWindowReader = ScanWindow
