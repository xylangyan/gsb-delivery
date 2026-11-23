"""
Preview whether two LDIF change sets built on the same LDIF base merge.

Usage:
    ldaptor-ldifmerge [options] BASE.ldif CHANGE_A.ldif CHANGE_B.ldif

The preview replays both change sets in both order-preserving
interleavings and reports every entry whose effective content depends on
which change set is applied first. Member attributes (by default
``member`` and ``uniqueMember``) are treated as DN references that follow
entries when they move; other attributes stay plain values.
"""

import json
import sys

from twisted.python import failure

from ldaptor import changeset, usage


def _sync(deferred):
    """
    Unwrap a Deferred backed by the synchronous in-memory directory.

    Every operation used by the preview fires immediately, so the Deferred
    already carries its result (or a Failure) without needing a reactor.
    """
    result = deferred.result
    if isinstance(result, failure.Failure):
        result.raiseException()
    return result


def _identity_to_text(identity):
    kind, *parts = identity
    if kind == "id":
        return f"identity {parts[0]}"
    if kind == "add":
        return f"added entry {parts[0]}"
    if kind == "dn":
        return f"entry {parts[0]}"
    if kind == "dangling":
        return f"dangling reference {parts[0]}"
    return " ".join(str(part) for part in identity)


def _render_text(preview):
    lines = []
    if preview.mergeable:
        lines.append("The two change sets merge cleanly in either order.")
    else:
        lines.append(
            f"Cannot merge automatically: {len(preview.conflicts)} "
            f"disagreement(s) between the two interleavings."
        )
    lines.append("")

    for number, conflict in enumerate(preview.conflicts, start=1):
        subject = conflict.base_dn or _identity_to_text(conflict.identity)
        lines.append(f"{number}. [{conflict.kind}] {subject}")
        if conflict.attribute:
            lines.append(f"   attribute: {conflict.attribute}")
        lines.append(f"   {conflict.detail}")
        if conflict.competing:
            lines.append("   competing changes:")
            for change in conflict.competing:
                for change_line in change.describe().splitlines():
                    lines.append(f"     {change_line}")
        for ordering, targets in conflict.outcomes.items():
            lines.append(f"   effective members under {ordering}:")
            for target in targets:
                if target.dangling:
                    lines.append(f"     - dangling: {target.dn}")
                elif target.employee_number:
                    lines.append(
                        f"     - {target.employee_number}: {target.dn}"
                    )
                else:
                    lines.append(f"     - {target.dn}")
        lines.append("")

    if preview.held_identities:
        lines.append("Held back pending confirmation:")
        for identity in preview.held_identities:
            lines.append(f"  - {_identity_to_text(identity)}")
        lines.append("")

    if preview.safe_operations:
        lines.append("Uncontested changes safe to load now:")
        for label, index, _ in preview.safe_operations:
            lines.append(f"  - {label}[{index}]")
        lines.append("")

    for warning in preview.warnings:
        lines.append(f"warning: {warning}")

    return "\n".join(lines).rstrip() + "\n"


def _render_json(preview):
    payload = {
        "mergeable": preview.mergeable,
        "conflicts": [
            {
                "kind": conflict.kind,
                "identity": list(conflict.identity),
                "base_dn": conflict.base_dn,
                "attribute": conflict.attribute,
                "detail": conflict.detail,
                "competing": [
                    {
                        "label": change.label,
                        "index": change.index,
                        "ldif": change.ldif,
                    }
                    for change in conflict.competing
                ],
                "outcomes": {
                    ordering: [
                        {
                            "identity": list(target.identity),
                            "dn": target.dn,
                            "employee_number": target.employee_number,
                            "dangling": target.dangling,
                        }
                        for target in targets
                    ]
                    for ordering, targets in conflict.outcomes.items()
                },
            }
            for conflict in preview.conflicts
        ],
        "held_identities": [list(identity) for identity in preview.held_identities],
        "safe_operations": [
            {"label": label, "index": index}
            for label, index, _ in preview.safe_operations
        ],
        "warnings": preview.warnings,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


class MyOptions(usage.Options):
    """Preview the merge of two LDIF change sets against a common base.

    Usage: ldaptor-ldifmerge [options] BASE.ldif CHANGE_A.ldif CHANGE_B.ldif

    Replays both change sets in both order-preserving interleavings and
    reports entries whose effective content depends on which change set is
    applied first. Member attributes (by default member and uniqueMember)
    are treated as DN references that follow entries when they move; other
    attributes stay plain values.
    """

    optFlags = [["json", "j", "emit the preview as JSON instead of text"]]
    optParameters = [
        [
            "identity",
            "i",
            changeset.DEFAULT_IDENTITY_ATTRIBUTE,
            "stable identity attribute on person entries",
        ],
        [
            "safe-ldif",
            "s",
            None,
            "write the uncontested subset as an LDIF tree to this path",
        ],
        [
            "label",
            "l",
            None,
            "comma separated labels for the two change sets (default: A,B)",
        ],
    ]

    def __init__(self):
        super().__init__()
        self["member"] = []

    def opt_member(self, value):
        """Member attribute whose values are DN references. Repeatable."""
        self["member"].append(value)

    def parseArgs(self, base, change_a, change_b):
        self.opts["base"] = base
        self.opts["change_a"] = change_a
        self.opts["change_b"] = change_b


def console_script():
    try:
        options = MyOptions()
        options.parseOptions()
    except usage.UsageError as error:
        sys.stderr.write(f"{sys.argv[0]}: {error}\n")
        sys.exit(1)

    if options["label"]:
        labels = tuple(label.strip() for label in options["label"].split(","))
        if len(labels) != 2:
            sys.stderr.write(
                f"{sys.argv[0]}: --label needs exactly two comma separated names\n"
            )
            sys.exit(1)
    else:
        labels = ("A", "B")

    member_attributes = options["member"]
    if not member_attributes:
        member_attributes = list(changeset.DEFAULT_MEMBER_ATTRIBUTES)

    with open(options["base"], "rb") as base_file, open(
        options["change_a"], "rb"
    ) as change_a_file, open(options["change_b"], "rb") as change_b_file:
        try:
            preview = _sync(
                changeset.preview_merge_from_files(
                    base_file,
                    [change_a_file, change_b_file],
                    labels=labels,
                    member_attributes=tuple(member_attributes),
                    identity_attribute=options["identity"],
                )
            )
        except Exception as exc:  # surface parse/load errors cleanly
            sys.stderr.write(f"{sys.argv[0]}: {exc}\n")
            sys.exit(2)

    if options["safe-ldif"] is not None:
        safe_ldif = _sync(preview.safe_tree_as_ldif())
        with open(options["safe-ldif"], "wb") as safe_file:
            safe_file.write(safe_ldif)

    if options["json"]:
        sys.stdout.write(_render_json(preview))
    else:
        sys.stdout.write(_render_text(preview))
        if options["safe-ldif"] is not None:
            sys.stdout.write(
                f"\nUncontested subset written to {options['safe-ldif']}.\n"
            )

    sys.exit(0 if preview.mergeable else 3)


if __name__ == "__main__":
    console_script()
