"""Command line entry point for the two-stage archive."""

import argparse
import pathlib
import sys

from curation.holdingcldf.errors import ArchiveError
from curation.holdingcldf.pipeline import build_edition_one, rearchive_edition_two


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    one = sub.add_parser('edition1', help='merge supplier batches into edition-1 CLDF')
    one.add_argument('--output', required=True, type=pathlib.Path)
    one.add_argument('--batch', required=True, action='append', type=pathlib.Path,
                     dest='batches', help='supplier batch directory (repeatable)')
    one.add_argument('--catalog', required=True, type=pathlib.Path)
    one.add_argument('--library', required=True, type=pathlib.Path,
                     help='TSV of bibliographic records, one row per holding')

    two = sub.add_parser('edition2', help='re-archive edition-1 artifact plus a supplement')
    two.add_argument('--output', required=True, type=pathlib.Path)
    two.add_argument('--edition-one', required=True, type=pathlib.Path)
    two.add_argument('--supplement', required=True, type=pathlib.Path)
    two.add_argument('--catalog', required=True, type=pathlib.Path)
    two.add_argument('--library', required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == 'edition1':
            result = build_edition_one(
                batch_dirs=args.batches, catalog_dir=args.catalog,
                source_library=args.library, output_dir=args.output)
        else:
            result = rearchive_edition_two(
                edition_one_dir=args.edition_one, supplement_dir=args.supplement,
                catalog_dir=args.catalog, source_library=args.library,
                output_dir=args.output)
    except (ArchiveError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f'wrote {result}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
