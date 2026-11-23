"""
Test cases for the ldaptor-ldifmerge console script.
"""

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

from twisted.trial import unittest

from ldaptor._scripts import ldifmerge
from ldaptor import changeset, inmemory
from twisted.internet import defer


BASE_LDIF = b"""\
version: 1

dn: dc=example,dc=com
objectClass: domain
dc: example

dn: ou=dept,dc=example,dc=com
objectClass: organizationalUnit
ou: dept

dn: cn=alice,ou=dept,dc=example,dc=com
objectClass: person
cn: alice
employeeNumber: E-1

dn: cn=bob,ou=dept,dc=example,dc=com
objectClass: person
cn: bob
employeeNumber: E-3

dn: ou=groups,dc=example,dc=com
objectClass: organizationalUnit
ou: groups

dn: cn=staff,ou=groups,dc=example,dc=com
objectClass: groupOfNames
cn: staff
member: cn=alice,ou=dept,dc=example,dc=com
member: cn=bob,ou=dept,dc=example,dc=com

"""

MOVE_LDIF = b"""\
version: 1

dn: ou=archive,dc=example,dc=com
changetype: add
objectClass: organizationalUnit
ou: archive

dn: ou=dept,dc=example,dc=com
changetype: moddn
newrdn: ou=dept
deleteoldrdn: 1
newsuperior: ou=archive,dc=example,dc=com

"""

CONFLICT_GROUP_LDIF = b"""\
version: 1

dn: cn=staff,ou=groups,dc=example,dc=com
changetype: modify
delete: member
member: cn=bob,ou=dept,dc=example,dc=com
-

"""

# The tree team also re-adds bob to the same group. Whether bob survives
# then depends on which team's change is applied first.
TREE_WITH_MEMBER_EDIT_LDIF = MOVE_LDIF + b"""\
dn: cn=staff,ou=groups,dc=example,dc=com
changetype: modify
add: member
member: cn=bob,ou=dept,dc=example,dc=com
-

"""

ADD_MEMBER_LDIF = b"""\
version: 1

dn: cn=staff,ou=groups,dc=example,dc=com
changetype: modify
add: member
member: cn=alice,ou=dept,dc=example,dc=com
-

"""


class LdifMergeScriptTests(unittest.TestCase):
    def _write(self, name, content):
        path = os.path.join(self.mktemp(), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def setUp(self):
        self.base = self._write("base.ldif", BASE_LDIF)
        self.move = self._write("move.ldif", MOVE_LDIF)
        self.tree_with_edit = self._write(
            "tree_edit.ldif", TREE_WITH_MEMBER_EDIT_LDIF
        )
        self.conflict_group = self._write(
            "conflict.ldif", CONFLICT_GROUP_LDIF
        )
        self.add_member = self._write("add.ldif", ADD_MEMBER_LDIF)

    def _run_script(self, *args):
        argv = ["ldaptor-ldifmerge", *args]
        output = io.StringIO()
        error = io.StringIO()
        import sys

        saved_argv = sys.argv
        sys.argv = argv
        try:
            with redirect_stdout(output), redirect_stderr(error):
                try:
                    ldifmerge.console_script()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
        finally:
            sys.argv = saved_argv
        return code, output.getvalue(), error.getvalue()

    def test_conflict_text_report_exit_code(self):
        code, output, error = self._run_script(
            "--label",
            "tree,groups",
            self.base,
            self.tree_with_edit,
            self.conflict_group,
        )
        self.assertEqual(code, 3)
        self.assertEqual(error, "")
        self.assertIn("disagreement", output)
        self.assertIn("cn=staff,ou=groups,dc=example,dc=com", output)
        self.assertIn("tree[", output)
        self.assertIn("groups[", output)
        # The competing member is shown at the moved, real address.
        self.assertIn("cn=bob,ou=dept,ou=archive,dc=example,dc=com", output)
        self.assertIn("Held back", output)

    def test_conflict_json_report(self):
        code, output, _ = self._run_script(
            "--json", self.base, self.tree_with_edit, self.conflict_group
        )
        self.assertEqual(code, 3)
        payload = json.loads(output)
        self.assertFalse(payload["mergeable"])
        self.assertEqual(payload["conflicts"][0]["kind"], "member")
        outcomes = payload["conflicts"][0]["outcomes"]
        self.assertEqual(len(outcomes), 2)
        for targets in outcomes.values():
            for target in targets:
                self.assertIn("identity", target)
                self.assertIn("dn", target)

    def test_clean_merge_exit_code_zero(self):
        code, output, error = self._run_script(
            self.base, self.move, self.add_member
        )
        self.assertEqual(code, 0, error)
        self.assertIn("merge cleanly", output)

    def test_safe_ldif_is_written(self):
        safe_dir = self.mktemp()
        os.makedirs(safe_dir, exist_ok=True)
        safe_path = os.path.join(safe_dir, "safe.ldif")
        code, _, error = self._run_script(
            "--safe-ldif",
            safe_path,
            self.base,
            self.tree_with_edit,
            self.conflict_group,
        )
        self.assertEqual(code, 3)
        self.assertTrue(os.path.exists(safe_path))
        with open(safe_path, "rb") as handle:
            content = handle.read()
        # The uncontested move lands in the safe tree, and member values
        # follow the moved entries.
        self.assertIn(
            b"dn: cn=alice,ou=dept,ou=archive,dc=example,dc=com", content
        )
        self.assertIn(
            b"member: cn=alice,ou=dept,ou=archive,dc=example,dc=com", content
        )
        # The contested bob add/delete pair is held, so staff keeps exactly
        # its two base members -- both rewritten to the post-move addresses.
        self.assertIn(
            b"member: cn=alice,ou=dept,ou=archive,dc=example,dc=com", content
        )
        self.assertEqual(content.count(b"member:"), 2)
        # The safe LDIF must itself be re-readable as a directory.
        with open(safe_path, "rb") as handle:
            root = ldifmerge._sync(inmemory.fromLDIFFile(handle))
        lookup = root.lookup("cn=alice,ou=dept,ou=archive,dc=example,dc=com")
        self.assertIsInstance(
            ldifmerge._sync(lookup), inmemory.ReadOnlyInMemoryLDAPEntry
        )

    def test_bad_label_count_fails(self):
        code, _, error = self._run_script(
            "--label", "only-one", self.base, self.move, self.add_member
        )
        self.assertEqual(code, 1)
        self.assertIn("two comma separated names", error)


class RendererUnitTests(unittest.TestCase):
    def test_identity_text_rendering(self):
        self.assertEqual(
            ldifmerge._identity_to_text(("id", "E-7")), "identity E-7"
        )
        self.assertEqual(
            ldifmerge._identity_to_text(("dn", "cn=x,dc=example,dc=com")),
            "entry cn=x,dc=example,dc=com",
        )

    def test_sync_unwraps_immediate_result_and_failure(self):
        result = ldifmerge._sync(defer.succeed({"ok": True}))
        self.assertEqual(result, {"ok": True})

        failed = defer.fail(ValueError("boom"))
        with self.assertRaises(ValueError):
            ldifmerge._sync(failed)
        # Consume the failure so trial does not report it as an unhandled
        # error on an unrelated test.
        failed.addErrback(lambda reason: None)


class PreviewConstructionTest(unittest.TestCase):
    def test_preview_requires_two_change_sets(self):
        root = inmemory.ReadOnlyInMemoryLDAPEntry(
            "dc=example,dc=com", {"objectClass": ["domain"]}
        )
        single = [changeset.ChangeSet("A", [])]
        d = changeset.preview_merge(root, single)
        return self.assertFailure(d, ValueError)
