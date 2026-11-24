"""Enhanced-dependency (DEPS) redaction solver.

Only the dependencies column (DEPS) is negotiated here.  The ordinary word
order, new ids and basic HEAD/DEPREL are confirmed elsewhere and passed in as
an id mapping.  The solver decides which original enhanced relations survive
and which permitted empty-node placeholder each removed intermediate is
represented by.
"""

from .model import Edge, License, Query, SentenceInput
from .solver import SentenceResult, solve_sentence
from .graph import EnhancedGraph, label_sequence_matches

__all__ = [
    "Edge",
    "EnhancedGraph",
    "License",
    "Query",
    "SentenceInput",
    "SentenceResult",
    "label_sequence_matches",
    "solve_sentence",
]
