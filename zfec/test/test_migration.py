from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from zfec import migration


def base_manifest(**overrides):
    """Four numbers on k=2 safe disks; only old-a must be retired.

    Initial state survives loss of either old disk:
      lose old-a -> numbers 1,2,3 remain
      lose old-b -> numbers 0,2,3 remain
    """
    data = {
        "versions": [
            {"name": "obj", "original_length": 400, "k": 2, "m": 4},
        ],
        "disks": [
            {"name": "old-a", "available_bytes": 0},
            {"name": "old-b", "available_bytes": 0},
            {"name": "keep", "available_bytes": 400, "retired": False},
        ],
        "shares": [
            {"version": "obj", "number": 0, "disk": "old-a", "size": 100},
            {"version": "obj", "number": 1, "disk": "old-a", "size": 100},
            {"version": "obj", "number": 2, "disk": "keep", "size": 100},
            {"version": "obj", "number": 3, "disk": "keep", "size": 100},
        ],
        "failures": [
            {"name": "old-a", "lost_disks": ["old-a"]},
            {"name": "old-b", "lost_disks": ["old-b"]},
        ],
    }
    data.update(overrides)
    return data


class ManifestTest(unittest.TestCase):
    def test_rejects_missing_failure_coverage_at_start(self):
        data = base_manifest()
        # Losing a keep disk leaves numbers 0,1: still k=2, but losing old-a
        # leaves 1,2,3.  To make the start unsafe, remove two keep numbers.
        data["shares"] = data["shares"][:2]
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.PlanningError) as raised:
            migration.plan(manifest)
        self.assertTrue(raised.exception.diagnostics.reasons)

    def test_rejects_disagreeing_sizes(self):
        data = base_manifest()
        data["shares"][3] = {
            "version": "obj",
            "number": 3,
            "disk": "keep",
            "size": 101,
        }
        data["shares"].append(
            {"version": "obj", "number": 3, "disk": "old-b", "size": 100}
        )
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.ManifestError):
            migration.plan(manifest)

    def test_accepts_capacity_and_used(self):
        data = base_manifest()
        data["disks"][2] = {
            "name": "keep",
            "capacity": 500,
            "used_bytes": 100,
            "retired": False,
        }
        manifest = migration.manifest_from_dict(data)
        self.assertEqual(manifest.disks["keep"].available_bytes, 400)

    def test_accepts_role_fields_and_nested_shares(self):
        data = base_manifest()
        structured = {
            "versions": {"obj": {"original_length": 400, "k": 2, "m": 4}},
            "disks": {
                "old-a": {"available_bytes": 0, "role": "retire"},
                "old-b": {"available_bytes": 0, "role": "retire"},
                "keep": {"available_bytes": 400, "role": "keep"},
            },
            "shares": {
                "obj": [
                    {"number": 0, "disk": "old-a", "size": 100},
                    {"number": 1, "disk": "old-b", "size": 100},
                    {"number": 2, "disk": "keep", "size": 100},
                    {"number": 3, "disk": "keep", "size": 100},
                ]
            },
            "failures": {"old-a": ["old-a"], "old-b": ["old-b"]},
        }
        manifest = migration.manifest_from_dict(structured)
        self.assertFalse(manifest.disks["keep"].retired)
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)

    def test_rejects_unknown_scratch_disk(self):
        data = base_manifest()
        data["scratch_disks"] = ["nope"]
        with self.assertRaises(migration.ManifestError):
            migration.manifest_from_dict(data)

    def test_rejects_missing_share_size(self):
        data = base_manifest()
        del data["shares"][0]["size"]
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.ManifestError):
            migration.plan(manifest)


