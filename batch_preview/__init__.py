"""Batch preview of release appendices built from shared Markdown manuals.

Given a batch of manuals, a selection table describing which segments each
release picks (and in which order), and an approval table of positions that
may change their reference/definition labels, this package:

* parses every manual with Marko to learn which definitions were actually in
  effect for each reference (links, footnotes, and links inside footnotes),
* chooses one label decision per original position, shared by every release
  that includes the position,
* emits one Markdown document per publishable release -- readable by existing
  footnote extensions, with footnote display numbers rearranged as needed --
  plus a change report keyed by original manual positions,
* reports exactly which unapproved literal texts block a common label when
  releases cannot be published, and says "pending" instead of giving a
  verdict when the batch is too large to decide within the time budget.
"""

from .model import BatchSpec, load_spec, load_spec_file
from .pipeline import BatchResult, run_batch

__all__ = [
    "BatchSpec",
    "BatchResult",
    "load_spec",
    "load_spec_file",
    "run_batch",
]
