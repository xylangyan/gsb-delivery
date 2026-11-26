"""
:mod:`pyformlang.fst`
=====================

This module deals with finite state transducers.

Available Classes
-----------------

FST
    A Finite State Transducer
TranslationReplay
    A lazy, ordinal-addressable replay of ``FST.translate``
VersionedReplay
    Two replays of the same events under different length limits

Available Functions
-------------------

replay
    Build a :class:`TranslationReplay`
compare_replays
    Build a :class:`VersionedReplay`

"""

from .fst import FST
from .replay import (
    Receipt, RangeStats, TranslationReplay, TransitionSource,
    VersionedReplay, assert_matches_translate, compare_replays, replay)

__all__ = [
    "FST",
    "Receipt",
    "RangeStats",
    "TranslationReplay",
    "TransitionSource",
    "VersionedReplay",
    "assert_matches_translate",
    "compare_replays",
    "replay",
]
