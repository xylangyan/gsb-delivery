# ----------------------------------------------------------------------------
# PyWavefront
# Copyright (c) 2018 Kurt Yoder
# All rights reserved.
# See the LICENSE file in the repository for the full BSD license text.
# ----------------------------------------------------------------------------
"""
Incremental, per-object loading and versioned archiving for Wavefront scenes.

The normal :class:`~pywavefront.obj.ObjParser` expands every face into an
interleaved float cache the moment the file is read.  A quality-inspection
pipeline that receives small supplemental batches cannot rebuild that cache
for the whole file every time a supplier updates a few records.

This module adds three things on top of the standard parser:

* :class:`RecordingObjParser` parses a complete first delivery exactly like
  the standard parser, but additionally keeps the *provenance* of every
  triangle: the raw signed reference tokens, the resolved source record
  identities, the owning object and the material occurrence that bound it.
* :func:`apply_patch` applies a supplemental batch described by a manifest
  (original record numbers plus exact insert/delete ranges) to one
  object/material group.  Only the triangles actually affected are expanded
  again; the float caches of every other group keep their contents.
* :func:`write_archive` / :func:`load_archive` persist pools, provenance,
  material revisions and expanded group buffers so a scene can be reopened
  from cache, and immutable snapshots of older revisions stay inspectable
  with the material/vertex combination they had at that time.

Positive and negative indices are deliberately treated differently:

* a positive index references a *record identity* (``v:7``, ``vt:3`` ...),
  so inserting records in front of it must not drag the referenced
  normal/position to another entry -- the identity translation keeps
  pointing at the same record and the neighbouring face is not rebuilt;
* a negative index is relative to the current end of pool and is
  re-resolved against the patched pool, so e.g. ``-1`` picks up the new
  last entry once a record is appended.
"""
import gzip
import json
import logging
import struct
from collections import namedtuple
from pathlib import Path

from pywavefront.material import Material, MaterialParser
from pywavefront.mesh import Mesh
from pywavefront.obj import ObjParser
from pywavefront.wavefront import Wavefront
from pywavefront.exceptions import IncrementalPatchError, PywavefrontException

logger = logging.getLogger("pywavefront")

POOL_VERTEX = "v"
POOL_TEXCOORD = "vt"
POOL_NORMAL = "vn"
POOLS = (POOL_VERTEX, POOL_TEXCOORD, POOL_NORMAL)

ARCHIVE_META_SUFFIX = ".pywf.json"
ARCHIVE_BIN_SUFFIX = ".pywf.bin"
ARCHIVE_FORMAT = "pywavefront-incremental/1.0"

# One component reference inside a face.
#   raw: the integer exactly as written (signed); None when absent
#   key: stable record identity for positive refs, None for negative refs
#   pos: 0-based position the reference resolved to at emit time
Ref = namedtuple("Ref", "raw key pos")

# One triangulated face with full provenance
ProvTriangle = namedtuple(
    "ProvTriangle",
    "refs has_vt has_vn group_key material_key mesh_name line_no event_seq",
)

GroupState = namedtuple(
    "GroupState",
    "key mesh_name material_key vertex_format triangles buffer",
)


def _identity(pool_name, one_based):
    return "{}:{}".format(pool_name, one_based)


class Pool:
    """A vertex/texcoord/normal pool with stable record identities."""

    def __init__(self, name, values=None, identities=None):
        self.name = name
        self.values = [tuple(v) for v in (values or [])]
        if identities is not None:
            self.identities = list(identities)
        else:
            self.identities = [_identity(name, i + 1)
                               for i in range(len(self.values))]
        self._rebuild_index()

    def _rebuild_index(self):
        self._index = {ident: pos for pos, ident in enumerate(self.identities)}

    def position_of(self, identity):
        try:
            return self._index[identity]
        except KeyError:
            raise IncrementalPatchError(
                "record {} was deleted from pool {}".format(identity, self.name))

    def insert(self, start_one_based, new_values, patch_id):
        pos = start_one_based - 1
        if not 0 <= pos <= len(self.values):
            raise IncrementalPatchError(
                "insert position {} out of range for pool {} ({} records)"
                .format(start_one_based, self.name, len(self.values)))
        new_idents = ["{}:{}:{}".format(patch_id, self.name, i + 1)
                      for i in range(len(new_values))]
        self.values[pos:pos] = [tuple(v) for v in new_values]
        self.identities[pos:pos] = new_idents
        self._rebuild_index()

    def delete(self, start_one_based, count):
        pos = start_one_based - 1
        if pos < 0 or pos + count > len(self.values) or count <= 0:
            raise IncrementalPatchError(
                "delete range {}/{} out of range for pool {} ({} records)"
                .format(start_one_based, count, self.name, len(self.values)))
        del self.values[pos:pos + count]
        del self.identities[pos:pos + count]
        self._rebuild_index()

    def resolve_ref(self, ref):
        """Resolve a face reference against the current pool state."""
        if ref is None:
            return None
        if ref.raw < 0:
            position = len(self.values) + ref.raw
            if position < 0:
                raise IncrementalPatchError(
                    "negative reference {} no longer resolves in pool {} "
                    "({} records)".format(ref.raw, self.name, len(self.values)))
            return position
        if ref.key is None:
            # Parser quirks (an empty component or an explicit zero):
            # the position was fixed when the face was emitted.
            return ref.pos
        return self._index[ref.key]

    def contains_identity(self, identity):
        return identity in self._index


