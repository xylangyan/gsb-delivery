"""
Test cases for ldaptor.changeset -- two-team merge preview.
"""

from twisted.internet import defer
from twisted.trial import unittest

from ldaptor import changeset, delta, inmemory
from ldaptor.entry import BaseLDAPEntry


def _person(cn, employee_number, extra=None):
    attributes = {
        "objectClass": ["person"],
        "cn": [cn],
        "employeeNumber": [employee_number],
    }
    if extra:
        attributes.update(extra)
    return attributes


class MergePreviewFixture(unittest.TestCase):
    def setUp(self):
        self.root = inmemory.ReadOnlyInMemoryLDAPEntry(
            "dc=example,dc=com",
            {"objectClass": ["domain"], "dc": ["example"]},
        )
        self.dept = self.root.addChild(
            "ou=dept",
            {"objectClass": ["organizationalUnit"], "ou": ["dept"]},
        )
        self.alice = self.dept.addChild(
            "cn=alice", _person("alice", "E-1")
        )
        self.bob = self.dept.addChild("cn=bob", _person("bob", "E-3"))
        self.dave = self.dept.addChild("cn=dave", _person("dave", "E-4"))
        self.groups = self.root.addChild(
            "ou=groups",
            {"objectClass": ["organizationalUnit"], "ou": ["groups"]},
        )
        self.staff = self.groups.addChild(
            "cn=staff",
            {
                "objectClass": ["groupOfNames"],
                "cn": ["staff"],
                "member": [
                    b"cn=alice,ou=dept,dc=example,dc=com",
                    b"cn=bob,ou=dept,dc=example,dc=com",
                    b"cn=dave,ou=dept,dc=example,dc=com",
                ],
            },
        )
        self.archive = self.root.addChild(
            "ou=archive",
            {"objectClass": ["organizationalUnit"], "ou": ["archive"]},
        )

    def _move_dept(self):
        return delta.MoveOp(
            "ou=dept,dc=example,dc=com",
            "ou=dept,ou=archive,dc=example,dc=com",
        )

    @defer.inlineCallbacks
    def _preview(self, operations_a, operations_b):
        preview = yield changeset.preview_merge(
            self.root,
            [
                changeset.ChangeSet("tree", operations_a),
                changeset.ChangeSet("groups", operations_b),
            ],
        )
        defer.returnValue(preview)


class CleanMergeTests(MergePreviewFixture):
    @defer.inlineCallbacks
    def test_independent_changes_merge_cleanly(self):
        move = self._move_dept()
        # The groups team adds an existing member again: idempotent and
        # order independent once references follow the moved entry.
        add_alice = delta.ModifyOp(
            "cn=staff,ou=groups,dc=example,dc=com",
            [delta.Add("member", [b"cn=alice,ou=dept,dc=example,dc=com"])],
        )
        preview = yield self._preview([move], [add_alice])

        self.assertTrue(preview.mergeable)
        self.assertEqual(preview.conflicts, [])
        self.assertEqual(preview.warnings, [])

        staff = yield preview._safe_root.lookup(
            "cn=staff,ou=groups,dc=example,dc=com"
        )
        self.assertEqual(
            {v.decode() for v in staff["member"]},
            {
                "cn=alice,ou=dept,ou=archive,dc=example,dc=com",
                "cn=bob,ou=dept,ou=archive,dc=example,dc=com",
                "cn=dave,ou=dept,ou=archive,dc=example,dc=com",
            },
        )

    @defer.inlineCallbacks
    def test_member_follows_moved_subtree(self):
        preview = yield self._preview([self._move_dept()], [])

        moved_alice = yield preview._safe_root.lookup(
            "cn=alice,ou=dept,ou=archive,dc=example,dc=com"
        )
        self.assertEqual(moved_alice["employeeNumber"], {b"E-1"})

        staff = yield preview._safe_root.lookup(
            "cn=staff,ou=groups,dc=example,dc=com"
        )
        members = {v.decode() for v in staff["member"]}
        self.assertIn(
            "cn=alice,ou=dept,ou=archive,dc=example,dc=com", members
        )
        # No stale reference to the pre-move address remains.
        self.assertNotIn("cn=alice,ou=dept,dc=example,dc=com", members)

    @defer.inlineCallbacks
    def test_dn_shaped_non_member_attribute_stays_plain(self):
        # seeAlso merely looks like a DN; it is not a configured member
        # attribute and must be left as an ordinary value.
        self.alice["seeAlso"] = [b"cn=alice,ou=dept,dc=example,dc=com"]
        preview = yield self._preview([self._move_dept()], [])

        moved_alice = yield preview._safe_root.lookup(
            "cn=alice,ou=dept,ou=archive,dc=example,dc=com"
        )
        self.assertEqual(
            list(moved_alice["seeAlso"]),
            [b"cn=alice,ou=dept,dc=example,dc=com"],
        )

    @defer.inlineCallbacks
    def test_base_tree_is_not_mutated(self):
        yield self._preview([self._move_dept()], [])

        original = yield self.root.lookup(
            "cn=alice,ou=dept,dc=example,dc=com"
        )
        self.assertEqual(
            original.dn.getText(), "cn=alice,ou=dept,dc=example,dc=com"
        )


