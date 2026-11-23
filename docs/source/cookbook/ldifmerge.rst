=============================
LDIF Change Set Merge Preview
=============================

Two teams sometimes edit the same LDIF base draft in parallel:

* one team moves whole subtrees (``changetype: modrdn`` / ``moddn``);
* the other team maintains group memberships.

``ldaptor-ldifmerge`` previews whether their two change sets can be merged
before anything is imported. It never silently picks a winner.

The preview replays both change sets twice, preserving the operation order
inside each change set:

1. change set A followed by change set B;
2. change set B followed by change set A.

If the two orderings produce a different effective directory, the entry is
reported as a disagreement together with:

* the concrete competing change records (with their change set label and
  position);
* the person each outcome points at, shown at that person's actual address.

Identity and references
-----------------------

Person entries are tracked by a stable identity attribute, by default
``employeeNumber``. Change sets are authored against the base draft, so a
reference written using an old address keeps pointing at the base identity
even after a different entry has since been registered at that address.

Only the configured member attributes (by default ``member`` and
``uniqueMember``) treat their values as distinguished names that refer to
real entries. When a referenced entry moves, these values follow it. Any
other attribute that merely looks like a DN, for example ``seeAlso``, is
kept as an ordinary plain value.

Two DN spellings that this library parses into the same distinguished name
(for example different letter case) are tracked as one and the same
address.

Safe subset
-----------

Entries and operations that are not part of any disagreement form a safe
subset. They can be handed to the directory first; the contested entries
stay held back until a confirmation arrives. The safe subset is a complete,
re-readable directory tree: it preserves the real hierarchy after moves,
the rewritten member relationships and the original case identities.

Usage
-----

::

    ldaptor-ldifmerge [options] BASE.ldif CHANGE_A.ldif CHANGE_B.ldif

Options:

* ``--label A,B`` -- names used for the two change sets in the report
  (default ``A,B``).
* ``--member ATTR`` -- member attribute whose values are DN references;
  repeatable (default ``member`` and ``uniqueMember``).
* ``--identity ATTR`` -- stable identity attribute (default
  ``employeeNumber``).
* ``--safe-ldif PATH`` -- write the uncontested subset as an LDIF tree.
* ``--json`` -- emit a machine-readable report instead of text.

Exit status is ``0`` when the change sets merge cleanly in either order and
``3`` when at least one disagreement is reported. Parse and load errors exit
with status ``2``; bad command line usage exits with ``1``.

Example
-------

Given a base where ``cn=alice,ou=dept,dc=example,dc=com`` belongs to a
group, one change set moves ``ou=dept`` under ``ou=archive`` while the
other change set edits the same group membership::

    $ ldaptor-ldifmerge --label tree,groups --safe-ldif safe.ldif \
        base.ldif tree.ldif groups.ldif
    Cannot merge automatically: 1 disagreement(s) between the two interleavings.

    1. [member] cn=staff,ou=groups,dc=example,dc=com
       attribute: member
       effective member membership depends on the interleaving of the two change sets
       competing changes:
         tree[3]
         dn: cn=staff,ou=groups,dc=example,dc=com
         ...
         groups[1]
         dn: cn=staff,ou=groups,dc=example,dc=com
         ...
       effective members under tree -> groups:
         - E-1: cn=alice,ou=dept,ou=archive,dc=example,dc=com
       effective members under groups -> tree:
         - E-1: cn=alice,ou=dept,ou=archive,dc=example,dc=com
         - E-3: cn=bob,ou=dept,ou=archive,dc=example,dc=com

The same data is available programmatically through
:class:`ldaptor.changeset.ChangeSet` and
:func:`ldaptor.changeset.preview_merge`.
