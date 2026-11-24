import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from pywavefront import Wavefront
from pywavefront.exceptions import IncrementalPatchError
from pywavefront.incremental import (
    record_scene,
    apply_patch,
    write_archive,
    load_archive,
    Pool,
    parse_supplemental,
    _load_pools,
)

FIXTURE = Path(__file__).parent / "fixtures" / "incremental"


def copy_fixture_tree():
    tmp = Path(tempfile.mkdtemp(prefix="pywf-inc-"))
    for item in FIXTURE.iterdir():
        shutil.copy(str(item), str(tmp / item.name))
    return tmp


def mesh_buffer(scene, mesh_name):
    return [value
            for group in scene.groups.values()
            if group.mesh_name == mesh_name
            for value in group.buffer]


def plain_material_vertices(path, mesh_name):
    scene = Wavefront(str(path))
    mesh = scene.meshes[mesh_name]
    return [value for material in mesh.materials
            for value in material.vertices]


class TestRecordingParity(unittest.TestCase):
    """The recording parser must expand exactly like the standard parser."""

    FIXTURES = [
        "simple.obj",
        "simple.obj.gz",
        "simple_negative_indices.obj",
        "simple_vt.obj",
        "simple_normals.obj",
        "simple_positions.obj",
        "simple_colors.obj",
        "simple_missing_uv.obj",
        "simple_missing_normal.obj",
        "simple_zero_indices.obj",
        "arbitrary-faces.obj",
    ]

    def test_expanded_vertices_match_standard_parser(self):
        fixtures = Path(__file__).parent / "fixtures"
        for name in self.FIXTURES:
            standard = Wavefront(str(fixtures / name))
            recorded = record_scene(str(fixtures / name))

            self.assertEqual(
                [mesh.name for mesh in standard.mesh_list],
                [mesh.name for mesh in recorded.mesh_list], name)

            # Materials are the aggregation unit: shared default materials
            # accumulate vertices from several meshes in both parsers.
            self.assertEqual(
                sorted(standard.materials), sorted(recorded.materials), name)
            for material_name, standard_material in standard.materials.items():
                recorded_material = recorded.materials[material_name]
                self.assertEqual(
                    recorded_material.vertices,
                    standard_material.vertices, name)
                self.assertEqual(
                    recorded_material.vertex_format,
                    standard_material.vertex_format, name)

    def test_collected_faces_match(self):
        fixtures = Path(__file__).parent / "fixtures"
        standard = Wavefront(str(fixtures / "arbitrary-faces.obj"),
                             collect_faces=True)
        recorded = record_scene(str(fixtures / "arbitrary-faces.obj"),
                                collect_faces=True)
        self.assertEqual([mesh.faces for mesh in standard.mesh_list],
                         [mesh.faces for mesh in recorded.mesh_list])


class TestProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = copy_fixture_tree()
        self.obj = self.tmp / "fixture.obj"
        self.scene = record_scene(
            str(self.obj),
            mtl_revisions={"red.mtl": "red-r1", "blue.mtl": "blue-r1"})

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_same_named_materials_keep_distinct_sources(self):
        registry = self.scene.material_registry
        self.assertEqual(sorted(registry),
                         ["blue.mtl::seal", "red.mtl::seal"])
        red = registry["red.mtl::seal"]
        blue = registry["blue.mtl::seal"]
        self.assertEqual(red.name, blue.name)
        self.assertIsNot(red, blue)
        self.assertEqual(red.source_file, "red.mtl")
        self.assertEqual(blue.source_file, "blue.mtl")
        self.assertEqual(red.mtl_revision, "red-r1")
        self.assertEqual(blue.mtl_revision, "blue-r1")
        self.assertAlmostEqual(red.diffuse[0], 0.8)
        self.assertAlmostEqual(blue.diffuse[2], 0.8)

    def test_material_occurrences_keep_line_and_group(self):
        events = self.scene.material_events
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["line_no"], 12)
        self.assertEqual(events[0]["mesh"], "RedPart")
        self.assertEqual(events[0]["material_key"], "red.mtl::seal")
        self.assertEqual(events[1]["line_no"], 25)
        self.assertEqual(events[1]["mesh"], "BluePart")
        self.assertEqual(events[1]["material_key"], "blue.mtl::seal")

    def test_face_provenance_records_raw_and_resolved_refs(self):
        blue = self.scene.groups["BluePart|blue.mtl::seal"]
        negative_face, positive_face = blue.triangles

        # Negative indices remember the raw token, not just coordinates
        self.assertEqual([ref.v.raw for ref in negative_face.refs],
                         [-3, -2, -1])
        self.assertTrue(all(ref.v.key is None for ref in negative_face.refs))
        self.assertEqual([ref.v.pos for ref in negative_face.refs],
                         [5, 6, 7])

        # Positive indices carry stable record identities
        self.assertEqual([ref.v.key for ref in positive_face.refs],
                         ["v:5", "v:6", "v:7"])
        self.assertEqual([ref.vt.key for ref in positive_face.refs],
                         ["vt:5", "vt:6", "vt:7"])
        self.assertEqual([ref.vn.key for ref in positive_face.refs],
                         ["vn:2", "vn:2", "vn:2"])

    def test_initial_scene_matches_full_reread(self):
        for mesh_name in ("RedPart", "BluePart"):
            self.assertEqual(
                mesh_buffer(self.scene, mesh_name),
                plain_material_vertices(self.obj, mesh_name))


