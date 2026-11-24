#!/usr/bin/env python3
# apngwin.py
"""Windowed frame-value queries over an APNG file, built on PyPNG.

A quality-control pass typically inspects only a few frames of one
APNG file, and only a small rectangle of each frame.  This module
supports exactly that access pattern:

* :func:`register` scans the file once and records the file signature
  (the file's "edition") together with the byte range of every chunk,
  in particular every animation frame control chunk (``fcTL``) and
  every compressed data chunk (``IDAT``/``fdAT``).
* :class:`ApngWindows` answers formal queries using only range reads
  against the registered ranges.  Each query reports the raw ranges
  it actually fetched.
* A source frame whose compressed body cannot influence any requested
  window -- proved from control information alone, or from already
  decoded pixels -- is never read: its compressed body stays in cold
  storage.
* A compressed stream that is needed is decompressed exactly once and
  shared by every window; no canvas larger than a requested window is
  ever materialized.

The static canvas (the default image) and the animation frames are
both interpreted according to the file's own convention: when the
first ``fcTL`` chunk follows the ``IDAT`` chunks the default image is
merely a cover and is not the first animation frame.

Values are reported as RGBA8 at the moment the requested frame has
been completely displayed, before its disposal operation is applied.
:func:`playback` is the independent reference: one full playback on a
full-size canvas, cropped to the window afterwards.
"""

import io
import struct

import png

#: PNG file signature; registered as the file's edition mark.
SIGNATURE = b'\x89PNG\r\n\x1a\n'

# Disposal operations (fcTL dispose_op).
DISPOSE_NONE = 0
DISPOSE_BACKGROUND = 1
DISPOSE_PREVIOUS = 2

# Blend operations (fcTL blend_op).
BLEND_SOURCE = 0
BLEND_OVER = 1

#: Frame selector for the static canvas (the default image).
DEFAULT = 'default'


class ChunkSpan:
    """Byte range of one chunk's data in the original file."""

    def __init__(self, type, offset, length):
        self.type = type
        self.offset = offset
        self.length = length

    def __repr__(self):
        return 'ChunkSpan(%r, %d, %d)' % (self.type, self.offset, self.length)


class FrameControl:
    """Parsed content of one ``fcTL`` chunk."""

    def __init__(self, data):
        (self.sequence_number,
         self.width, self.height,
         self.x_offset, self.y_offset,
         self.delay_num, self.delay_den,
         self.dispose_op, self.blend_op) = struct.unpack('!5I2H2B', data)

    @property
    def rect(self):
        """Frame rectangle as (x, y, width, height) in canvas coordinates."""
        return (self.x_offset, self.y_offset, self.width, self.height)


class ApngIndex:
    """Registered layout of one (A)PNG file.

    Records the file signature and the byte ranges of the animation
    frame control chunks and the compressed data chunks, without
    keeping any of the compressed bodies.
    """

    def __init__(self, signature, ihdr, actl, spans):
        self.signature = signature
        self.ihdr = ihdr
        self.actl = actl
        self.spans = spans
        # fcTL spans, in sequence order.
        self.frames = []
        # Per animation frame: the IDAT/fdAT spans of its compressed body.
        self.frame_data = []
        # IDAT spans of a default image that is not part of the animation.
        self.default_data = []
        self.plte = None
        self.trns = None
        idat_after_fctl = []
        for span in spans:
            if span.type == b'fcTL':
                self.frames.append(span)
                self.frame_data.append([])
            elif span.type == b'fdAT':
                if not self.frames:
                    raise png.FormatError('fdAT chunk before any fcTL chunk')
                self.frame_data[-1].append(span)
            elif span.type == b'IDAT':
                if self.frames:
                    idat_after_fctl.append(span)
                else:
                    self.default_data.append(span)
            elif span.type == b'PLTE':
                self.plte = span
            elif span.type == b'tRNS':
                self.trns = span
        if idat_after_fctl:
            # The first fcTL precedes the IDAT chunks, so the default
            # image is the first animation frame and its IDAT chunks
            # carry frame 0's compressed body.
            self.frame_data[0] = idat_after_fctl + self.frame_data[0]
            self.default_data = []

    @property
    def is_animated(self):
        return bool(self.frames)

    @property
    def default_is_cover(self):
        """True when the default image is only a cover, not a frame."""
        return bool(self.default_data)