class PatchManifest:
    """Validated view of a supplemental batch manifest (json)."""

    def __init__(self, data):
        try:
            self.patch_id = str(data["patch_id"])
            self.base_revision = str(data["base_revision"])
            self.object_name = str(data["object"])
        except KeyError as missing:
            raise IncrementalPatchError(
                "manifest is missing required field {}".format(missing))

        self.new_revision = str(data.get("new_revision", self.patch_id))
        self.material_name = data.get("material")
        mtllib = data.get("mtllib")
        self.mtllib = dict(mtllib) if mtllib else None
        if self.mtllib:
            for required in ("file", "revision"):
                if required not in self.mtllib:
                    raise IncrementalPatchError(
                        "manifest mtllib entry is missing {!r}".format(required))
            # Logical library name as referenced by the obj; when omitted the
            # replacement file itself is the logical name.
            self.mtllib.setdefault("name", self.mtllib["file"])

        self.data_changes = {
            name: list(data.get("data_changes", {}).get(name, []))
            for name in POOLS}
        for name, changes in self.data_changes.items():
            for change in changes:
                if change.get("op") not in ("insert", "delete") \
                        or not isinstance(change.get("start"), int) \
                        or not isinstance(change.get("count"), int) \
                        or change["count"] <= 0:
                    raise IncrementalPatchError(
                        "invalid {} change in manifest: {}".format(name, change))

        self.face_replacements = list(data.get("face_replacements", []))
        for rng in self.face_replacements:
            if not isinstance(rng.get("start"), int) \
                    or not isinstance(rng.get("count"), int) or rng["count"] <= 0:
                raise IncrementalPatchError(
                    "invalid face replacement range: {}".format(rng))

    @classmethod
    def from_file(cls, path):
        with open(str(path), "r", encoding="utf-8") as fd:
            return cls(json.load(fd))


# ---------------------------------------------------------------------------
# Recording parser
# ---------------------------------------------------------------------------

