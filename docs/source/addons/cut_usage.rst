.. _cut_usage_addon:

.. module:: ezdxf.addons.cut_usage

Cut Usage
=========

.. versionadded:: 1.4.6

This add-on reports the cut length which a cutter, a laser or a plotter actually
travels. It follows the cutting process of a shop floor:

- every process layer is reported independently
- on one support line the strokes are cut only once, even when several block
  references or source paths produce coincident strokes
- source paths are drawn completely first, with their real linetype, layer
  inheritance and viewport scale, and are clipped afterwards
- the line pattern phase continues across polyline corners and is never reset at
  a clipping boundary
- clipping boundaries are XCLIP paths of block references and VIEWPORT windows in
  paperspace; inverted XCLIP paths are supported

The add-on processes 2D ``LINE``, ``LWPOLYLINE`` and 2D ``POLYLINE`` entities,
directly in a layout or nested in any number of block references. Curves are
flattened before the dash phase is computed.

Two results are available. :meth:`CutUsage.layer_lengths` returns the exact cut
length per layer. :meth:`CutUsage.strokes` returns the exact visible strokes of a
given window together with their original source.

Quick Start
-----------

.. code-block:: Python

    import ezdxf
    from ezdxf.addons import cut_usage

    doc = ezdxf.readfile("workshop.dxf")
    usage = cut_usage.CutUsage(doc)
    for entry in usage.layer_lengths():
        print(f"{entry.layer}: {entry.length_float:.3f}")

The result uses arbitrary precision rational numbers. Round only for the report.

Window Strokes
--------------

A given paper window can be queried independently of the analysed viewport:

.. code-block:: Python

    strokes = usage.strokes_in((120.0, 80.0), (180.0, 140.0))
    for stroke in strokes:
        print(stroke.layer, stroke.start, stroke.end, stroke.origin)

Each stroke knows its process layer, its exact start and end point, its exact
length and the original source. ``stroke.origin`` names the INSERT stack and the
DXF entity, for example ``"INSERT 8F,91 / LINE 8E"``. The ``source_index`` is
unique per original source path, so a repeated block insertion can be audited
separately.

A window edge which falls into a real gap does not create a stroke, therefore an
uncut part of the drawing is visible in the query result.

Viewports and Layer Inheritance
-------------------------------

A paperspace viewport is analysed with the modelspace as input:

.. code-block:: Python

    layout = doc.layouts.get("Layout1")
    viewport = layout.viewports()[0]
    usage = cut_usage.CutUsage(doc, doc.modelspace(), viewport=viewport)

The viewport contributes three things:

- the rectangular or custom clipping window in paperspace coordinates
- the layer linetype overrides of this viewport
- the modelspace to paperspace transform, so the strokes are reported as they
  appear on the printed sheet

The cut length stays in drawing units. A paper stroke may be longer or shorter
than the reported ``stroke.length`` when the viewport scale is not 1:1.

The linetype of an entity is resolved in this order:

1. the explicit entity linetype
2. the inherited linetype of an enclosing block reference for ``BYBLOCK``
3. the linetype of the process layer
4. a VIEWPORT layer override when the entity is rendered through that viewport

The effective pattern scale is the entity ``ltscale`` times the global
``$LTSCALE``.

Exact Totals
------------

The totals are analytic. A wide window with millions of strokes is measured by
whole pattern repeats, not by expanding the strokes. Different line patterns on
the same support line are combined inside one common repeat, which is built once
and then tiled analytically. The common repeat is never laid out across the
complete drawing and single strokes are never enumerated.

The results are exact rational values, including phase, period, stroke count and
the deduplicated length of coincident strokes.

API
---

.. autoclass:: CutUsage
    :members:

.. autoclass:: LayerLength
    :members:

.. autoclass:: Stroke
    :members:

.. autofunction:: layer_lengths

.. autofunction:: window_strokes