class OccupancyTests(MergePreviewFixture):
    @defer.inlineCallbacks
    def test_supplement_keeps_base_identity_after_address_reused(self):
        new_dept = delta.AddOp(
            BaseLDAPEntry(
                "ou=dept,dc=example,dc=com",
                {"objectClass": ["organizationalUnit"], "ou": ["dept"]},
            )
        )
        # A brand new employee is registered at the address the moved
        # alice used to occupy.
        newcomer = delta.AddOp(
            BaseLDAPEntry(
                "cn=alice,ou=dept,dc=example,dc=com",
                _person("alice", "E-2"),
            )
        )
        # The other team still references the base-draft identity via the
        # old address.
        supplement = delta.ModifyOp(
            "cn=staff,ou=groups,dc=example,dc=com",
            [delta.Add("member", [b"cn=alice,ou=dept,dc=example,dc=com"])],
        )

        preview = yield self._preview(
            [self._move_dept(), new_dept, newcomer], [supplement]
        )

        self.assertTrue(preview.mergeable, preview.conflicts)
        staff = yield preview._safe_root.lookup(
            "cn=staff,ou=groups,dc=example,dc=com"
        )
        members = {v.decode() for v in staff["member"]}
        # The supplement points at the actually-moved person (E-1), not at
        # the new employee E-2 now sitting at the old address.
        self.assertIn(
            "cn=alice,ou=dept,ou=archive,dc=example,dc=com", members
        )
        self.assertNotIn("cn=alice,ou=dept,dc=example,dc=com", members)

        old_address = yield preview._safe_root.lookup(
            "cn=alice,ou=dept,dc=example,dc=com"
        )
        # Added during replay, so the value keeps its original str flavor.
        self.assertEqual(old_address["employeeNumber"], {"E-2"})


class ConflictTests(MergePreviewFixture):
    def _tree_changes(self):
        new_dept = delta.AddOp(
            BaseLDAPEntry(
                "ou=dept,dc=example,dc=com",
                {"objectClass": ["organizationalUnit"], "ou": ["dept"]},
            )
        )
        carol = delta.AddOp(
            BaseLDAPEntry(
                "cn=carol,ou=dept,dc=example,dc=com",
                _person("carol", "E-2"),
            )
        )
        tree_edit = delta.ModifyOp(
            "cn=staff,ou=groups,dc=example,dc=com",
            [
                delta.Delete(
                    "member", [b"cn=dave,ou=dept,dc=example,dc=com"]
                ),
                delta.Add(
                    "member", [b"cn=bob,ou=dept,dc=example,dc=com"]
                ),
            ],
        )
        return [self._move_dept(), new_dept, carol, tree_edit]

    def _group_changes(self):
        # Different spelling of the same DN must track the same address.
        add_alice = delta.ModifyOp(
            "cn=staff,ou=groups,dc=example,dc=com",
            [
                delta.Add(
                    "member",
                    [b"CN=alice,OU=dept,DC=example,DC=com"],
                )
            ],
        )
        delete_bob = delta.ModifyOp(
            "cn=staff,ou=groups,dc=example,dc=com",
            [
                delta.Delete(
                    "member", [b"cn=bob,ou=dept,dc=example,dc=com"]
                )
            ],
        )
        return [add_alice, delete_bob]

    @defer.inlineCallbacks
    def test_order_dependent_membership_is_reported(self):
        preview = yield self._preview(
            self._tree_changes(), self._group_changes()
        )

        self.assertFalse(preview.mergeable)
        member_conflicts = preview.member_conflicts()
        self.assertEqual(len(member_conflicts), 1)
        conflict = member_conflicts[0]
        self.assertEqual(conflict.attribute, "member")
        self.assertEqual(
            conflict.base_dn, "cn=staff,ou=groups,dc=example,dc=com"
        )

        labels = {(change.label, change.index) for change in conflict.competing}
        self.assertEqual(
            labels,
            {("tree", 3), ("groups", 0), ("groups", 1)},
        )

        ordering_ab = "tree -> groups"
        ordering_ba = "groups -> tree"
        identities = {
            ordering: {
                target.employee_number for target in targets
            }
            for ordering, targets in conflict.outcomes.items()
        }
        self.assertEqual(identities[ordering_ab], {"E-1"})
        self.assertEqual(identities[ordering_ba], {"E-1", "E-3"})

        # Every reported person is shown at its actual (post-move) address.
        for targets in conflict.outcomes.values():
            for target in targets:
                self.assertIn(
                    "ou=dept,ou=archive,dc=example,dc=com", target.dn
                )
                self.assertFalse(target.dangling)

    @defer.inlineCallbacks
    def test_dn_spelling_variants_track_same_address(self):
        book = yield changeset._AddressBook.seed(
            self.root, changeset.DEFAULT_IDENTITY_ATTRIBUTE
        )
        self.assertEqual(
            book.resolve(b"CN=alice,OU=dept,DC=example,DC=com"),
            book.resolve(b"cn=alice,ou=dept,dc=example,dc=com"),
        )
        self.assertEqual(
            changeset.dn_key(b"CN=Alice,OU=Dept,DC=Example,DC=COM"),
            changeset.dn_key("cn=alice,ou=dept,dc=example,dc=com"),
        )

    @defer.inlineCallbacks
    def test_safe_subset_excludes_contested_operations(self):
        preview = yield self._preview(
            self._tree_changes(), self._group_changes()
        )

        safe = {(label, index) for label, index, _ in preview.safe_operations}
        # Structural and unrelated additions are safe ...
        self.assertEqual(
            safe,
            {("tree", 0), ("tree", 1), ("tree", 2)},
        )
        # ... the contested group edits and their targets are held.
        held = set(preview.held_identities)
        self.assertIn(("id", "E-1"), held)
        self.assertIn(("id", "E-3"), held)
        self.assertIn(
            ("dn", "cn=staff,ou=groups,dc=example,dc=com"), held
        )

    @defer.inlineCallbacks
    def test_safe_tree_keeps_real_hierarchy_and_rewritten_members(self):
        preview = yield self._preview(
            self._tree_changes(), self._group_changes()
        )

        # The moved subtree and the re-created department are both present,
        # so hierarchy and the original case identities are re-readable.
        moved = yield preview._safe_root.lookup(
            "cn=alice,ou=dept,ou=archive,dc=example,dc=com"
        )
        self.assertEqual(moved["employeeNumber"], {b"E-1"})
        carol = yield preview._safe_root.lookup(
            "cn=carol,ou=dept,dc=example,dc=com"
        )
        self.assertEqual(carol["employeeNumber"], {"E-2"})

        # Contested group edits are absent, but the surviving member values
        # still point at the entries' real post-move addresses.
        staff = yield preview._safe_root.lookup(
            "cn=staff,ou=groups,dc=example,dc=com"
        )
        self.assertEqual(
            {v.decode() for v in staff["member"]},
            {
                "cn=alice,ou=dept,ou=archive,dc=example,dc=com",
                "cn=bob,ou=dept,ou=archive,dc=example,dc=com",
                "cn=dave,ou=dept,ou=archive,dc=example,dc=com",
            },
        )