class RecordingObjParser(ObjParser):
    """Obj parser that additionally records triangle provenance.

    The expanded float output is identical to :class:`~pywavefront.obj.ObjParser`;
    the extra bookkeeping (pools, groups, material occurrences) lives on the
    wavefront scene.
    """

    def __init__(self, wavefront, file_name, revision="r0",
                 mtl_revisions=None, strict=False, encoding="utf-8",
                 create_materials=False, collect_faces=False, parse=True,
                 cache=False):
        self.scene_revision = revision
        self.mtl_revisions = mtl_revisions or {}
        self._line_number = 0
        self._event_seq = 0
        self._current_mtllib = None
        super(RecordingObjParser, self).__init__(
            wavefront, file_name, strict=strict, encoding=encoding,
            create_materials=create_materials, collect_faces=collect_faces,
            parse=parse, cache=cache)

    def next_line(self):
        super(RecordingObjParser, self).next_line()
        self._line_number += 1

    def _setup_scene(self):
        wavefront = self.wavefront
        wavefront.current_revision = self.scene_revision
        wavefront.base_revision = self.scene_revision
        wavefront.history = [{"revision": self.scene_revision, "patch_id": None}]
        # Stable material instances keyed by source file + material name, so
        # two mtl files defining the same name stay distinct.
        wavefront.material_registry = {}
        wavefront.material_groups = {}
        wavefront.groups = {}
        wavefront.material_events = []
        wavefront.ident_v = []
        wavefront.ident_vt = []
        wavefront.ident_vn = []

    def parse(self):
        self._setup_scene()
        super(RecordingObjParser, self).parse()

    def post_parse(self):
        scene = self.wavefront
        scene.pool_vt = list(self.tex_coords)
        scene.ident_vt = [_identity(POOL_TEXCOORD, i + 1)
                          for i in range(len(scene.pool_vt))]
        scene.pool_vn = list(self.normals)
        scene.ident_vn = [_identity(POOL_NORMAL, i + 1)
                          for i in range(len(scene.pool_vn))]
        scene.ident_v = [_identity(POOL_VERTEX, i + 1)
                         for i in range(len(scene.vertices))]
        # Assemble material.vertices from the authoritative group buffers.
        for material_key, group_keys in scene.material_groups.items():
            material = scene.material_registry[material_key]
            material.vertices = []
            for group_key in group_keys:
                material.vertices += list(scene.groups[group_key].buffer)
        super(RecordingObjParser, self).post_parse()

    def material_key(self, mtllib_name, material_name):
        if mtllib_name:
            return "{}::{}".format(mtllib_name, material_name)
        return "obj::{}".format(material_name)

    def parse_mtllib(self):
        mtllib = " ".join(self.values[1:])
        revision = self.mtl_revisions.get(mtllib)
        try:
            materials = self.material_parser_cls(
                self.dir / mtllib,
                encoding=self.encoding,
                strict=self.strict,
                collect_faces=self.collect_faces,
                revision=revision,
            ).materials
            self.wavefront.mtllibs.append(mtllib)
        except IOError:
            if self.create_materials:
                self.consume_line()
                return
            raise

        self._current_mtllib = Path(mtllib).name
        for name, material in materials.items():
            key = self.material_key(self._current_mtllib, name)
            material.source_file = self._current_mtllib
            material.mtl_revision = revision
            self.wavefront.material_registry[key] = material
            # Keep the legacy flat name mapping; last loaded wins, as usual.
            self.wavefront.materials[name] = material
        self.consume_line()

    def _bind_material(self, name):
        key = self.material_key(self._current_mtllib, name)
        registry = self.wavefront.material_registry
        material = registry.get(key)
        if material is None:
            if not self.create_materials:
                raise PywavefrontException("Unknown material: %s" % name)
            material = Material(name, is_default=True,
                                has_faces=self.collect_faces)
            material.source_file = self._current_mtllib
            registry[key] = material
            self.wavefront.materials[name] = material
        return key, material

    def parse_usemtl(self):
        name = " ".join(self.values[1:])
        key, material = self._bind_material(name)
        self.material = material
        self._event_seq += 1
        self.wavefront.material_events.append({
            "seq": self._event_seq,
            "line_no": self._line_number,
            "mesh": self.mesh.name if self.mesh else None,
            "material_key": key,
            "material_name": name,
            "mtllib": self._current_mtllib,
        })
        if self.mesh is not None:
            self.mesh.add_material(material)
        self.consume_line()

    def parse_usemat(self):
        self.parse_usemtl()

    def _current_group(self, has_vt, has_vn, has_colors):
        if self.material is None:
            self.material = Material(
                "default{}".format(len(self.wavefront.materials)),
                is_default=True, has_faces=self.collect_faces)
            key = self.material_key(None, self.material.name)
            self.material.source_file = None
            self.wavefront.material_registry[key] = self.material
            self.wavefront.materials[self.material.name] = self.material
            self._event_seq += 1
            self.wavefront.material_events.append({
                "seq": self._event_seq,
                "line_no": self._line_number,
                "mesh": self.mesh.name if self.mesh else None,
                "material_key": key,
                "material_name": self.material.name,
                "mtllib": None,
            })

        if self.mesh is None:
            self.mesh = Mesh(has_faces=self.collect_faces)
            self.wavefront.add_mesh(self.mesh)
            self.mesh.add_material(self.material)

        self.mesh.add_material(self.material)
        material_key = self.material_key(self._current_mtllib,
                                         self.material.name)
        group_key = "{}|{}".format(self.mesh.name, material_key)
        scene = self.wavefront
        group = scene.groups.get(group_key)
        if group is None:
            vertex_format = "_".join(e[0] for e in [
                ("T2F", has_vt),
                ("C3F", has_colors),
                ("N3F", has_vn),
                ("V3F", True),
            ] if e[1])
            if self.material.vertex_format and \
                    self.material.vertex_format != vertex_format:
                raise ValueError((
                    "Trying to merge vertex data with different format: {}. "
                    "Material {} has vertex format {}"
                ).format(vertex_format, self.material.name,
                         self.material.vertex_format))
            self.material.vertex_format = vertex_format
            group = GroupState(group_key, self.mesh.name, material_key,
                               vertex_format, [], [])
            scene.groups[group_key] = group
            scene.material_groups.setdefault(material_key, []).append(group_key)
        return group, material_key

    def parse_f(self):
        parts = self.values[1].split("/")
        has_vt = len(parts) >= 2 and (len(parts) == 2 or parts[1] != "")
        has_vn = len(parts) == 3
        first_v = int(parts[0])
        probe_index = first_v + len(self.wavefront.vertices) if first_v < 0 \
            else first_v - 1
        has_colors = len(self.wavefront.vertices[probe_index]) == 6

        group, material_key = self._current_group(has_vt, has_vn, has_colors)

        while True:
            refs = [self._make_ref(token, has_vt, has_vn)
                    for token in self.values[1:]]
            fan = [(0, 1, 2)]
            for j in range(3, len(refs)):
                fan.append((j, 0, j - 1))

            for tri in fan:
                tri_refs = tuple(refs[k] for k in tri)
                triangle = ProvTriangle(
                    refs=tri_refs, has_vt=has_vt, has_vn=has_vn,
                    group_key=group.key, material_key=material_key,
                    mesh_name=group.mesh_name, line_no=self._line_number,
                    event_seq=self._event_seq,
                )
                group.triangles.append(triangle)
                group.buffer.extend(
                    self._emit(tri_refs, has_vt, has_vn, has_colors))
                if self.collect_faces:
                    self.mesh.faces.append([ref.v.pos for ref in tri_refs])

            try:
                self.next_line()
            except StopIteration:
                self.line = None
                self.values = None
                break
            if not self.values or self.values[0] != "f":
                break

    def _make_ref(self, token, has_vt, has_vn):
        parts = token.split("/")
        v_ref = self._component_ref(int(parts[0]), POOL_VERTEX,
                                    len(self.wavefront.vertices))

        vt_ref = None
        if has_vt:
            vt_ref = self._component_ref(parts[1], POOL_TEXCOORD,
                                         len(self.tex_coords))

        vn_ref = None
        if has_vn:
            vn_ref = self._component_ref(parts[2], POOL_NORMAL,
                                         len(self.normals))
        return FaceRefs(v_ref, vt_ref, vn_ref)

    def _component_ref(self, part, pool_name, pool_length):
        if part == "":
            # An empty component ("1//1") lands on the first record in the
            # standard parser, unlike an explicit zero which stays past the
            # end and resolves to a missing component.
            return Ref(0, None, 0)
        return _initial_ref(int(part), pool_name, pool_length)

    def _emit(self, refs, has_vt, has_vn, has_colors):
        floats = []
        for ref in refs:
            if has_vt and ref.vt.pos < len(self.tex_coords):
                floats.extend(self.tex_coords[ref.vt.pos])
            if has_colors:
                floats.extend(self.wavefront.vertices[ref.v.pos][3:])
            if has_vn and ref.vn.pos < len(self.normals):
                floats.extend(self.normals[ref.vn.pos])
            vertex = self.wavefront.vertices[ref.v.pos]
            floats.extend(vertex[0:3] if has_colors else vertex)
        return floats