class TestIncrementalPatch(unittest.TestCase):
    def setUp(self):
        self.tmp = copy_fixture_tree()
        self.obj = self.tmp / "fixture.obj"
        self.expected = self.tmp / "expected_after_vt.obj"

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _scene(self):
        return record_scene(
            str(self.obj),
            mtl_revisions={"red.mtl": "red-r1", "blue.mtl": "blue-r1"})

    def test_vt_insert_negative_indices_follow_pool_end(self):
        scene = self._scene()
        red_before = mesh_buffer(scene, "RedPart")

        apply_patch(
            scene,
            str(self.tmp / "manifest_blue_vt.json"),
            str(self.tmp / "supp_blue_vt.obj"))

        self.assertEqual(scene.current_revision, "r1")
        # New last texture coordinate is what -1 resolves to
        self.assertEqual(scene.pool_vt[-1], (5.0, 5.0))
        blue = scene.groups["BluePart|blue.mtl::seal"]
        self.assertEqual(
            [ref.vt.pos for ref in blue.triangles[0].refs], [5, 6, 7])
        self.assertEqual(
            [ref.vt.raw for ref in blue.triangles[0].refs], [-3, -2, -1])

        # Triangle expansion matches a full re-read of the patched file
        self.assertEqual(
            mesh_buffer(scene, "BluePart"),
            plain_material_vertices(self.expected, "BluePart"))

        # Geometry of the other object is reused untouched
        self.assertEqual(mesh_buffer(scene, "RedPart"), red_before)

    def test_positive_neighbour_face_is_not_shifted(self):
        scene = self._scene()
        apply_patch(
            scene,
            str(self.tmp / "manifest_blue_vt.json"),
            str(self.tmp / "supp_blue_vt.obj"))

        blue = scene.groups["BluePart|blue.mtl::seal"]
        neighbour = blue.triangles[1]
        # The explicit positive indices keep the original records:
        # vt #5..#7 and vn #2 must not be dragged to the new entries.
        self.assertEqual([ref.vt.pos for ref in neighbour.refs], [4, 5, 6])
        self.assertEqual([ref.vn.pos for ref in neighbour.refs], [1, 1, 1])
        self.assertEqual([ref.v.pos for ref in neighbour.refs], [4, 5, 6])
        self.assertEqual(
            mesh_buffer(scene, "BluePart"),
            plain_material_vertices(self.expected, "BluePart"))

    def test_vertex_insert_in_front_keeps_identities(self):
        scene = self._scene()
        original_blue = scene.groups["BluePart|blue.mtl::seal"]
        vertex_size = 8  # T2F_N3F_V3F
        neighbour_slice_before = list(original_blue.buffer[
            3 * vertex_size:6 * vertex_size])

        apply_patch(
            scene,
            str(self.tmp / "manifest_blue_v_insert.json"),
            str(self.tmp / "supp_blue_v_insert.obj"))

        blue = scene.groups["BluePart|blue.mtl::seal"]
        # The positive neighbour still resolves to the original v records,
        # now shifted one position forward by identity translation.
        pools = _load_pools(scene)
        neighbour = blue.triangles[1]
        self.assertEqual([ref.v.key for ref in neighbour.refs],
                         ["v:5", "v:6", "v:7"])
        self.assertEqual(
            [pools["v"].resolve_ref(ref.v) for ref in neighbour.refs],
            [5, 6, 7])
        # Its expanded floats are byte-for-byte the same as before the patch
        neighbour_slice_after = list(blue.buffer[
            3 * vertex_size:6 * vertex_size])
        self.assertEqual(neighbour_slice_after, neighbour_slice_before)

        # The negative face re-resolves relative to the pool end: with the
        # insert landing in front of the block, -3..-1 still name the last
        # three records of the pool.
        negative = blue.triangles[0]
        self.assertEqual([ref.v.raw for ref in negative.refs], [-3, -2, -1])
        self.assertEqual(
            [scene.vertices[ref.v.pos] for ref in negative.refs],
            [(3.0, 0.0, 0.0), (3.0, 1.0, 0.0), (2.0, 1.0, 0.0)])

        self.assertEqual(
            mesh_buffer(scene, "BluePart"),
            plain_material_vertices(
                self.tmp / "expected_after_v_insert.obj", "BluePart"))

        write_archive(scene, str(self.obj))
        reopened = load_archive(str(self.obj))
        self.assertEqual(
            mesh_buffer(reopened, "BluePart"),
            mesh_buffer(scene, "BluePart"))
        self.assertEqual(
            reopened.groups[blue.key].triangles, blue.triangles)

    def test_material_only_patch_reuses_geometry(self):
        scene = self._scene()
        apply_patch(
            scene,
            str(self.tmp / "manifest_blue_vt.json"),
            str(self.tmp / "supp_blue_vt.obj"))
        red_group = scene.groups["RedPart|red.mtl::seal"]
        buffer_before = list(red_group.buffer)
        identity_before = id(red_group.buffer)

        apply_patch(
            scene,
            str(self.tmp / "manifest_red_mtl.json"),
            str(self.tmp / "supp_red_mtl.obj"))

        red_group = scene.groups["RedPart|red.mtl::seal"]
        self.assertEqual(red_group.buffer, buffer_before)
        self.assertEqual(id(red_group.buffer), identity_before)

        red = scene.material_registry["red.mtl::seal"]
        self.assertEqual(red.mtl_revision, "red-r2")
        self.assertAlmostEqual(red.diffuse[0], 0.95)
        # The other material keeps its own revision and vertices
        blue = scene.material_registry["blue.mtl::seal"]
        self.assertEqual(blue.mtl_revision, "blue-r1")
        self.assertAlmostEqual(blue.diffuse[2], 0.8)
        self.assertEqual(
            blue.vertices,
            [v for key in scene.material_groups["blue.mtl::seal"]
             for v in scene.groups[key].buffer])

    def test_reverse_patch_order(self):
        scene = self._scene()
        red_before = mesh_buffer(scene, "RedPart")

        apply_patch(
            scene,
            str(self.tmp / "manifest_red_mtl_first.json"),
            str(self.tmp / "supp_red_mtl.obj"))
        self.assertEqual(
            scene.material_registry["red.mtl::seal"].mtl_revision, "red-r2")
        self.assertEqual(mesh_buffer(scene, "RedPart"), red_before)

        apply_patch(
            scene,
            str(self.tmp / "manifest_blue_vt_second.json"),
            str(self.tmp / "supp_blue_vt.obj"))

        self.assertEqual(scene.current_revision, "r2")
        self.assertEqual(
            mesh_buffer(scene, "BluePart"),
            plain_material_vertices(self.expected, "BluePart"))
        self.assertEqual(mesh_buffer(scene, "RedPart"), red_before)
        blue = scene.groups["BluePart|blue.mtl::seal"]
        self.assertEqual([ref.vt.pos for ref in blue.triangles[1].refs],
                         [4, 5, 6])

    def test_wrong_base_revision_is_rejected(self):
        scene = self._scene()
        with self.assertRaises(IncrementalPatchError):
            apply_patch(
                scene,
                str(self.tmp / "manifest_red_mtl.json"),
                str(self.tmp / "supp_red_mtl.obj"))

    def test_declared_range_must_match_affected_triangles(self):
        scene = self._scene()
        bad = dict(json.loads(
            (self.tmp / "manifest_blue_vt.json").read_text()))
        # Triangle 1 uses positive indices and is therefore not dirty
        bad["face_replacements"] = [{"start": 1, "count": 1}]
        bad_path = self.tmp / "bad_manifest.json"
        bad_path.write_text(json.dumps(bad))
        with self.assertRaises(IncrementalPatchError):
            apply_patch(
                scene, str(bad_path),
                str(self.tmp / "supp_blue_vt.obj"))

    def test_unknown_object_is_rejected(self):
        scene = self._scene()
        bad = dict(json.loads(
            (self.tmp / "manifest_blue_vt.json").read_text()))
        bad["object"] = "NoSuchPart"
        bad_path = self.tmp / "bad_manifest.json"
        bad_path.write_text(json.dumps(bad))
        with self.assertRaises(IncrementalPatchError):
            apply_patch(
                scene, str(bad_path),
                str(self.tmp / "supp_blue_vt.obj"))

    def test_rejected_patch_leaves_scene_untouched(self):
        scene = self._scene()
        blue_before = list(
            scene.groups["BluePart|blue.mtl::seal"].buffer)
        revision_before = scene.current_revision

        bad = dict(json.loads(
            (self.tmp / "manifest_blue_vt.json").read_text()))
        # Triangle 1 uses positive indices and is therefore not dirty
        bad["face_replacements"] = [{"start": 1, "count": 1}]
        bad_path = self.tmp / "bad_manifest.json"
        bad_path.write_text(json.dumps(bad))

        with self.assertRaises(IncrementalPatchError):
            apply_patch(
                scene, str(bad_path),
                str(self.tmp / "supp_blue_vt.obj"))

        self.assertEqual(scene.current_revision, revision_before)
        self.assertEqual(
            scene.groups["BluePart|blue.mtl::seal"].buffer, blue_before)
        self.assertEqual(len(scene.pool_vt), 7)

    def test_deleted_record_invalidates_positive_identity(self):
        scene = self._scene()
        # Delete vt record #7, which both blue faces reference (the positive
        # face through identity vt:7, the negative face relatively).
        manifest = {
            "patch_id": "p-del-vt7",
            "base_revision": "r0",
            "new_revision": "r1",
            "object": "BluePart",
            "data_changes": {
                "vt": [{"op": "delete", "start": 7, "count": 1}]
            },
            "face_replacements": [
                {"start": 0, "count": 1},
                {"start": 1, "count": 1},
            ],
        }
        (self.tmp / "del_manifest.json").write_text(json.dumps(manifest))
        (self.tmp / "supp_del.obj").write_text(
            "f -3/-3/-1 -2/-2/-1 -1/-1/-1\n"
            "f 5/5/2 6/6/2 7/6/2\n")

        apply_patch(
            scene,
            str(self.tmp / "del_manifest.json"),
            str(self.tmp / "supp_del.obj"))

        self.assertEqual(len(scene.pool_vt), 6)
        blue = scene.groups["BluePart|blue.mtl::seal"]
        self.assertEqual(len(blue.triangles), 2)
        # Replacement positive face now explicitly points at vt #6
        self.assertEqual(
            [ref.vt.key for ref in blue.triangles[1].refs],
            ["vt:5", "vt:6", "vt:6"])