def register(source):
    """Scan *source* once and register its layout as an :class:`ApngIndex`.

    *source* is a filename or a readable, seekable file object.
    Only the signature, the ``IHDR`` and ``acTL`` contents, and the
    byte ranges of the chunks are recorded; compressed bodies are not
    read.
    """

    close = False
    if isinstance(source, str):
        f = open(source, 'rb')
        close = True
    else:
        f = source
    try:
        signature = f.read(8)
        if signature != SIGNATURE:
            raise png.FormatError('PNG file has invalid signature.')
        ihdr = None
        actl = None
        spans = []
        while True:
            header = f.read(8)
            if not header:
                break
            if len(header) != 8:
                raise png.FormatError(
                    'End of file whilst reading chunk length and type.')
            length, tag = struct.unpack('!I4s', header)
            offset = f.tell()
            if tag == b'IHDR':
                data = f.read(length)
                (w, h, bitdepth, color_type, compression,
                 filt, interlace) = struct.unpack('!2I5B', data)
                ihdr = dict(width=w, height=h, bitdepth=bitdepth,
                            color_type=color_type, compression=compression,
                            filter=filt, interlace=interlace)
                f.seek(4, 1)
            elif tag == b'acTL':
                data = f.read(length)
                actl = struct.unpack('!2I', data)
                f.seek(4, 1)
            else:
                f.seek(length + 4, 1)
            spans.append(ChunkSpan(tag, offset, length))
    finally:
        if close:
            f.close()
    if ihdr is None:
        raise png.FormatError('This PNG file has no IHDR chunk.')
    index = ApngIndex(signature, ihdr, actl, spans)
    if actl is not None and actl[0] != len(index.frames):
        raise png.FormatError('acTL num_frames does not match fcTL count.')
    return index


# Rectangles are (x, y, width, height) tuples; a region is a list of
# disjoint rectangles.

def _rect_intersect(a, b):
    x = max(a[0], b[0])
    y = max(a[1], b[1])
    right = min(a[0] + a[2], b[0] + b[2])
    bottom = min(a[1] + a[3], b[1] + b[3])
    if right <= x or bottom <= y:
        return None
    return (x, y, right - x, bottom - y)


def _rect_subtract(a, b):
    """List of rectangles covering a \\ b."""
    i = _rect_intersect(a, b)
    if i is None:
        return [a]
    ax, ay, aw, ah = a
    ix, iy, iw, ih = i
    out = []
    if iy > ay:
        out.append((ax, ay, aw, iy - ay))
    if iy + ih < ay + ah:
        out.append((ax, iy + ih, aw, ay + ah - (iy + ih)))
    if ix > ax:
        out.append((ax, iy, ix - ax, ih))
    if ix + iw < ax + aw:
        out.append((ix + iw, iy, ax + aw - (ix + iw), ih))
    return out


def _intersect_all(region, rect):
    out = []
    for r in region:
        i = _rect_intersect(r, rect)
        if i is not None:
            out.append(i)
    return out


def _subtract_all(region, rect):
    out = []
    for r in region:
        out.extend(_rect_subtract(r, rect))
    return out


def _over(fore, back):
    """Source-over blend of one RGBA8 pixel onto another."""
    fr, fg, fb, fa = fore
    br, bg, bb, ba = back
    if fa == 0:
        return back
    if fa == 255:
        return fore
    af = fa / 255.0
    ab = ba / 255.0
    ao = af + ab * (1.0 - af)
    if ao == 0:
        return (0, 0, 0, 0)
    r = round((fr * af + br * ab * (1.0 - af)) / ao)
    g = round((fg * af + bg * ab * (1.0 - af)) / ao)
    b = round((fb * af + bb * ab * (1.0 - af)) / ao)
    return (r, g, b, round(ao * 255))


def _row_runs(row, base, width, pred):
    """Runs of columns where pred(alpha) holds in one decoded row."""
    runs = []
    dx = 0
    while dx < width:
        if pred(row[4 * (base + dx) + 3]):
            start = dx
            dx += 1
            while dx < width and pred(row[4 * (base + dx) + 3]):
                dx += 1
            runs.append((start, dx - start))
        else:
            dx += 1
    return runs