# Tuple holding the three component references of one face corner
FaceRefs = namedtuple("FaceRefs", "v vt vn")


# ---------------------------------------------------------------------------
# Supplemental file parsing
# ---------------------------------------------------------------------------

def record_scene(file_name, revision="r0", mtl_revisions=None,
                 strict=False, encoding="utf-8", create_materials=False,
                 collect_faces=False):
    """Run the first, complete scan of a delivery and record provenance."""
    scene = Wavefront.create_blank(file_name)
    RecordingObjParser(
        scene, file_name, revision=revision, mtl_revisions=mtl_revisions,
        strict=strict, encoding=encoding,
        create_materials=create_materials, collect_faces=collect_faces,
        parse=True, cache=False)
    return scene


SupplementalData = namedtuple(
    "SupplementalData", "pool_records face_lines usemtl mtllib")


def parse_supplemental(path):
    """Parse the small partial obj shipped with a patch."""
    pool_records = {name: [] for name in POOLS}
    face_lines = []
    usemtl = None
    mtllib = None
    with open(str(path), "r", encoding="utf-8") as fd:
        for line in fd:
            values = line.split()
            if not values or values[0] == "#" or len(values) < 2:
                continue
            keyword = values[0]
            if keyword in POOLS:
                pool_records[keyword].append(
                    tuple(float(v) for v in values[1:]))
            elif keyword == "f":
                face_lines.append(values[1:])
            elif keyword == "usemtl":
                usemtl = " ".join(values[1:])
            elif keyword == "mtllib":
                mtllib = " ".join(values[1:])
    return SupplementalData(pool_records, face_lines, usemtl, mtllib)


# ---------------------------------------------------------------------------
# Face expansion against patched pools
# ---------------------------------------------------------------------------

def _component_ref(raw, pool):
    if raw is None:
        return None
    if raw < 0:
        return Ref(raw, None, len(pool.values) + raw)
    if raw == 0:
        return Ref(raw, None, len(pool.values))
    identity = _identity(pool.name, raw)
    if not pool.contains_identity(identity):
        raise IncrementalPatchError(
            "face references {} record {} but it no longer exists"
            .format(pool.name, raw))
    return Ref(raw, identity, raw - 1)


def _component_ref_from_part(part, pool):
    if part == "":
        return Ref(0, None, 0)
    return _component_ref(int(part), pool)


def _initial_ref(raw, pool_name, pool_length):
    """Build a reference during the first full parse (pools are plain lists)."""
    if raw < 0:
        return Ref(raw, None, pool_length + raw)
    if raw == 0:
        return Ref(raw, None, pool_length)
    return Ref(raw, _identity(pool_name, raw), raw - 1)


