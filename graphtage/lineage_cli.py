"""A command line front end for :func:`graphtage.lineage.audit`.

The command takes the three archive versions, one or more section specifications, the reviewed identity table, and
the third-version registration to trace. It prints the audit as JSON so it can be diffed and archived.

Example::

    $ graphtage-lineage v1.json v2.json v3.json \
        --section rows:ordered \
        --identity r2@v1=t1@v3 \
        --identity r1@v1!=t1@v3 \
        --target t1

"""

import argparse
import json
import sys

from . import lineage


def _parse_section(value: str) -> lineage.SectionSpec:
    """Parses ``path:ordered`` or ``path:unordered`` section syntax.

    A path may use dots for nesting, and may be followed by ``=registration_key`` to name the member that holds
    the archive's registration number.
    """
    registration_key = None
    if "=" in value:
        value, registration_key = value.split("=", 1)
    ordered = True
    if ":" in value:
        value, mode = value.rsplit(":", 1)
        if mode not in ("ordered", "unordered"):
            raise argparse.ArgumentTypeError(f"Section mode must be 'ordered' or 'unordered', not {mode!r}")
        ordered = mode == "ordered"
    path = tuple(part for part in value.split(".") if part)
    return lineage.SectionSpec(path, ordered=ordered, registration_key=registration_key,
                               strip_registration_key=registration_key is not None)


def _parse_reference(value: str) -> lineage.OccurrenceRef:
    """Parses ``registration@vN`` into an occurrence reference.

    The registration is parsed as an integer when possible and kept as a string otherwise.
    """
    registration, separator, version = value.rpartition("@v")
    if not separator:
        raise argparse.ArgumentTypeError(f"Expected REGISTRATION@vVERSION, not {value!r}")
    try:
        version_number = int(version) - 1
    except ValueError:
        raise argparse.ArgumentTypeError(f"Expected a version number after @v, not {version!r}")
    if version_number not in (0, 1, 2):
        raise argparse.ArgumentTypeError(f"Version must be 1, 2, or 3, not {version}")
    return lineage.OccurrenceRef(version_number, lineage._parse_registration(registration))


def _parse_identity(value: str) -> lineage.IdentityEvidence:
    """Parses ``LEFT=RIGHT`` or ``LEFT!=RIGHT`` into identity evidence."""
    if "!=" in value:
        left, _, right = value.partition("!=")
        return lineage.different(_parse_reference(left.strip()), _parse_reference(right.strip()))
    if "=" in value:
        left, _, right = value.partition("=")
        return lineage.same(_parse_reference(left.strip()), _parse_reference(right.strip()))
    raise argparse.ArgumentTypeError(f"Expected LEFT=RIGHT or LEFT!=RIGHT, not {value!r}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Trace a third-version registration through two adjacent archive versions, preserving every "
                    "equally cheap correspondence and the reviewed identity table.",
    )
    parser.add_argument("VERSIONS", nargs=3, metavar=("FIRST", "SECOND", "THIRD"),
                        help="the three archive versions, oldest first")
    parser.add_argument("--section", action="append", type=_parse_section, default=[],
                        help="a section as PATH:MODE[=REGISTRATION_KEY]; MODE is ordered or unordered; "
                             "may be repeated")
    parser.add_argument("--identity", action="append", type=_parse_identity, default=[],
                        help="reviewed evidence as LEFT=RIGHT (same entity) or LEFT!=RIGHT (different); "
                             "LEFT and RIGHT are REGISTRATION@vVERSION; may be repeated")
    parser.add_argument("--target", required=True,
                        help="the third-version registration to trace")
    parser.add_argument("--target-section", default=None,
                        help="the section holding the target, when the registration is not unique across sections")
    parser.add_argument("--explanation-limit", type=int, default=lineage._DEFAULT_EXPLANATION_LIMIT,
                        help="the number of concrete explanations to render (default %(default)s)")
    args = parser.parse_args(argv[1:] if argv is not None else None)

    target = lineage._parse_registration(args.target)
    section = None
    if args.target_section:
        section = tuple(part for part in args.target_section.split(".") if part)
    try:
        result = lineage.audit(
            args.VERSIONS,
            sections=args.section or None,
            target=target,
            identities=args.identity,
            section=section,
            explanation_limit=args.explanation_limit,
        )
    except lineage.LineageError as error:
        sys.stderr.write(f"Error: {error!s}\n")
        return 2
    json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