class PlanningTest(unittest.TestCase):
    def test_copy_precedes_delete_and_counts_bytes(self):
        manifest = migration.manifest_from_dict(base_manifest())
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)
        # Only share 0 sits on a retiring disk; it is copied once and then the
        # old copy is deleted.  Other shares are already on the keep disk.
        self.assertEqual(result.total_bytes_written, 200)
        self.assertEqual(
            [step.action.kind for step in result.steps],
            ["copy", "delete", "copy", "delete"],
        )

    def test_transient_copy_is_charged(self):
        # Share 0 is only on old-a. keep2 has one slot, so exactly one new copy
        # is written before old-a's copy is deleted.
        data = base_manifest()
        data["disks"] = [
            {"name": "old-a", "available_bytes": 0},
            {"name": "old-b", "available_bytes": 0},
            {"name": "keep", "available_bytes": 0, "retired": False},
            {"name": "keep2", "available_bytes": 100, "retired": False},
        ]
        data["shares"] = [
            {"version": "obj", "number": 0, "disk": "old-a", "size": 100},
            {"version": "obj", "number": 1, "disk": "keep", "size": 100},
            {"version": "obj", "number": 2, "disk": "keep", "size": 100},
        ]
        manifest = migration.manifest_from_dict(data)
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)
        self.assertEqual(result.total_bytes_written, 100)
        self.assertEqual(
            [step.action.kind for step in result.steps], ["copy", "delete"]
        )

    def test_preserve_all_retains_surplus_numbers(self):
        # k=1 so keep_k may discard number 0 entirely.  preserve_all must move
        # both numbers 0 and 1 off the old disk, writing 200 bytes.
        data = {
            "versions": [{"name": "v", "original_length": 300, "k": 1, "m": 3}],
            "disks": [
                {"name": "old", "available_bytes": 0},
                {"name": "safe", "available_bytes": 300, "retired": False},
            ],
            "shares": [
                {"version": "v", "number": 0, "disk": "old", "size": 100},
                {"version": "v", "number": 1, "disk": "old", "size": 100},
                {"version": "v", "number": 2, "disk": "safe", "size": 100},
            ],
            "failures": [{"name": "old", "lost_disks": ["old"]}],
        }
        manifest = migration.manifest_from_dict(data)
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)
        self.assertEqual(result.total_bytes_written, 200)
        self.assertEqual(
            [s.action.kind for s in result.steps],
            ["copy", "delete", "copy", "delete"],
        )

        data["policy"] = "keep_k"
        manifest = migration.manifest_from_dict(data)
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)
        # Number 2 alone satisfies k=1; both old copies may be dropped.
        self.assertEqual(result.total_bytes_written, 0)
        self.assertEqual(
            [s.action.kind for s in result.steps], ["delete", "delete"]
        )

    def test_swap_without_space_is_rejected_with_details(self):
        # Every share is on an old disk or a completely full keep disk. No
        # safety window and no space to create one.
        data = {
            "versions": [{"name": "v", "original_length": 300, "k": 2, "m": 3}],
            "disks": [
                {"name": "old-a", "available_bytes": 0},
                {"name": "old-b", "available_bytes": 0},
                {"name": "keep", "available_bytes": 0, "retired": False},
            ],
            "shares": [
                {"version": "v", "number": 0, "disk": "old-a", "size": 100},
                {"version": "v", "number": 1, "disk": "old-b", "size": 100},
                {"version": "v", "number": 2, "disk": "keep", "size": 100},
            ],
            "failures": [
                {"name": "old-a", "lost_disks": ["old-a"]},
                {"name": "old-b", "lost_disks": ["old-b"]},
            ],
        }
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.PlanningError) as raised:
            migration.plan(manifest)
        diag = raised.exception.diagnostics
        self.assertTrue(diag.reasons)
        self.assertIn("free", " ".join(diag.reasons + diag.capacity_conflicts))

    def test_swap_uses_order_to_avoid_failure_window(self):
        # k=2,m=3.  Losing old-a leaves numbers 1,2; losing old-b leaves 0,2.
        # Moving 0 A->B would leave a window with only 1,2 (still k=2, safe),
        # but B is full.  Copy 1 from B to the one free keep slot first, delete
        # it from B, then place 0 into B, then delete A.  This is a real
        # ordering problem, not a simple capacity problem.
        data = {
            "versions": [{"name": "v", "original_length": 300, "k": 2, "m": 3}],
            "disks": [
                {"name": "old-a", "available_bytes": 0},
                {"name": "old-b", "available_bytes": 0},
                {"name": "keep", "available_bytes": 100, "retired": False},
            ],
            "shares": [
                {"version": "v", "number": 0, "disk": "old-a", "size": 100},
                {"version": "v", "number": 1, "disk": "old-b", "size": 100},
                {"version": "v", "number": 2, "disk": "keep", "size": 100},
            ],
            "failures": [
                {"name": "old-a", "lost_disks": ["old-a"]},
                {"name": "old-b", "lost_disks": ["old-b"]},
            ],
        }
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.PlanningError) as raised:
            migration.plan(manifest)
        # Both old copies need a keep home; only 100 bytes are free.  A scratch
        # disk is required.  The diagnostic must say so instead of emitting a
        # half-migration.
        self.assertTrue(raised.exception.diagnostics.reasons)

    def test_scratch_must_end_empty(self):
        data = base_manifest()
        data["disks"].append({"name": "spare", "available_bytes": 100})
        data["scratch_disks"] = ["spare"]
        manifest = migration.manifest_from_dict(data)
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)
        final = result.steps[-1].copies if result.steps else {}
        for disks in final.values():
            self.assertNotIn("spare", disks)

    def test_impossible_version_is_named(self):
        data = base_manifest()
        data["shares"] = [
            {"version": "obj", "number": 0, "disk": "old-a", "size": 100},
            {"version": "obj", "number": 1, "disk": "old-a", "size": 100},
            {"version": "obj", "number": 2, "disk": "old-a", "size": 100},
        ]
        data["failures"] = [{"name": "old-a", "lost_disks": ["old-a"]}]
        data["disks"][2]["available_bytes"] = 0
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.PlanningError) as raised:
            migration.plan(manifest)
        self.assertIn("obj", raised.exception.diagnostics.blocked_versions)

    def test_no_plan_explains_initial_failure(self):
        data = base_manifest()
        data["shares"] = data["shares"][:3]
        data["failures"] = [
            {"name": "both-old", "lost_disks": ["old-a", "old-b"]},
        ]
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.PlanningError) as raised:
            migration.plan(manifest)
        reasons = " ".join(raised.exception.diagnostics.reasons)
        self.assertIn("both-old", reasons)

    def test_search_limit_is_explicit(self):
        data = base_manifest()
        data["disks"][2]["available_bytes"] = 100
        data["disks"].append({"name": "spare", "available_bytes": 200})
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.PlanningError) as raised:
            migration.plan(manifest, max_states=1)
        self.assertIn("exceeded", str(raised.exception))

    def test_remaining_numbers_reported_per_step(self):
        manifest = migration.manifest_from_dict(base_manifest())
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)
        for step in result.steps:
            self.assertIn("obj", step.remaining)
            self.assertGreaterEqual(step.remaining["obj"], 2)
            for failure_counts in step.remaining_by_failure.values():
                self.assertGreaterEqual(failure_counts.get("obj", 0), 2)