def _expand_face(tokens, has_vt, has_vn, pools, group, line_no, event_seq):
    refs = []
    for token in tokens:
        parts = token.split("/")
        v_ref = _component_ref(int(parts[0]), pools[POOL_VERTEX])

        vt_ref = None
        if has_vt:
            vt_ref = _component_ref_from_part(parts[1], pools[POOL_TEXCOORD])

        vn_ref = None
        if has_vn:
            vn_ref = _component_ref_from_part(parts[2], pools[POOL_NORMAL])

        refs.append(FaceRefs(v_ref, vt_ref, vn_ref))

    fan = [(0, 1, 2)]
    for j in range(3, len(refs)):
        fan.append((j, 0, j - 1))

    return [ProvTriangle(
        refs=tuple(refs[k] for k in tri), has_vt=has_vt, has_vn=has_vn,
        group_key=group.key, material_key=group.material_key,
        mesh_name=group.mesh_name, line_no=line_no, event_seq=event_seq,
    ) for tri in fan]


def _expand_floats(triangle, pools):
    has_colors = any(len(record) == 6
                     for record in pools[POOL_VERTEX].values)
    floats = []
    for ref in triangle.refs:
        if triangle.has_vt:
            pos = pools[POOL_TEXCOORD].resolve_ref(ref.vt)
            if pos is not None and pos < len(pools[POOL_TEXCOORD].values):
                floats.extend(pools[POOL_TEXCOORD].values[pos])
        if has_colors:
            v_pos = pools[POOL_VERTEX].resolve_ref(ref.v)
            floats.extend(pools[POOL_VERTEX].values[v_pos][3:])
        if triangle.has_vn:
            pos = pools[POOL_NORMAL].resolve_ref(ref.vn)
            if pos is not None and pos < len(pools[POOL_NORMAL].values):
                floats.extend(pools[POOL_NORMAL].values[pos])
        v_pos = pools[POOL_VERTEX].resolve_ref(ref.v)
        vertex = pools[POOL_VERTEX].values[v_pos]
        floats.extend(vertex[0:3] if has_colors else vertex)
    return floats


def _triangle_is_dirty(triangle, pools, changed_pools):
    """Decide from provenance whether a triangle must be re-expanded."""
    for ref in triangle.refs:
        for pool_name, component in (
                (POOL_VERTEX, ref.v),
                (POOL_TEXCOORD, ref.vt),
                (POOL_NORMAL, ref.vn)):
            if component is None or pool_name not in changed_pools:
                continue
            if component.raw < 0:
                # Relative references follow the pool end and therefore
                # change meaning as soon as that pool is patched.
                return True
            if component.key is None:
                # Parser quirk (empty component or explicit zero): the
                # emitted position was fixed by definition.
                continue
            if not pools[pool_name].contains_identity(component.key):
                # The exact record the positive index pointed at is gone.
                return True
    return False


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def apply_patch(scene, manifest_path, supplemental_path):
    """Apply one supplemental batch to a recorded/archived scene.

    :param scene: a :class:`~pywavefront.wavefront.Wavefront` produced by
        :class:`RecordingObjParser` or :func:`load_archive`
    :param manifest_path: json manifest with record ranges and revisions
    :param supplemental_path: partial obj containing the new records and any
        replacement face lines
    :return: the scene, mutated in place
    """
    if not hasattr(scene, "groups"):
        raise IncrementalPatchError(
            "scene was not recorded; load it with RecordingObjParser first")

    manifest = PatchManifest.from_file(manifest_path)
    if manifest.base_revision != scene.current_revision:
        raise IncrementalPatchError(
            "patch {} expects revision {} but scene is at {}"
            .format(manifest.patch_id, manifest.base_revision,
                    scene.current_revision))

    supplemental = parse_supplemental(supplemental_path)

    target_groups = [group for group in scene.groups.values()
                     if group.mesh_name == manifest.object_name]
    if not target_groups:
        raise IncrementalPatchError(
            "patch {} targets unknown object {}".format(
                manifest.patch_id, manifest.object_name))

    # Work on pool copies first so a rejected patch never mutates the scene.
    pools = _load_pools(scene)
    changed_pools = set()
    _apply_data_changes(manifest, supplemental, pools, changed_pools)

    # Validate the face ranges of every target group before touching state.
    plans = []
    for group in target_groups:
        dirty = {index for index, triangle in enumerate(group.triangles)
                 if _triangle_is_dirty(triangle, pools, changed_pools)}
        declared = _declared_indexes(manifest.face_replacements)
        if dirty != declared:
            raise IncrementalPatchError(
                "face replacement ranges {} do not match triangles actually "
                "affected {} in group {}".format(
                    sorted(declared), sorted(dirty), group.key))
        if declared:
            _validate_ranges(group, manifest.face_replacements)
        plans.append((group, dirty))

    if len(supplemental.face_lines) != \
            sum(rng["count"] for rng in manifest.face_replacements):
        raise IncrementalPatchError(
            "supplemental face count does not match face_replacements")

    # The pre-patch state is frozen before the first in-memory mutation.
    base, _ = _archive_paths(scene.file_name)
    snapshot_version(scene, base, scene.current_revision)

    if manifest.mtllib:
        _reload_material(scene, manifest, supplemental)

    for group, _ in plans:
        _rebuild_group(scene, group, manifest, supplemental, pools)

    _store_pools(scene, pools)
    scene.current_revision = manifest.new_revision
    scene.history.append({
        "revision": manifest.new_revision,
        "patch_id": manifest.patch_id,
        "object": manifest.object_name,
    })
    logger.info("applied patch %s -> revision %s",
                manifest.patch_id, scene.current_revision)
    return scene


