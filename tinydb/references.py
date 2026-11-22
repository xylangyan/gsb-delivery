"""
Reference types used to locate documents across table renames/merges.

A work order only stores the table name (and usually a document ID) that was
current when the table was opened. Once tables have been merged or a table
name has been reused for a new, empty generation, a bare table name is no
longer enough to tell generations apart. These types carry the additional
generation information and describe the outcome of resolving a reference.
"""

from dataclasses import dataclass
from typing import Optional

__all__ = (
    'DEFAULT_TABLE_VERSION',
    'TableRef',
    'ResolvedLocation',
    'AmbiguousReference',
    'UnresolvableReference',
    'TableRetiredError',
)

#: Tables created without an explicit version belong to the first generation.
#: Existing databases that predate the generation feature are interpreted as
#: containing generation-1 tables.
DEFAULT_TABLE_VERSION: int = 1


@dataclass(frozen=True)
class TableRef:
    """
    A pointer to a document (or a whole table) captured at open time.

    :param name: The table name as it was when the work order was created.
    :param version: The table generation the handle was opened with.
                    When ``None``, only the bare table name is known and
                    resolution stays conservative (see
                    :meth:`tinydb.database.TinyDB.resolve_ref`).
    :param doc_id: The document ID stored in the work order, if any.
    """

    name: str
    version: Optional[int] = None
    doc_id: Optional[int] = None


@dataclass(frozen=True)
class ResolvedLocation:
    """
    Where a :class:`TableRef` points according to the current database state.

    :param table: The table that currently holds the referenced data.
    :param doc_id: The document ID inside that table; ``None`` when the
                   reference did not carry a document ID.
    """

    table: str
    doc_id: Optional[int] = None


class AmbiguousReference(Exception):
    """
    Raised when a reference without generation information matches more than
    one table generation.

    The candidate locations are attached so callers can present them instead
    of guessing. Callers that prefer data over an exception can use
    :meth:`tinydb.database.TinyDB.resolve_candidates`.
    """

    def __init__(self, ref: TableRef, candidates: list[ResolvedLocation]):
        self.ref = ref
        self.candidates = candidates
        super().__init__(
            'Reference {!r} matches multiple table generations: {}'.format(
                ref, candidates
            )
        )


class UnresolvableReference(Exception):
    """
    Raised when a reference matches no known table generation.

    This means the stored table name/ID alone is insufficient evidence: the
    referenced table may have been dropped without a merge alias, or the
    reference may predate any recorded generation. The database deliberately
    does not fall back to a same-named newer table in this situation.
    """

    def __init__(self, ref: TableRef):
        self.ref = ref
        super().__init__(
            f'Reference {ref!r} cannot be resolved to any known table '
            f'generation'
        )


class TableRetiredError(RuntimeError):
    """
    Raised when a write is attempted through a table handle whose name has
    been retired by a merge. The handle remains usable for reads.
    """