class PolicyAndDiagnosticsTest(unittest.TestCase):
    def test_preserve_all_needs_room_for_every_number(self):
        # k=2, m=3: all three numbers must survive. keep has room for one new
        # copy; two old numbers need homes, so this must be rejected.
        data = {
            "versions": [{"name": "v", "original_length": 300, "k": 2, "m": 3}],
            "disks": [
                {"name": "a", "available_bytes": 0},
                {"name": "b", "available_bytes": 0},
                {"name": "keep", "available_bytes": 100, "retired": False},
            ],
            "shares": [
                {"version": "v", "number": 0, "disk": "a", "size": 100},
                {"version": "v", "number": 1, "disk": "b", "size": 100},
                {"version": "v", "number": 2, "disk": "keep", "size": 100},
            ],
            "failures": [
                {"name": "a", "lost_disks": ["a"]},
                {"name": "b", "lost_disks": ["b"]},
            ],
        }
        manifest = migration.manifest_from_dict(data)
        with self.assertRaises(migration.PlanningError) as raised:
            migration.plan(manifest)
        reasons = " ".join(raised.exception.diagnostics.reasons)
        self.assertIn("v/0", reasons)
        self.assertIn("v/1", reasons)

        data["preserve_all"] = False
        manifest = migration.manifest_from_dict(data)
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)
        # One copy then one delete; the third number is surplus and dropped.
        self.assertEqual(result.total_bytes_written, 100)
        self.assertEqual(
            [s.action.kind for s in result.steps], ["copy", "delete", "delete"]
        )
        self.assertGreaterEqual(
            result.steps[-1].remaining["v"], 2
        )

    def test_allow_drop_can_delete_surplus_keep_number_to_make_room(self):
        # keepA and keepB both hold number 0 and are full. Numbers 1 and 2
        # are stranded on old. With k=2, one must survive on a keep disk. The
        # plan must drop the duplicate 0 from keepA to open a slot, copy 1
        # there, then drop the surplus 2.
        data = {
            "versions": [{"name": "v", "original_length": 300, "k": 2, "m": 3}],
            "disks": [
                {"name": "old", "available_bytes": 0},
                {"name": "keepA", "available_bytes": 0, "retired": False},
                {"name": "keepB", "available_bytes": 0, "retired": False},
            ],
            "shares": [
                {"version": "v", "number": 0, "disk": "keepA", "size": 100},
                {"version": "v", "number": 0, "disk": "keepB", "size": 100},
                {"version": "v", "number": 1, "disk": "old", "size": 100},
                {"version": "v", "number": 2, "disk": "old", "size": 100},
            ],
            "failures": [],
            "preserve_all": False,
        }
        manifest = migration.manifest_from_dict(data)
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)
        self.assertEqual(result.total_bytes_written, 100)
        kinds = [step.action.kind for step in result.steps]
        self.assertIn("copy", kinds)
        # At least one delete must come from a keep disk (the relief step).
        self.assertTrue(
            any(
                step.action.kind == "delete"
                and step.disk in ("keepA", "keepB")
                for step in result.steps
            )
        )
        self.assertGreaterEqual(result.steps[-1].remaining["v"], 2)

    def test_manifest_without_failure_scenarios_is_allowed(self):
        data = base_manifest()
        data["failures"] = []
        manifest = migration.manifest_from_dict(data)
        result = migration.plan(manifest)
        migration.verify_plan(manifest, result)

    def test_text_report_has_per_step_failure_counts(self):
        manifest = migration.manifest_from_dict(base_manifest())
        result = migration.plan(manifest)
        rendered = migration._render_text(manifest, result)
        self.assertIn("after failure 'old-a'", rendered)
        self.assertIn("distinct numbers", rendered)
        self.assertIn("Total bytes written", rendered)