class WarningTests(MergePreviewFixture):
    @defer.inlineCallbacks
    def test_modification_of_missing_entry_is_warning_not_crash(self):
        ghost = delta.ModifyOp(
            "cn=ghost,ou=groups,dc=example,dc=com",
            [delta.Add("member", [b"cn=alice,ou=dept,dc=example,dc=com"])],
        )
        preview = yield self._preview([], [ghost])

        self.assertTrue(preview.warnings)
        self.assertTrue(
            any("missing entry" in message for message in preview.warnings)
        )
        # The failed operation is recorded as a conflict so it cannot be
        # silently treated as an agreed change.
        self.assertTrue(
            any(conflict.kind == "presence" for conflict in preview.conflicts)
        )


class AddressBookTests(unittest.TestCase):
    def test_move_keeps_old_address_as_alias_until_reused(self):
        book = changeset._AddressBook("employeeNumber")
        book.on_add(
            ("id", "E-1"),
            "cn=alice,ou=dept,dc=example,dc=com",
        )
        book.on_move(
            "cn=alice,ou=dept,dc=example,dc=com",
            "cn=alice,ou=archive,dc=example,dc=com",
        )
        # The historical address still resolves to the moved identity.
        self.assertEqual(
            book.resolve("cn=alice,ou=dept,dc=example,dc=com"),
            ("id", "E-1"),
        )
        self.assertEqual(
            book.current_dn(("id", "E-1")).getText(),
            "cn=alice,ou=archive,dc=example,dc=com",
        )

        # Reusing the address redirects it to the new identity.
        book.on_add(
            ("id", "E-2"),
            "cn=alice,ou=dept,dc=example,dc=com",
        )
        self.assertEqual(
            book.resolve("cn=alice,ou=dept,dc=example,dc=com"),
            ("id", "E-2"),
        )
        self.assertEqual(
            book.current_dn(("id", "E-1")).getText(),
            "cn=alice,ou=archive,dc=example,dc=com",
        )

    def test_subtree_move_rewrites_descendant_addresses(self):
        book = changeset._AddressBook("employeeNumber")
        book.on_add(("id", "E-1"), "cn=alice,ou=dept,dc=example,dc=com")
        book.on_move(
            "ou=dept,dc=example,dc=com",
            "ou=dept,ou=archive,dc=example,dc=com",
        )
        self.assertEqual(
            book.current_dn(("id", "E-1")).getText(),
            "cn=alice,ou=dept,ou=archive,dc=example,dc=com",
        )
