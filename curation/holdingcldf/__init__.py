"""Two-edition archival pipeline for holding-library wordlists (built on pycldf).

Numbering model (three layers, never mixed by string shape):

1. ``holding_id``  - stable library holding, e.g. ``R37`` / ``R88``.
2. local code      - supplier batch code, meaningful *only* inside that batch's
   own mapping table (alpha ``X1`` and beta ``AB`` may both mean ``R37``, while
   beta's raw ``X1`` means ``R88``).
3. official code   - catalogue code of a holding under a catalogue edition
   (edition 1: ``R37 -> X1``; edition 2: ``R37 -> CAT37``).

Resolution is always driven by the context declared in manifests/mappings,
never by the shape of a code string.
"""

from curation.holdingcldf.catalog import BatchMapping, HoldingCatalog
from curation.holdingcldf.errors import ArchiveError, RefProblem
from curation.holdingcldf.pipeline import build_edition_one, rearchive_edition_two

__all__ = [
    'BatchMapping',
    'HoldingCatalog',
    'ArchiveError',
    'RefProblem',
    'build_edition_one',
    'rearchive_edition_two',
]