class TestPool(unittest.TestCase):
    def test_insert_keeps_identity_positions_stable(self):
        pool = Pool("vt", [(0.0, 0.0), (1.0, 1.0)])
        pool.insert(3, [(2.0, 2.0)], "p1")
        self.assertEqual(
            pool.identities, ["vt:1", "vt:2", "p1:vt:1"])
        self.assertEqual(pool.position_of("vt:1"), 0)
        self.assertEqual(pool.position_of("vt:2"), 1)

    def test_insert_in_front_shifts_position_not_identity(self):
        pool = Pool("v", [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)])
        pool.insert(1, [(9.0, 9.0, 9.0)], "p1")
        self.assertEqual(pool.position_of("v:2"), 2)
        self.assertEqual(pool.values[2], (1.0, 1.0, 1.0))

    def test_delete_removes_identity(self):
        pool = Pool("vn", [(0.0, 0.0, 1.0), (0.0, 1.0, 0.0)])
        pool.delete(1, 1)
        self.assertFalse(pool.contains_identity("vn:1"))
        self.assertTrue(pool.contains_identity("vn:2"))

    def test_negative_ref_follows_pool_end(self):
        from pywavefront.incremental import Ref
        pool = Pool("vt", [(0.0, 0.0)])
        ref = Ref(-1, None, 0)
        self.assertEqual(pool.resolve_ref(ref), 0)
        pool.insert(2, [(1.0, 1.0)], "p1")
        self.assertEqual(pool.resolve_ref(ref), 1)


class TestArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = copy_fixture_tree()
        self.obj = self.tmp / "fixture.obj"
        self.expected = self.tmp / "expected_after_vt.obj"
        self.scene = record_scene(
            str(self.obj),
            mtl_revisions={"red.mtl": "red-r1", "blue.mtl": "blue-r1"})
        apply_patch(
            self.scene,
            str(self.tmp / "manifest_blue_vt.json"),
            str(self.tmp / "supp_blue_vt.obj"))
        apply_patch(
            self.scene,
            str(self.tmp / "manifest_red_mtl.json"),
            str(self.tmp / "supp_red_mtl.obj"))
        write_archive(self.scene, str(self.obj))

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_reopen_from_cache_matches_live_scene(self):
        reopened = load_archive(str(self.obj))
        self.assertEqual(reopened.current_revision, "r2")
        for key, group in self.scene.groups.items():
            cached = reopened.groups[key]
            self.assertEqual(cached.buffer, group.buffer)
            self.assertEqual(cached.triangles, group.triangles)
            self.assertEqual(cached.material_key, group.material_key)

        for mesh_name in ("RedPart", "BluePart"):
            self.assertEqual(
                mesh_buffer(reopened, mesh_name),
                mesh_buffer(self.scene, mesh_name))

        red = reopened.material_registry["red.mtl::seal"]
        blue = reopened.material_registry["blue.mtl::seal"]
        self.assertEqual(red.mtl_revision, "red-r2")
        self.assertEqual(blue.mtl_revision, "blue-r1")

    def test_reopened_cache_matches_full_reread(self):
        reopened = load_archive(str(self.obj))
        self.assertEqual(
            mesh_buffer(reopened, "BluePart"),
            plain_material_vertices(self.expected, "BluePart"))
        self.assertEqual(
            mesh_buffer(reopened, "RedPart"),
            plain_material_vertices(self.obj, "RedPart"))

    def test_historical_snapshots_keep_their_revisions(self):
        r0 = load_archive(str(self.obj), revision="r0")
        r1 = load_archive(str(self.obj), revision="r1")

        self.assertEqual(len(r0.pool_vt), 7)
        self.assertEqual(len(r1.pool_vt), 8)
        self.assertEqual(
            r0.material_registry["red.mtl::seal"].mtl_revision, "red-r1")
        self.assertEqual(
            r1.material_registry["red.mtl::seal"].mtl_revision, "red-r1")
        self.assertEqual(
            load_archive(str(self.obj)).material_registry[
                "red.mtl::seal"].mtl_revision,
            "red-r2")

        # r0 geometry is exactly the original full re-read
        self.assertEqual(
            mesh_buffer(r0, "BluePart"),
            plain_material_vertices(self.obj, "BluePart"))
        self.assertEqual(
            mesh_buffer(r0, "RedPart"),
            plain_material_vertices(self.obj, "RedPart"))

    def test_archived_material_occurrences_survive(self):
        reopened = load_archive(str(self.obj))
        keys = {event["material_key"] for event in reopened.material_events}
        self.assertEqual(keys, {"red.mtl::seal", "blue.mtl::seal"})
        mesh_materials = [
            (material.source_file, material.name)
            for mesh in reopened.mesh_list
            for material in mesh.materials]
        self.assertIn(("red.mtl", "seal"), mesh_materials)
        self.assertIn(("blue.mtl", "seal"), mesh_materials)


class TestParseSupplemental(unittest.TestCase):
    def test_parse_partial_obj(self):
        tmp = copy_fixture_tree()
        try:
            data = parse_supplemental(str(tmp / "supp_blue_vt.obj"))
            self.assertEqual(data.pool_records["vt"], [(5.0, 5.0)])
            self.assertEqual(
                data.face_lines,
                [["-3/-3/-1", "-2/-2/-1", "-1/-1/-1"]])
            self.assertEqual(data.usemtl, None)
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