def _merge_runs(rows_of_runs, x, y):
    """Merge per-row runs into maximal vertical rectangles."""
    rects = []
    open = {}
    for dy in range(len(rows_of_runs) + 1):
        current = set(rows_of_runs[dy]) if dy < len(rows_of_runs) else set()
        for key in list(open):
            if key not in current:
                start, length = key
                top = open.pop(key)
                rects.append((x + start, y + top, length, dy - top))
        for key in current:
            if key not in open:
                open[key] = dy
    return rects


def _alpha_split(rows, frame_rect, rects):
    """Split *rects* of a decoded frame by alpha.

    Returns (keep, semi): *keep* covers pixels with alpha > 0 (the
    frame visibly contributes there); *semi* covers pixels with
    alpha < 255 (the prior canvas still shows through there).
    *rects* are canvas rectangles contained in *frame_rect*; *rows*
    are the frame's decoded RGBA8 rows.
    """

    fx, fy = frame_rect[0], frame_rect[1]
    keep = []
    semi = []
    for (x, y, w, h) in rects:
        base = x - fx
        window_rows = rows[y - fy:y - fy + h]
        keep += _merge_runs(
            [_row_runs(r, base, w, lambda a: a > 0) for r in window_rows],
            x, y)
        semi += _merge_runs(
            [_row_runs(r, base, w, lambda a: a < 255) for r in window_rows],
            x, y)
    return keep, semi


def _decode_image(index, width, height, body, aux=()):
    """Decode one compressed image stream via PyPNG's pixel reader.

    *body* is the concatenated IDAT/fdAT content (fdAT sequence
    numbers already stripped).  A synthetic single-image PNG stream is
    assembled from the registered header information and fed to
    :class:`png.Reader`, so all filtering, interlacing and colour
    handling is PyPNG's own.  Returns RGBA8 rows.
    """

    ihdr = index.ihdr
    out = io.BytesIO()
    out.write(SIGNATURE)
    png.write_chunk(out, b'IHDR', struct.pack(
        '!2I5B', width, height,
        ihdr['bitdepth'], ihdr['color_type'],
        ihdr['compression'], ihdr['filter'], ihdr['interlace']))
    for tag, data in aux:
        png.write_chunk(out, tag, data)
    png.write_chunk(out, b'IDAT', body)
    png.write_chunk(out, b'IEND', b'')
    w, h, rows, info = png.Reader(bytes=out.getvalue()).asRGBA8()
    if (w, h) != (width, height):
        raise png.FormatError('decoded image has unexpected size')
    return [list(row) for row in rows]


class WindowResult:
    """Answer to one windowed query.

    `rows` are the window's RGBA8 pixels, `rows[y][x]` an
    (r, g, b, a) tuple, sampled when the frame has been completely
    displayed, before disposal.  `reads` lists the raw (offset,
    length) ranges of the original file that this query actually
    fetched; `decoded` lists the animation frames whose compressed
    body was decompressed for this query.
    """

    def __init__(self, frame, window, rows, reads, decoded):
        self.frame = frame
        self.window = window
        self.rows = rows
        self.reads = reads
        self.decoded = decoded