class CliTest(unittest.TestCase):
    def _run(self, data, *args):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir="."
        ) as f:
            json.dump(data, f)
            path = f.name
        try:
            return subprocess.run(
                [sys.executable, "-m", "zfec.migration", path, *args],
                text=True,
                capture_output=True,
            )
        finally:
            os.unlink(path)

    def test_json_output(self):
        completed = self._run(base_manifest(), "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["feasible"])
        self.assertEqual(payload["total_bytes_written"], 200)
        first = payload["steps"][0]
        self.assertEqual(first["action"]["kind"], "copy")
        self.assertIn("remaining_distinct_shares_by_failure", first)
        self.assertEqual(first["remaining_distinct_shares"]["obj"], 4)

    def test_failure_output_explains_constraint(self):
        data = base_manifest()
        data["failures"] = [
            {"name": "all", "lost_disks": ["old-a", "old-b", "keep"]}
        ]
        completed = self._run(data, "--json")
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["feasible"])
        self.assertTrue(payload["reasons"])

    def test_bad_manifest_exit_code(self):
        completed = self._run({"versions": []}, "--json")
        self.assertEqual(completed.returncode, 2)

    def test_text_output_mentions_verification(self):
        completed = self._run(base_manifest())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("verify", completed.stdout)
        self.assertIn("Total bytes written: 200", completed.stdout)


if __name__ == "__main__":
    unittest.main()
