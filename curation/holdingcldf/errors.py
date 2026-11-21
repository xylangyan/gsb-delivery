"""Structured failures for the archival pipeline.

Every failure pinpoints:
- the supplier *batch* (or catalogue) being processed,
- the original *field* (local citation column) that held the bad reference,
- the 1-based source *row* (with the row's ID when known),
- and either the missing target or the concrete candidate list.

Nothing is written half-updated: validation collects all problems first, and
output is only written when zero problems were found.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RefProblem:
    """One unresolvable citation, with everything needed to locate it."""

    kind: str  # 'missing_target' | 'ambiguous_target'
    batch: str
    table: str
    column: str
    row_number: int
    row_id: Optional[str]
    raw_ref: str
    detail: str
    candidates: list[str] = field(default_factory=list)

    def render(self) -> str:
        where = (
            f"batch={self.batch!r} table={self.table} field={self.column!r} "
            f"row={self.row_number}"
        )
        if self.row_id is not None:
            where += f" id={self.row_id!r}"
        if self.kind == 'missing_target':
            return (
                f"missing target [{where}] raw_ref={self.raw_ref!r}: {self.detail}"
            )
        candidates = ', '.join(self.candidates)
        return (
            f"ambiguous target [{where}] raw_ref={self.raw_ref!r}: "
            f"{self.detail}; candidates=[{candidates}]"
        )


class ArchiveError(Exception):
    """Aggregate error raised when one or more references cannot be resolved."""

    def __init__(self, problems: list[RefProblem], stage: str):
        self.problems = problems
        self.stage = stage
        header = f"{stage} aborted with {len(problems)} unresolved reference(s); no output written"
        super().__init__(header + ':\n' + '\n'.join(p.render() for p in problems))