class ApngWindows:
    """Windowed frame-value queries over one APNG file.

    All access to the original file is by range reads against the
    ranges recorded in the :class:`ApngIndex`; every range actually
    fetched is appended to :attr:`reads`.
    """

    def __init__(self, source, index=None):
        if index is None:
            index = register(source)
        self.index = index
        self._close = False
        if isinstance(source, str):
            self._file = open(source, 'rb')
            self._close = True
        else:
            self._file = source
        # Every (offset, length) range fetched after registration.
        self.reads = []
        # Animation frames whose compressed body was decompressed.
        self.decoded = []
        self._controls = {}
        self._pixels = {}
        self._default = None
        self._aux = None

    def close(self):
        if self._close:
            self._file.close()
            self._close = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _read_span(self, span):
        self._file.seek(span.offset)
        data = self._file.read(span.length)
        if len(data) != span.length:
            raise png.FormatError('Chunk %s too short for required octets.'
                                  % (span.type,))
        self.reads.append((span.offset, span.length))
        return data

    def _control(self, i):
        """Frame control of animation frame *i*, read by range."""
        if i not in self._controls:
            self._controls[i] = FrameControl(
                self._read_span(self.index.frames[i]))
        return self._controls[i]

    def _aux_chunks(self):
        if self._aux is None:
            aux = []
            for span in (self.index.plte, self.index.trns):
                if span is not None:
                    aux.append((span.type, self._read_span(span)))
            self._aux = aux
        return self._aux

    def _frame_rows(self, i):
        """Decoded RGBA8 rows of animation frame *i*.

        The frame's compressed stream is decompressed exactly once and
        the decoded rows are shared by every window that needs them.
        """
        if i not in self._pixels:
            fc = self._control(i)
            parts = []
            for span in self.index.frame_data[i]:
                data = self._read_span(span)
                if span.type == b'fdAT':
                    data = data[4:]
                parts.append(data)
            self._pixels[i] = _decode_image(
                self.index, fc.width, fc.height,
                b''.join(parts), self._aux_chunks())
            self.decoded.append(i)
        return self._pixels[i]

    def _default_rows(self):
        """Decoded RGBA8 rows of the static canvas (default image)."""
        if self._default is None:
            if not self.index.default_data:
                raise png.Error(
                    'default image is an animation frame; query frame 0')
            body = b''.join(
                self._read_span(span) for span in self.index.default_data)
            self._default = _decode_image(
                self.index, self.index.ihdr['width'],
                self.index.ihdr['height'], body, self._aux_chunks())
        return self._default

    def _needed_regions(self, frame, window):
        """Map frame index -> canvas rectangles whose rendered pixels
        are required to answer *window* at *frame*.

        Walks backwards from the requested frame.  A frame is skipped
        (its compressed body stays in cold storage) whenever control
        information or already decoded pixels prove it cannot
        influence the window: rectangles that do not intersect, areas
        replaced by a SOURCE blend, areas cleared by a BACKGROUND
        disposal, frames undone by a PREVIOUS disposal, and areas
        where an OVER frame is already fully opaque.
        """
        needed = {}
        region = [window]
        i = frame
        while i >= 0 and region:
            fc = self._control(i)
            inside = _intersect_all(region, fc.rect)
            prior = _subtract_all(region, fc.rect)
            if inside:
                if fc.blend_op == BLEND_OVER:
                    keep, semi = _alpha_split(
                        self._frame_rows(i), fc.rect, inside)
                    needed.setdefault(i, []).extend(keep)
                    prior.extend(semi)
                else:
                    needed.setdefault(i, []).extend(inside)
            # Translate the prior-canvas requirement through the
            # disposal chain to a requirement on an earlier display.
            j = i - 1
            while j >= 0 and self._control(j).dispose_op == DISPOSE_PREVIOUS:
                j -= 1
            if j < 0:
                # Only the initial blank canvas is required.
                region = []
            else:
                previous = self._control(j)
                if previous.dispose_op == DISPOSE_BACKGROUND:
                    region = _subtract_all(prior, previous.rect)
                else:
                    region = prior
            i = j
        return needed

    def _render(self, frame, window, needed):
        """Composite the window's buffer up to *frame*'s display.

        Only window-sized buffers are used; the canvas beyond the
        requested window is never materialized.
        """
        x0, y0, w, h = window
        buf = [[(0, 0, 0, 0)] * w for _ in range(h)]
        saved = None
        for i in range(frame + 1):
            fc = self._control(i)
            if fc.dispose_op == DISPOSE_PREVIOUS:
                saved = [row[:] for row in buf]
            rects = needed.get(i)
            if rects:
                rows = self._frame_rows(i)
                fx, fy = fc.x_offset, fc.y_offset
                for (rx, ry, rw, rh) in rects:
                    for yy in range(ry, ry + rh):
                        frow = rows[yy - fy]
                        brow = buf[yy - y0]
                        for xx in range(rx, rx + rw):
                            px = tuple(frow[4 * (xx - fx):4 * (xx - fx) + 4])
                            if fc.blend_op == BLEND_SOURCE:
                                brow[xx - x0] = px
                            else:
                                brow[xx - x0] = _over(px, brow[xx - x0])
            if i == frame:
                break
            if fc.dispose_op == DISPOSE_BACKGROUND:
                self._clear(buf, window, fc.rect)
            elif fc.dispose_op == DISPOSE_PREVIOUS:
                buf = saved
        return buf

    @staticmethod
    def _clear(buf, window, rect):
        r = _rect_intersect(window, rect)
        if r is None:
            return
        x0, y0 = window[0], window[1]
        for yy in range(r[1], r[1] + r[3]):
            row = buf[yy - y0]
            for xx in range(r[0], r[0] + r[2]):
                row[xx - x0] = (0, 0, 0, 0)

    def _check_window(self, window):
        x, y, w, h = window
        if w <= 0 or h <= 0:
            raise png.Error('window must not be empty')
        if (x < 0 or y < 0 or x + w > self.index.ihdr['width']
                or y + h > self.index.ihdr['height']):
            raise png.Error('window lies outside the canvas')

    def query(self, frame, x, y, w, h):
        """RGBA8 values of one window of one frame.

        *frame* is an animation frame index, or :data:`DEFAULT` for
        the static canvas.  The values are sampled when the frame has
        been completely displayed, before its disposal operation.
        Returns a :class:`WindowResult`.
        """
        window = (x, y, w, h)
        self._check_window(window)
        read_mark = len(self.reads)
        decoded_mark = len(self.decoded)
        if frame == DEFAULT:
            rows = self._default_rows()
            out = [[tuple(rows[y + dy][4 * (x + dx):4 * (x + dx) + 4])
                    for dx in range(w)] for dy in range(h)]
        else:
            frame = int(frame)
            if not 0 <= frame < len(self.index.frames):
                raise png.Error('no such animation frame: %r' % (frame,))
            needed = self._needed_regions(frame, window)
            out = self._render(frame, window, needed)
        return WindowResult(frame, window, out,
                            self.reads[read_mark:],
                            self.decoded[decoded_mark:])


