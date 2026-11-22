# Copyright 2020-2026 Ternaris
# SPDX-License-Identifier: Apache-2.0
"""Extract a time window from rosbag2 recordings."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rosbags.extract import (
    ExtractError,
    MissingDefinitionError,
    extract,
)
from rosbags.typesys import Stores, get_typestore

SEPARATOR = '=' * 80

if TYPE_CHECKING:
    from collections.abc import Sequence


def _type_from_path(path: str) -> str | None:
    """Derive a message type from a package message path."""
    parts = Path(path).parts
    if len(parts) < 2 or not parts[-1].endswith('.msg'):
        return None
    package = parts[-2] if parts[-2] != 'msg' else parts[-3] if len(parts) > 2 else ''
    if not package:
        return None
    return f'{package}/msg/{Path(path).stem}'


def main(argv: Sequence[str] | None = None) -> int:
    """Run the extraction command.

    Args:
        argv: Command line arguments.

    Returns:
        Process exit code.

    """
    parser = argparse.ArgumentParser(
        prog='rosbags-extract',
        description=(
            'Extract a time window from one or more rosbag2 recordings.\n'
            '\n'
            'The result is a regular rosbag2 directory that existing readers '
            'can reopen. Connections keep their topics; types whose '
            'definitions conflict between sources are renamed and recorded in '
            'the result.'
        ),
    )
    _ = parser.add_argument('--src', dest='srcs', type=Path, nargs='+', required=True)
    _ = parser.add_argument('--dst', dest='dst', type=Path, required=True)
    _ = parser.add_argument('--start', dest='start', type=int, default=None)
    _ = parser.add_argument('--stop', dest='stop', type=int, default=None)
    _ = parser.add_argument('--topic', dest='topics', action='append', default=None)
    _ = parser.add_argument(
        '--start-time',
        dest='start_time',
        type=int,
        default=None,
        help='Alias for --start.',
    )
    _ = parser.add_argument(
        '--stop-time',
        dest='stop_time',
        type=int,
        default=None,
        help='Alias for --stop.',
    )
    _ = parser.add_argument(
        '--rename-scope',
        dest='scope_alias',
        default=None,
        help='Alias for --scope.',
    )
    _ = parser.add_argument(
        '--scope',
        dest='scope',
        default=None,
        help='Package prefix used to name conflicting type variants.',
    )
    _ = parser.add_argument(
        '--fallback-typestore',
        dest='fallback_typestore',
        choices=[x.name.lower() for x in Stores],
        default=None,
        help='Typestore consulted for connections with incomplete definitions.',
    )
    _ = parser.add_argument(
        '--fallback-definition',
        dest='fallback_definition',
        action='append',
        default=None,
        metavar='PATH',
        help='File with a definition, e.g. a .msg file. May be repeated.',
    )
    _ = parser.add_argument(
        '--fallback-type',
        dest='fallback_type',
        default=None,
        metavar='TYPE',
        help='Message type the fallback definition files describe.',
    )
    _ = parser.add_argument(
        '--storage',
        dest='storage',
        choices=['sqlite3', 'mcap'],
        default='sqlite3',
        help='Destination storage plugin.',
    )
    _ = parser.add_argument(
        '--strict',
        dest='strict',
        action='store_true',
        help='Fail instead of writing a partial result when definitions are missing.',
    )
    args = parser.parse_args(argv)

    fallback = (
        get_typestore(Stores(args.fallback_typestore.upper())) if args.fallback_typestore else None
    )
    candidates = [fallback] if fallback is not None else []
    for path in args.fallback_definition or []:
        text = Path(path).read_text()
        msgtype = args.fallback_type or _type_from_path(path)
        if msgtype:
            text = f'{SEPARATOR}\nMSG: {msgtype}\n{text}'
        candidates.append(text)
    try:
        result = extract(
            args.srcs,
            args.dst,
            start=args.start if args.start is not None else args.start_time,
            stop=args.stop if args.stop is not None else args.stop_time,
            topics=args.topics,
            fallback=candidates or None,
            include_missing=not args.strict,
            scope=args.scope or args.scope_alias,
            storage=args.storage,
        )
    except MissingDefinitionError as err:
        for connection in err.connections:
            print(  # noqa: T201
                f'Missing definition for {connection.topic} ({connection.msgtype})',
            )
        return 1
    except ExtractError as err:
        print(f'ERROR: {err}', file=sys.stderr)  # noqa: T201
        return 1

    print(  # noqa: T201
        f'Wrote {result.message_count} messages on {result.written} connections.',
    )
    for old, new in sorted(result.renamed.items()):
        print(f'Renamed {old} variants: {", ".join(new)}')  # noqa: T201
    for connection in result.missing:
        print(  # noqa: T201
            f'Missing definition for {connection.topic} ({connection.msgtype})',
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
