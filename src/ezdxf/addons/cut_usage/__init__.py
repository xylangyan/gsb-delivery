# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""Cut usage accounting for DXF drawings.

The add-on reports the cutting length which a cutter or laser actually travels.
It follows these rules:

- Every process layer is reported independently.
- On one support line the strokes are cut only once, even when several source
  paths or block references produce coincident strokes.
- Source paths are drawn completely first, with their real linetype, layer
  inheritance and viewport scale, and are clipped afterwards. The phase of a
  linetype continues across polyline corners and is never reset at a clipping
  boundary.
- Clipping boundaries are XCLIP paths of block references and VIEWPORT windows in
  paperspace. Inverted XCLIP paths are supported.
- Totals are computed analytically. A wide window with millions of strokes is
  measured without expanding the strokes and without laying out a common repeat
  of several periods.

The main entry points are :func:`layer_lengths`, :func:`window_strokes` and the
:class:`CutUsage` class.
"""
from __future__ import annotations

from .geometry import CutUsageError
from .engine import CutUsage, LayerLength, Stroke, layer_lengths, window_strokes

__all__ = [
    "CutUsage",
    "CutUsageError",
    "LayerLength",
    "Stroke",
    "layer_lengths",
    "window_strokes",
]
