"""
Preview whether two change sets built on the same LDIF base can be merged.

A change set is an ordered list of :mod:`ldaptor.delta` operations parsed
from an LDIF delta file. Two teams work on the same base draft:

* one team moves whole subtrees (``modrdn`` / ``moddn``);
* the other team maintains group memberships.

People carry a stable identity attribute (by default ``employeeNumber``).
Only the configured member attributes (by default ``member`` and
``uniqueMember``) treat their values as distinguished names that refer to
real entries and must follow those entries when they move. Any other
attribute that merely looks like a DN is kept as an ordinary plain value.

The preview never silently picks a winner. It replays both change sets in
both order-preserving interleavings (A then B, and B then A) and reports
every entry whose effective content depends on which change set is applied
first, including the concrete competing changes and the persons each
outcome points at. Entries that are not part of any disagreement form a
safe subset that can be handed to the directory before the contested
entries are confirmed.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from io import BytesIO

from twisted.internet import defer

from ldaptor import attributeset, delta, inmemory
from ldaptor._encoder import to_unicode
from ldaptor.protocols.ldap import distinguishedname

DEFAULT_IDENTITY_ATTRIBUTE = "employeeNumber"
DEFAULT_MEMBER_ATTRIBUTES = ("member", "uniqueMember")


def _dn(dn_or_text):
    if isinstance(dn_or_text, distinguishedname.DistinguishedName):
        return dn_or_text
    return distinguishedname.DistinguishedName(stringValue=to_unicode(dn_or_text))


def dn_key(dn_or_text):
    """
    Canonical address for a DN value.

    Two spellings that this library parses into the same distinguished
    name (for example different escaping or letter case) get the same key,
    so references are tracked as one and the same address.
    """
    return _dn(dn_or_text).getText().lower()


def _attr_key(attribute):
    return to_unicode(attribute).lower()


def _like(value, text):
    """Return ``text`` with the same str/bytes flavor as ``value``."""
    if isinstance(value, bytes):
        return text.encode("utf-8")
    return text


def _member_key_set(member_attributes):
    return {_attr_key(attribute) for attribute in member_attributes}


@dataclass
class ChangeSet:
    """An ordered bundle of operations handed in by one team."""

    label: str
    operations: list

    def __iter__(self):
        return iter(self.operations)

    def __len__(self):
        return len(self.operations)


class _AddressBook:
    """
    Tracks where every known entry currently lives.

    Identity keys are stable across renames:

    * entries carrying the identity attribute are keyed by its value;
    * other base entries get a synthetic key derived from their base DN;
    * entries added by a change set get a synthetic key derived from
      their new DN, independent of which change set added them.
    """

    def __init__(self, identity_attribute):
        self.identity_attribute = _attr_key(identity_attribute)
        self._ident_to_dn = {}
        self._dn_to_ident = {}

    @classmethod
    @defer.inlineCallbacks
    def seed(cls, root, identity_attribute):
        book = cls(identity_attribute)
        entries = yield root.subtree()
        for entry in entries:
            book._register(book._identity_for(entry.dn, entry), entry.dn)
        defer.returnValue(book)

    def _identity_for(self, dn, entry):
        values = entry.get(self.identity_attribute, ())
        if values:
            return ("id", to_unicode(sorted(values)[0]))
        return ("dn", dn_key(dn))

    @staticmethod
    def add_identity(dn):
        return ("add", dn_key(dn))

    def _register(self, ident, dn):
        dn = _dn(dn)
        self._ident_to_dn[ident] = dn
        self._dn_to_ident[dn_key(dn)] = ident

    def resolve(self, value):
        """Return the identity a DN value refers to, or None if dangling."""
        try:
            key = dn_key(value)
        except distinguishedname.InvalidRelativeDistinguishedName:
            return None
        return self._dn_to_ident.get(key)

    def current_dn(self, ident):
        return self._ident_to_dn.get(ident)

    def on_add(self, ident, dn):
        # Claiming an address overrides any alias left behind by a move:
        # once a new employee is registered at an old DN, that DN refers to
        # the new identity and no longer to whoever used to live there.
        self._register(ident, dn)

    def on_delete(self, dn):
        dn = _dn(dn)
        gone = [
            ident
            for ident, current in self._ident_to_dn.items()
            if current == dn or dn.contains(current)
        ]
        for ident in gone:
            current = self._ident_to_dn.pop(ident)
            aliases = [
                address
                for address, owner in self._dn_to_ident.items()
                if owner == ident
            ]
            for address in aliases:
                self._dn_to_ident.pop(address, None)
            self._dn_to_ident.pop(dn_key(current), None)

    def on_move(self, old_dn, new_dn):
        old_dn = _dn(old_dn)
        new_dn = _dn(new_dn)
        suffix_len = len(old_dn.split())
        for ident, current in list(self._ident_to_dn.items()):
            if not (current == old_dn or old_dn.contains(current)):
                continue
            if current == old_dn:
                moved_dn = new_dn
            else:
                relative = current.split()[:-suffix_len]
                moved_dn = distinguishedname.DistinguishedName(
                    listOfRDNs=relative + new_dn.split()
                )
            # Keep the old address as an alias so pre-existing member
            # values can still follow the moved identity. The alias is
            # overridden as soon as another entry claims that address.
            self._ident_to_dn[ident] = moved_dn
            self._dn_to_ident[dn_key(moved_dn)] = ident


@dataclass
class MemberTarget:
    """A person a member value effectively points at in one result tree."""

    identity: tuple
    dn: str
    employee_number: str = None
    dangling: bool = False


@dataclass
class CompetingChange:
    """One change record involved in a disagreement."""

    label: str
    index: int
    ldif: str

    def describe(self):
        return f"{self.label}[{self.index}]\n{self.ldif}"


@dataclass
class Conflict:
    """
    A disagreement between the two order-preserving interleavings.

    ``kind`` is ``"member"`` for order-dependent group memberships,
    ``"attribute"`` for other attribute edits, ``"location"`` for entries
    ending up in different places and ``"presence"`` for entries existing
    in only one of the two results.
    """

    kind: str
    identity: tuple
    base_dn: str
    detail: str
    attribute: str = None
    competing: list = field(default_factory=list)
    outcomes: dict = field(default_factory=dict)


@dataclass
class MergePreview:
    change_sets: list
    conflicts: list
    safe_operations: list
    held_identities: list
    warnings: list
    _safe_root: object

    @property
    def mergeable(self):
        return not self.conflicts

    @defer.inlineCallbacks
    def safe_tree_as_ldif(self):
        entries = yield self._safe_root.subtree()
        defer.returnValue(b"".join(entry.toWire() for entry in entries))

    def member_conflicts(self):
        return [conflict for conflict in self.conflicts if conflict.kind == "member"]


def _clone_root(root):
    d = root.subtree()

    def got_entries(entries):
        buf = BytesIO(b"".join(entry.toWire() for entry in entries))
        return inmemory.fromLDIFFile(buf)

    d.addCallback(got_entries)
    return d


def _is_member_attribute(key, member_attributes):
    return _attr_key(key) in _member_key_set(member_attributes)


def _authored_identity(base_book, live_book, value):
    """
    Resolve a DN written in a change set to a stable identity.

    Change sets are authored against the base draft, so the base address
    book is consulted first. This keeps a reference pointing at the
    identity that used to occupy an address even after a different entry
    has since been registered there. Only when the base does not know the
    address (for example an entry added during replay) is the live book
    consulted.
    """
    identity = base_book.resolve(value)
    if identity is None:
        identity = live_book.resolve(value)
    return identity


def _current_equivalent(live_book, dn):
    """
    Translate a placement DN into its current address using the live tree.

    Used for where a brand-new or moved entry is placed: the address must
    refer to whatever currently occupies it, not to a historical alias left
    behind by a move. The DN itself, and every superior that has since
    moved, is rewritten; unknown superiors are left untouched.
    """
    dn = _dn(dn)
    rdns = list(dn.split())
    for index in range(len(rdns)):
        superior = distinguishedname.DistinguishedName(listOfRDNs=rdns[index + 1 :])
        identity = live_book.resolve(superior)
        if identity is not None:
            current_superior = live_book.current_dn(identity)
            rewritten_rdns = rdns[: index + 1] + list(current_superior.split())
            return distinguishedname.DistinguishedName(listOfRDNs=rewritten_rdns)
    return dn


def _rewrite_modification(
    op, base_book, live_book, member_attributes, target_entry=None
):
    """
    Rewrite member values of a ModifyOp to the entries' current DNs.

    Non-member attributes are not touched, however DN-shaped their values
    look. When the target entry already stores the attribute, rewritten
    values adopt its str/bytes flavor so the attribute set never mixes
    the two (mixing breaks value sorting later).
    """
    for modification in op.modifications:
        if not _is_member_attribute(modification.key, member_attributes):
            continue
        stored = target_entry.get(modification.key) if target_entry else None
        flavor_sample = next(iter(stored), None) if stored is not None else None
        rewritten = attributeset.LDAPAttributeSet(modification.key)
        for value in modification:
            identity = _authored_identity(base_book, live_book, value)
            if identity is None:
                text = to_unicode(value)
            else:
                text = live_book.current_dn(identity).getText()
            if flavor_sample is not None:
                rewritten.add(_like(flavor_sample, text))
            else:
                rewritten.add(_like(value, text))
        modification.clear()
        modification.update(rewritten)


@defer.inlineCallbacks
def _rewrite_existing_members(root, book, member_attributes):
    entries = yield root.subtree()
    for entry in entries:
        for attribute in member_attributes:
            values = entry.get(attribute)
            if values is None:
                continue
            # Mutate the stored attribute set in place. Replacing it via
            # entry[attribute] would store the value under a different
            # str/bytes spelling of the key than the one delta operations
            # use when they look the attribute back up.
            rewritten = []
            changed = False
            for value in values:
                identity = book.resolve(value)
                if identity is None:
                    rewritten.append(value)
                    continue
                current = book.current_dn(identity)
                if dn_key(current) != dn_key(value):
                    changed = True
                rewritten.append(_like(value, current.getText()))
            if changed:
                values.clear()
                values.update(rewritten)


@defer.inlineCallbacks
def _apply(
    change_set,
    operations,
    root,
    base_book,
    book,
    member_attributes,
    identity_attribute,
    errors,
):
    for index, original in enumerate(operations):
        op = deepcopy(original)
        label = change_set.label
        try:
            if isinstance(op, delta.MoveOp):
                identity = _authored_identity(base_book, book, op.dn)
                if identity is None:
                    errors.append(
                        f"{label}[{index}] moves missing entry "
                        f"{op.dn.getText()}"
                    )
                    continue
                op.dn = book.current_dn(identity)
                op.newDN = _current_equivalent(book, op.newDN)
                yield op.patch(root)
                book.on_move(op.dn, op.newDN)
                yield _rewrite_existing_members(root, book, member_attributes)
            elif isinstance(op, delta.AddOp):
                op.entry.dn = _current_equivalent(book, op.entry.dn)
                yield op.patch(root)
                identity = _AddressBook.add_identity(op.entry.dn)
                book.on_add(identity, op.entry.dn)
                for value in op.entry.get(identity_attribute, ()):
                    book.on_add(("id", to_unicode(value)), op.entry.dn)
            elif isinstance(op, delta.DeleteOp):
                identity = _authored_identity(base_book, book, op.dn)
                if identity is None:
                    errors.append(
                        f"{label}[{index}] deletes missing entry "
                        f"{op.dn.getText()}"
                    )
                    continue
                book.on_delete(book.current_dn(identity))
                op.dn = book.current_dn(identity)
                yield op.patch(root)
            elif isinstance(op, delta.ModifyOp):
                identity = _authored_identity(base_book, book, op.dn)
                if identity is None:
                    errors.append(
                        f"{label}[{index}] modifies missing entry "
                        f"{op.dn.getText()}"
                    )
                    continue
                op.dn = book.current_dn(identity)
                target_entry = yield root.lookup(op.dn)
                _rewrite_modification(
                    op,
                    base_book,
                    book,
                    member_attributes,
                    target_entry,
                )
                yield op.patch(root)
            else:
                yield op.patch(root)
        except Exception as exc:  # preview must stay total and report both orders
            errors.append(
                f"{label}[{index}] failed against this ordering: {exc}"
            )


@defer.inlineCallbacks
def _simulate(
    base_root,
    base_book,
    change_sets,
    ordering,
    member_attributes,
    identity_attribute,
):
    root = yield _clone_root(base_root)
    book = yield _AddressBook.seed(root, identity_attribute)
    errors = []
    for change_set in ordering:
        yield _apply(
            change_set,
            change_set.operations,
            root,
            base_book,
            book,
            member_attributes,
            identity_attribute,
            errors,
        )
    defer.returnValue((root, book, errors))


def _entry_identity(entry, book):
    ident = book.resolve(entry.dn)
    if ident is None:
        ident = ("dn", dn_key(entry.dn))
    return ident


def _member_targets(values, book):
    targets = []
    for value in sorted(to_unicode(v) for v in values):
        ident = book.resolve(value)
        if ident is None:
            targets.append(
                MemberTarget(
                    identity=("dangling", dn_key(value)),
                    dn=value,
                    dangling=True,
                )
            )
            continue
        current = book.current_dn(ident)
        employee_number = ident[1] if ident[0] == "id" else None
        targets.append(
            MemberTarget(
                identity=ident,
                dn=current.getText(),
                employee_number=employee_number,
            )
        )
    return targets


def _snapshot(root, book, member_attributes):
    d = root.subtree()
    member_keys = _member_key_set(member_attributes)

    def got_entries(entries):
        result = {}
        for entry in entries:
            ident = _entry_identity(entry, book)
            attributes = {}
            members = {}
            for key, values in entry.items():
                normalized = {to_unicode(v) for v in values}
                if _attr_key(key) in member_keys:
                    members[_attr_key(key)] = _member_targets(values, book)
                attributes[_attr_key(key)] = normalized
            result[ident] = {
                "dn": _dn(entry.dn),
                "dn_text": entry.dn.getText(),
                "attributes": attributes,
                "members": members,
            }
        return result

    d.addCallback(got_entries)
    return d


def _competing_changes(change_sets, ident, base_book, attribute):
    found = []
    for change_set in change_sets:
        for index, op in enumerate(change_set.operations):
            if not isinstance(op, delta.ModifyOp):
                continue
            if base_book.resolve(op.dn) != ident:
                continue
            for modification in op.modifications:
                if _attr_key(modification.key) == _attr_key(attribute):
                    found.append(
                        CompetingChange(
                            label=change_set.label,
                            index=index,
                            ldif=op.asLDIF().decode("utf-8"),
                        )
                    )
                    break
    return found


def _all_competing_changes(change_sets, ident, base_book):
    found = []
    for change_set in change_sets:
        for index, op in enumerate(change_set.operations):
            if isinstance(op, delta.AddOp):
                target = op.entry.dn
            else:
                target = getattr(op, "dn", None)
            if target is not None and base_book.resolve(target) == ident:
                found.append(
                    CompetingChange(
                        label=change_set.label,
                        index=index,
                        ldif=op.asLDIF().decode("utf-8"),
                    )
                )
    return found


def _build_conflicts(
    change_sets, base_book, snapshots, ordering_names, member_attributes
):
    first, second = ordering_names
    left = snapshots[first]
    right = snapshots[second]
    conflicts = []
    member_keys = _member_key_set(member_attributes)

    for ident in sorted(set(left) | set(right), key=lambda item: repr(item)):
        if ident not in left or ident not in right:
            present_in = first if ident in left else second
            conflicts.append(
                Conflict(
                    kind="presence",
                    identity=ident,
                    base_dn="",
                    detail=(
                        f"entry exists in ordering {present_in} but not in "
                        f"{second if ident in left else first}"
                    ),
                    competing=_all_competing_changes(
                        change_sets, ident, base_book
                    ),
                )
            )
            continue

        left_state = left[ident]
        right_state = right[ident]
        base_dn = left_state["dn_text"]

        if left_state["dn"] != right_state["dn"]:
            conflicts.append(
                Conflict(
                    kind="location",
                    identity=ident,
                    base_dn=base_dn,
                    detail=(
                        f"ends at {left_state['dn_text']} under {first} but at "
                        f"{right_state['dn_text']} under {second}"
                    ),
                    competing=_all_competing_changes(
                        change_sets, ident, base_book
                    ),
                )
            )

        for attribute in sorted(
            set(left_state["attributes"]) | set(right_state["attributes"])
        ):
            in_left = attribute in left_state["attributes"]
            in_right = attribute in right_state["attributes"]
            if not in_left or not in_right:
                conflicts.append(
                    Conflict(
                        kind="attribute",
                        identity=ident,
                        base_dn=base_dn,
                        attribute=attribute,
                        detail=(
                            f"attribute present only under "
                            f"{first if in_left else second}"
                        ),
                        competing=_competing_changes(
                            change_sets, ident, base_book, attribute
                        ),
                    )
                )
                continue

            if (
                left_state["attributes"][attribute]
                == right_state["attributes"][attribute]
            ):
                continue

            if attribute in member_keys:
                conflicts.append(
                    Conflict(
                        kind="member",
                        identity=ident,
                        base_dn=base_dn,
                        attribute=attribute,
                        detail=(
                            f"effective {attribute} membership depends on the "
                            f"interleaving of the two change sets"
                        ),
                        competing=_competing_changes(
                            change_sets, ident, base_book, attribute
                        ),
                        outcomes={
                            first: left_state["members"][attribute],
                            second: right_state["members"][attribute],
                        },
                    )
                )
            else:
                conflicts.append(
                    Conflict(
                        kind="attribute",
                        identity=ident,
                        base_dn=base_dn,
                        attribute=attribute,
                        detail=(
                            f"effective {attribute} values depend on the "
                            f"interleaving of the two change sets"
                        ),
                        competing=_competing_changes(
                            change_sets, ident, base_book, attribute
                        ),
                    )
                )

    return conflicts


def _contested_identities(conflicts, snapshots, ordering_names):
    contested = set()
    for conflict in conflicts:
        contested.add(conflict.identity)
        for name in ordering_names:
            state = snapshots[name].get(conflict.identity)
            if state is None:
                continue
            prefix_dn = state["dn"]
            for ident, entry_state in snapshots[name].items():
                other_dn = entry_state["dn"]
                if other_dn == prefix_dn or prefix_dn.contains(other_dn):
                    contested.add(ident)
        if conflict.kind == "member":
            for targets in conflict.outcomes.values():
                for target in targets:
                    if not target.dangling:
                        contested.add(target.identity)
    return contested


def _operation_touches(
    op, base_book, live_book, contested, member_attributes
):
    """
    Decide whether an operation belongs to the held subset.

    Structural moves and deletes are only held when the entry they target
    is itself contested (for example a location conflict); moving a subtree
    whose members are contested elsewhere is still safe, because member
    references follow the entries. Modifications are held when they touch
    a contested entry or reference a contested member identity.
    """
    references = []
    if isinstance(op, delta.AddOp):
        target_dn = op.entry.dn
        if _AddressBook.add_identity(target_dn) in contested:
            return True
        for attribute in member_attributes:
            references.extend(op.entry.get(attribute, ()))
    else:
        target_dn = getattr(op, "dn", None)
        if isinstance(op, delta.ModifyOp):
            for modification in op.modifications:
                if _is_member_attribute(modification.key, member_attributes):
                    references.extend(modification)

    if target_dn is not None:
        identity = _authored_identity(base_book, live_book, target_dn)
        if identity is not None and identity in contested:
            return True
    for reference in references:
        identity = _authored_identity(base_book, live_book, reference)
        if identity is not None and identity in contested:
            return True
    return False


@defer.inlineCallbacks
def _build_safe_tree(
    base_root,
    change_sets,
    contested,
    base_book,
    member_attributes,
    identity_attribute,
):
    safe_root = yield _clone_root(base_root)
    book = yield _AddressBook.seed(safe_root, identity_attribute)
    safe_operations = []
    errors = []
    for change_set in change_sets:
        for index, op in enumerate(change_set.operations):
            if _operation_touches(
                op, base_book, book, contested, member_attributes
            ):
                continue
            yield _apply(
                change_set,
                [op],
                safe_root,
                base_book,
                book,
                member_attributes,
                identity_attribute,
                errors,
            )
            safe_operations.append((change_set.label, index, op))
    defer.returnValue((safe_root, safe_operations, errors))


@defer.inlineCallbacks
def preview_merge(
    base_root,
    change_sets,
    member_attributes=DEFAULT_MEMBER_ATTRIBUTES,
    identity_attribute=DEFAULT_IDENTITY_ATTRIBUTE,
):
    """
    Preview the merge of two change sets against a base directory tree.

    @param base_root: root of an in-memory copy of the common LDIF base.
    @param change_sets: exactly two :class:`ChangeSet` instances, each
        holding the operations of one team in their original order.
    @returns: a :class:`MergePreview` via a Deferred.
    """
    if len(change_sets) != 2:
        raise ValueError("preview_merge compares exactly two change sets")

    first, second = change_sets
    ordering_ab = f"{first.label} -> {second.label}"
    ordering_ba = f"{second.label} -> {first.label}"

    base_book = yield _AddressBook.seed(base_root, identity_attribute)

    root_ab, book_ab, errors_ab = yield _simulate(
        base_root,
        base_book,
        change_sets,
        (first, second),
        member_attributes,
        identity_attribute,
    )
    root_ba, book_ba, errors_ba = yield _simulate(
        base_root,
        base_book,
        change_sets,
        (second, first),
        member_attributes,
        identity_attribute,
    )

    snapshot_ab = yield _snapshot(root_ab, book_ab, member_attributes)
    snapshot_ba = yield _snapshot(root_ba, book_ba, member_attributes)
    snapshots = {
        ordering_ab: snapshot_ab,
        ordering_ba: snapshot_ba,
    }

    conflicts = _build_conflicts(
        change_sets,
        base_book,
        snapshots,
        (ordering_ab, ordering_ba),
        member_attributes,
    )

    warning_messages = sorted(set(errors_ab) | set(errors_ba))
    for message in warning_messages:
        conflicts.append(
            Conflict(
                kind="presence",
                identity=("error", message),
                base_dn="",
                detail=message,
            )
        )

    contested = _contested_identities(
        conflicts, snapshots, (ordering_ab, ordering_ba)
    )

    safe_root, safe_operations, safe_errors = yield _build_safe_tree(
        base_root,
        change_sets,
        contested,
        base_book,
        member_attributes,
        identity_attribute,
    )

    held = sorted(contested, key=lambda item: repr(item))
    warnings = sorted(set(warning_messages) | set(safe_errors))

    defer.returnValue(
        MergePreview(
            change_sets=list(change_sets),
            conflicts=conflicts,
            safe_operations=safe_operations,
            held_identities=held,
            warnings=warnings,
            _safe_root=safe_root,
        )
    )


@defer.inlineCallbacks
def preview_merge_from_files(
    base_file,
    change_files,
    labels=("A", "B"),
    member_attributes=DEFAULT_MEMBER_ATTRIBUTES,
    identity_attribute=DEFAULT_IDENTITY_ATTRIBUTE,
):
    """Convenience wrapper loading the base tree and LDIF delta files."""
    from ldaptor.protocols.ldap import ldifdelta

    base_root = yield inmemory.fromLDIFFile(base_file)
    change_sets = []
    for label, change_file in zip(labels, change_files):
        operations = ldifdelta.fromLDIFFile(change_file)
        change_sets.append(ChangeSet(label=label, operations=operations))
    preview = yield preview_merge(
        base_root,
        change_sets,
        member_attributes=member_attributes,
        identity_attribute=identity_attribute,
    )
    defer.returnValue(preview)