def _load_pools(scene):
    return {
        POOL_VERTEX: Pool(POOL_VERTEX, scene.vertices,
                          getattr(scene, "ident_v", None)),
        POOL_TEXCOORD: Pool(POOL_TEXCOORD, getattr(scene, "pool_vt", []),
                            getattr(scene, "ident_vt", None)),
        POOL_NORMAL: Pool(POOL_NORMAL, getattr(scene, "pool_vn", []),
                          getattr(scene, "ident_vn", None)),
    }


def _store_pools(scene, pools):
    scene.vertices = list(pools[POOL_VERTEX].values)
    scene.ident_v = list(pools[POOL_VERTEX].identities)
    scene.pool_vt = list(pools[POOL_TEXCOORD].values)
    scene.ident_vt = list(pools[POOL_TEXCOORD].identities)
    scene.pool_vn = list(pools[POOL_NORMAL].values)
    scene.ident_vn = list(pools[POOL_NORMAL].identities)


def _apply_data_changes(manifest, supplemental, pools, changed_pools):
    for pool_name in POOLS:
        pool = pools[pool_name]
        incoming = supplemental.pool_records[pool_name]
        cursor = 0
        for change in manifest.data_changes[pool_name]:
            start, count, op = change["start"], change["count"], change["op"]
            if op == "insert":
                segment = incoming[cursor:cursor + count]
                if len(segment) != count:
                    raise IncrementalPatchError(
                        "manifest declares {} {} inserts but supplemental "
                        "file contains {}".format(count, pool_name, len(segment)))
                pool.insert(start, segment, manifest.patch_id)
                cursor += count
                changed_pools.add(pool_name)
            else:
                pool.delete(start, count)
                changed_pools.add(pool_name)
        if cursor != len(incoming):
            raise IncrementalPatchError(
                "supplemental {} records do not match manifest ranges"
                .format(pool_name))


def _reload_material(scene, manifest, supplemental):
    mtl_file = manifest.mtllib["file"]
    logical_name = Path(manifest.mtllib["name"]).name
    revision = manifest.mtllib["revision"]
    mtl_path = Path(scene.file_name).parent / mtl_file
    if not mtl_path.exists():
        mtl_path = Path(mtl_file)
    materials = MaterialParser(mtl_path, revision=revision).materials

    name = manifest.material_name or supplemental.usemtl
    if name not in materials:
        raise IncrementalPatchError(
            "material {} not found in {}".format(name, mtl_file))
    material = materials[name]
    # The key is the *logical* material library + name, so successive
    # revisions of the same library rebind the same groups instead of
    # orphaning them.  The physical file and revision live on the material.
    key = "{}::{}".format(logical_name, name)
    material.source_file = logical_name
    material.mtl_revision = revision

    previous = scene.material_registry.get(key)
    scene.material_registry[key] = material
    scene.materials[name] = material
    if previous is not None:
        # Keep mesh references pointing at the current revision of the key.
        for mesh in scene.mesh_list:
            mesh.materials = [material if item is previous else item
                              for item in mesh.materials]
        material.vertex_format = previous.vertex_format
        material.vertices = list(previous.vertices)

    scene.material_events.append({
        "seq": len(scene.material_events) + 1,
        "line_no": None,
        "mesh": manifest.object_name,
        "material_key": key,
        "material_name": name,
        "mtllib": logical_name,
        "mtl_revision": revision,
        "patch_id": manifest.patch_id,
    })


def _rebuild_group(scene, group, manifest, supplemental, pools):
    declared = _declared_indexes(manifest.face_replacements)
    if not declared:
        # Geometry is untouched (e.g. a material-only patch): the expanded
        # buffer stays exactly as it was.
        return

    replacement = {}
    face_cursor = 0
    for rng in manifest.face_replacements:
        new_triangles = []
        for _ in range(rng["count"]):
            tokens = supplemental.face_lines[face_cursor]
            face_cursor += 1
            new_triangles.extend(_expand_face(
                tokens, group.triangles[0].has_vt,
                group.triangles[0].has_vn, pools, group,
                line_no=None, event_seq=len(scene.material_events)))
        replacement[rng["start"]] = (rng["count"], new_triangles)

    vertex_size = _vertex_size(group.vertex_format)
    updated_triangles = []
    updated_buffer = []
    index = 0
    while index < len(group.triangles):
        if index in replacement:
            count, new_triangles = replacement[index]
            updated_triangles.extend(new_triangles)
            for triangle in new_triangles:
                updated_buffer.extend(_expand_floats(triangle, pools))
            index += count
        else:
            triangle = group.triangles[index]
            updated_triangles.append(triangle)
            start = index * 3 * vertex_size
            updated_buffer.extend(
                group.buffer[start:start + 3 * vertex_size])
            index += 1

    material_key = group.material_key
    scene.groups[group.key] = GroupState(
        group.key, group.mesh_name, material_key, group.vertex_format,
        updated_triangles, updated_buffer)

    material = scene.material_registry[material_key]
    material.vertices = []
    for group_key in scene.material_groups[material_key]:
        material.vertices += list(scene.groups[group_key].buffer)


