"""Parse and rewrite CLDF citation strings.

Citation cells follow the CLDF convention ``Key[description]`` with several
references separated by ``;`` (e.g. ``AB[3-5];AB[12];AB[18]``).  Only the
citation *key* is ever rewritten; the bracket descriptions (page ranges,
appendix sections, ...) and the ordering of references are preserved exactly.
"""

from typing import Callable, Optional

from pycldf.sources import Sources

REF_SEPARATOR = ';'


def split_refs(cell: str | list[str] | None) -> list[str]:
    """Split a raw citation cell into individual ``Key[desc]`` strings."""
    if cell is None:
        return []
    if isinstance(cell, list):
        pieces = cell
    else:
        pieces = cell.split(REF_SEPARATOR)
    return [piece.strip() for piece in pieces if piece and piece.strip()]


def parse_ref(ref: str) -> tuple[str, Optional[str]]:
    """Return ``(citation key, bracket description or None)`` for one reference."""
    return Sources.parse(ref)


def render_ref(key: str, description: Optional[str]) -> str:
    return f'{key}[{description}]' if description else key


def rewrite_refs(
    cell: str | list[str] | None,
    rewrite_key: Callable[[str], str],
) -> list[str]:
    """Rewrite every citation key in a cell, keeping descriptions and order."""
    rewritten = []
    for ref in split_refs(cell):
        key, description = parse_ref(ref)
        rewritten.append(render_ref(rewrite_key(key), description))
    return rewritten


def join_refs(refs: list[str]) -> str:
    return REF_SEPARATOR.join(refs)
