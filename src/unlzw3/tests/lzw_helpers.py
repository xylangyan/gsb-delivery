"""
Minimal LZW compressor used only to build deterministic .Z test fixtures.

The system ``compress`` binary cannot be driven with every combination of
block mode, code width and dictionary reset policy that the comparison code has
to handle.  This helper writes streams that the reference :func:`unlzw3.unlzw`
reader accepts.  It deliberately mirrors that reader's slot numbering,
including the never-walked slot 256 allocated by the first code after a
dictionary clear.
"""

from __future__ import annotations


class _BitWriter:
    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0
        self.bit_pos = 0

    def write(self, code: int, bits: int) -> None:
        self.acc |= code << self.nbits
        self.nbits += bits
        self.bit_pos += bits
        while self.nbits >= 8:
            self.out.append(self.acc & 0xFF)
            self.acc >>= 8
            self.nbits -= 8

    def align(self, mark_bit: int, bits: int) -> int:
        """Pad to the next 8*bits boundary, returning the new mark position."""
        if self.nbits:
            self.out.append(self.acc & 0xFF)
            self.acc = 0
            self.bit_pos += 8 - self.nbits
            self.nbits = 0
        boundary = ((self.bit_pos - mark_bit + bits * 8 - 1) // (bits * 8)) * (bits * 8)
        boundary += mark_bit
        if boundary > self.bit_pos:
            self.out.extend(b"\x00" * ((boundary - self.bit_pos) // 8))
            self.bit_pos = boundary
        return self.bit_pos

    def finish(self) -> bytes:
        if self.nbits:
            self.out.append(self.acc & 0xFF)
            self.nbits = 0
        return bytes(self.out)


def lzw_compress(data: bytes, maxbits: int = 12, block: bool = True) -> bytes:
    """Compress ``data`` into an LZW .Z byte string."""
    if maxbits == 9:
        maxbits = 10  # header value 9 is treated as 10 by the reference reader
    header = bytes([0x1F, 0x9D, (0x80 if block else 0x00) | maxbits])
    if not data:
        return header

    # Phase 1: choose codes and dictionary clears using slot counts only.
    # Read widths are assigned later from the decoder-faithful state machine.
    table = {bytes([i]): i for i in range(256)}
    next_code = 257 if block else 256
    symbols: list[int] = []
    w = b""
    for value in data:
        wc = w + bytes([value])
        if wc in table:
            w = wc
            continue
        symbols.append(table[w])
        if next_code <= (1 << maxbits) - 1:
            table[wc] = next_code
            next_code += 1
        elif block:
            # Table exhausted at the maximum width: request a reset.  Slot 256
            # stays reserved as the (phantom) clear slot, so new phrases resume
            # at 257 exactly as in the initial table.
            symbols.append(256)
            table = {bytes([i]): i for i in range(256)}
            next_code = 257
        w = bytes([value])
    symbols.append(table[w])

    # Phase 2: pack codes, inserting the same alignment padding the decoder
    # skips on width changes and dictionary clears.
    writer = _BitWriter()
    mark_bit = 0
    current_bits = 9
    current_mask = 0x1FF
    end = 256 if block else 255
    for index, code in enumerate(symbols):
        if end >= current_mask and current_bits < maxbits:
            mark_bit = writer.align(mark_bit, current_bits)
            current_bits += 1
            current_mask = (current_mask << 1) | 1
        writer.write(code, current_bits)
        if code == 256 and block:
            mark_bit = writer.align(mark_bit, current_bits)
            current_bits = 9
            current_mask = 0x1FF
            end = 255
            continue
        # The stream's first code is preloaded by the reader and never creates
        # a table entry, in either block or non-block mode.
        if index != 0:
            if end < current_mask:
                end += 1
    return header + writer.finish()