def _declared_indexes(ranges):
    indexes = set()
    for rng in ranges:
        indexes.update(range(rng["start"], rng["start"] + rng["count"]))
    return indexes


def _validate_ranges(group, ranges):
    ordered = sorted(ranges, key=lambda rng: rng["start"])
    cursor = 0
    for rng in ordered:
        if rng["start"] < cursor:
            raise IncrementalPatchError(
                "overlapping face replacement ranges in {}".format(group.key))
        if rng["start"] + rng["count"] > len(group.triangles):
            raise IncrementalPatchError(
                "face replacement range exceeds triangle count of {}"
                .format(group.key))
        cursor = rng["start"] + rng["count"]


def _vertex_size(vertex_format):
    size = 3
    if "T2F" in vertex_format:
        size += 2
    if "C3F" in vertex_format:
        size += 3
    if "N3F" in vertex_format:
        size += 3
    return size


# ---------------------------------------------------------------------------
# Versioned archive
# ---------------------------------------------------------------------------

def _archive_paths(file_name):
    path = Path(file_name)
    return path.with_suffix(""), path


def write_archive(scene, file_name):
    """Write the canonical cache files (``.pywf.json`` / ``.pywf.bin``)."""
    base, _ = _archive_paths(file_name)
    _write_snapshot(
        scene,
        str(base) + ARCHIVE_META_SUFFIX,
        str(base) + ARCHIVE_BIN_SUFFIX,
    )


def snapshot_version(scene, file_name_or_base, revision):
    """Write an immutable snapshot for a historical revision."""
    path = Path(file_name_or_base)
    base = path.with_suffix("") if path.suffix else path
    meta_path = "{}.{}{}".format(base, revision, ARCHIVE_META_SUFFIX)
    bin_path = "{}.{}{}".format(base, revision, ARCHIVE_BIN_SUFFIX)
    _write_snapshot(scene, meta_path, bin_path, revision=revision)
    return meta_path, bin_path


def _write_snapshot(scene, meta_path, bin_path, revision=None):
    pools = _load_pools(scene)
    sections = []
    blob = bytearray()

    for name in POOLS:
        pool = pools[name]
        offset = len(blob) // 4
        for record in pool.values:
            blob.extend(struct.pack("{}f".format(len(record)), *record))
        sections.append({
            "kind": "pool", "name": name,
            "offset": offset,
            "length": sum(len(record) for record in pool.values),
            "dim": len(pool.values[0]) if pool.values else 0,
            "identities": list(pool.identities),
        })

    for key, group in scene.groups.items():
        offset = len(blob) // 4
        blob.extend(struct.pack("{}f".format(len(group.buffer)), *group.buffer))
        sections.append({
            "kind": "group", "key": key,
            "mesh": group.mesh_name,
            "material_key": group.material_key,
            "vertex_format": group.vertex_format,
            "offset": offset, "length": len(group.buffer),
            "triangles": [_triangle_to_dict(tri) for tri in group.triangles],
        })

    material_sections = []
    for key, material in scene.material_registry.items():
        material_sections.append({
            "key": key,
            "name": material.name,
            "source_file": material.source_file,
            "mtl_revision": material.mtl_revision,
            "vertex_format": material.vertex_format,
            "groups": list(scene.material_groups.get(key, [])),
            "diffuse": list(material.diffuse),
            "ambient": list(material.ambient),
            "specular": list(material.specular),
            "transparency": material.transparency,
            "shininess": material.shininess,
        })

    meta = {
        "format": ARCHIVE_FORMAT,
        "revision": revision or scene.current_revision,
        "current_revision": scene.current_revision,
        "base_revision": scene.base_revision,
        "history": list(scene.history),
        "file_name": str(scene.file_name) if scene.file_name else None,
        "mtllibs": list(scene.mtllibs),
        "material_events": list(scene.material_events),
        "materials": material_sections,
        "sections": sections,
        "meshes": [{
            "name": mesh.name,
            "material_keys": [
                "{}::{}".format(material.source_file or "obj", material.name)
                for material in mesh.materials],
            "faces": list(getattr(mesh, "faces", [])),
        } for mesh in scene.mesh_list],
    }

    with gzip.open(bin_path, "wb") as fd:
        fd.write(bytes(blob))
    with open(meta_path, "w", encoding="utf-8") as fd:
        json.dump(meta, fd, indent=2)