def playback(source, frame, window):
    """Reference: one full playback, then crop *window*.

    Decodes every frame up to *frame* onto a full-size canvas and
    crops the window after the frame has been completely displayed,
    before its disposal.  *frame* is an animation frame index or
    :data:`DEFAULT` for the static canvas.  Returns RGBA8 rows in the
    same shape as :meth:`ApngWindows.query`.
    """

    index = register(source)
    close = False
    if isinstance(source, str):
        f = open(source, 'rb')
        close = True
    else:
        f = source
    try:
        def span_data(span):
            f.seek(span.offset)
            return f.read(span.length)

        aux = []
        for span in (index.plte, index.trns):
            if span is not None:
                aux.append((span.type, span_data(span)))

        if frame == DEFAULT:
            body = b''.join(span_data(s) for s in index.default_data)
            rows = _decode_image(index, index.ihdr['width'],
                                 index.ihdr['height'], body, aux)
            x, y, w, h = window
            return [[tuple(rows[y + dy][4 * (x + dx):4 * (x + dx) + 4])
                     for dx in range(w)] for dy in range(h)]

        width = index.ihdr['width']
        height = index.ihdr['height']
        canvas = [[(0, 0, 0, 0)] * width for _ in range(height)]
        saved = None
        for i in range(frame + 1):
            fc = FrameControl(span_data(index.frames[i]))
            parts = []
            for span in index.frame_data[i]:
                data = span_data(span)
                if span.type == b'fdAT':
                    data = data[4:]
                parts.append(data)
            rows = _decode_image(index, fc.width, fc.height,
                                 b''.join(parts), aux)
            if fc.dispose_op == DISPOSE_PREVIOUS:
                saved = [row[:] for row in canvas]
            fx, fy = fc.x_offset, fc.y_offset
            for yy in range(fc.height):
                crow = canvas[fy + yy]
                frow = rows[yy]
                for xx in range(fc.width):
                    px = tuple(frow[4 * xx:4 * xx + 4])
                    if fc.blend_op == BLEND_SOURCE:
                        crow[fx + xx] = px
                    else:
                        crow[fx + xx] = _over(px, crow[fx + xx])
            if i == frame:
                break
            if fc.dispose_op == DISPOSE_BACKGROUND:
                for yy in range(fc.y_offset, fc.y_offset + fc.height):
                    crow = canvas[yy]
                    for xx in range(fc.x_offset, fc.x_offset + fc.width):
                        crow[xx] = (0, 0, 0, 0)
            elif fc.dispose_op == DISPOSE_PREVIOUS:
                canvas = saved
        x, y, w, h = window
        return [row[x:x + w] for row in canvas[y:y + h]]
    finally:
        if close:
            f.close()