def _triangle_to_dict(triangle):
    return {
        "refs": [[_ref_to_dict(ref) for ref in face_refs]
                 for face_refs in triangle.refs],
        "has_vt": triangle.has_vt,
        "has_vn": triangle.has_vn,
        "group_key": triangle.group_key,
        "material_key": triangle.material_key,
        "mesh_name": triangle.mesh_name,
        "line_no": triangle.line_no,
        "event_seq": triangle.event_seq,
    }


def _ref_to_dict(ref):
    if ref is None:
        return None
    return [ref.raw, ref.key, ref.pos]


def _ref_from_dict(data):
    if data is None:
        return None
    return Ref(raw=data[0], key=data[1], pos=data[2])


def load_archive(file_name, revision=None):
    """Reopen a scene from cache, optionally at a historical revision."""
    base, _ = _archive_paths(file_name)
    if revision:
        meta_path = "{}.{}{}".format(base, revision, ARCHIVE_META_SUFFIX)
        bin_path = "{}.{}{}".format(base, revision, ARCHIVE_BIN_SUFFIX)
    else:
        meta_path = str(base) + ARCHIVE_META_SUFFIX
        bin_path = str(base) + ARCHIVE_BIN_SUFFIX

    with open(meta_path, "r", encoding="utf-8") as fd:
        meta = json.load(fd)
    if meta.get("format") != ARCHIVE_FORMAT:
        raise IncrementalPatchError(
            "unknown archive format in {}".format(meta_path))

    with gzip.open(bin_path, "rb") as fd:
        blob = fd.read()

    scene = Wavefront.create_blank(meta.get("file_name") or file_name)
    scene.current_revision = meta["current_revision"]
    scene.base_revision = meta["base_revision"]
    scene.history = list(meta["history"])
    scene.mtllibs = list(meta["mtllibs"])
    scene.material_events = list(meta["material_events"])
    scene.material_registry = {}
    scene.material_groups = {}
    scene.groups = {}

    for section in meta["sections"]:
        if section["kind"] != "pool":
            continue
        count = section["length"]
        values = struct.unpack(
            "{}f".format(count),
            blob[section["offset"] * 4:
                 section["offset"] * 4 + count * 4])
        dim = section["dim"]
        records = [tuple(values[i:i + dim]) for i in range(0, count, dim)]
        if section["name"] == POOL_VERTEX:
            scene.vertices = records
            scene.ident_v = list(section["identities"])
        elif section["name"] == POOL_TEXCOORD:
            scene.pool_vt = records
            scene.ident_vt = list(section["identities"])
        else:
            scene.pool_vn = records
            scene.ident_vn = list(section["identities"])

    for mat_meta in meta["materials"]:
        material = Material(mat_meta["name"])
        material.source_file = mat_meta["source_file"]
        material.mtl_revision = mat_meta["mtl_revision"]
        material.vertex_format = mat_meta["vertex_format"]
        material.diffuse = list(mat_meta["diffuse"])
        material.ambient = list(mat_meta["ambient"])
        material.specular = list(mat_meta["specular"])
        material.transparency = mat_meta["transparency"]
        material.shininess = mat_meta["shininess"]
        scene.material_registry[mat_meta["key"]] = material
        scene.materials[material.name] = material
        scene.material_groups[mat_meta["key"]] = list(mat_meta["groups"])

    for section in meta["sections"]:
        if section["kind"] != "group":
            continue
        count = section["length"]
        buffer_ = list(struct.unpack(
            "{}f".format(count),
            blob[section["offset"] * 4:
                 section["offset"] * 4 + count * 4]))
        triangles = []
        for tri in section["triangles"]:
            refs = tuple(FaceRefs(
                v=_ref_from_dict(corner[0]),
                vt=_ref_from_dict(corner[1]),
                vn=_ref_from_dict(corner[2]),
            ) for corner in tri["refs"])
            triangles.append(ProvTriangle(
                refs=refs, has_vt=tri["has_vt"], has_vn=tri["has_vn"],
                group_key=tri["group_key"], material_key=tri["material_key"],
                mesh_name=tri["mesh_name"], line_no=tri["line_no"],
                event_seq=tri["event_seq"],
            ))
        scene.groups[section["key"]] = GroupState(
            section["key"], section["mesh"], section["material_key"],
            section["vertex_format"], triangles, buffer_)

    for mesh_meta in meta["meshes"]:
        mesh = Mesh(mesh_meta["name"])
        for key in mesh_meta["material_keys"]:
            if key in scene.material_registry:
                mesh.add_material(scene.material_registry[key])
        mesh.faces = list(mesh_meta["faces"])
        scene.mesh_list.append(mesh)
        scene.meshes[mesh.name] = mesh

    for key, group_keys in scene.material_groups.items():
        material = scene.material_registry[key]
        material.vertices = []
        for group_key in group_keys:
            material.vertices += list(scene.groups[group_key].buffer)

    return scene
