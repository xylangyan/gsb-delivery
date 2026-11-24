"""Build XDF bytes for recovery tests, with exact control over every byte."""

from __future__ import annotations

import struct

import numpy as np


def varlen(n: int) -> bytes:
    if n < 256:
        return b"\x01" + struct.pack("<B", n)
    if n < 2**32:
        return b"\x04" + struct.pack("<I", n)
    return b"\x08" + struct.pack("<Q", n)


def chunk(tag: int, content: bytes, stream_id: int | None = None) -> bytes:
    body = struct.pack("<H", tag)
    if stream_id is not None:
        body += struct.pack("<I", stream_id)
    body += content
    return varlen(len(body)) + body


def file_header(version: str = "1.0") -> bytes:
    return chunk(
        1, f"<?xml version='1.0'?><info><version>{version}</version></info>".encode()
    )


def stream_header(
    stream_id: int,
    *,
    name: str = "Test",
    type: str = "EEG",
    nchannels: int = 1,
    srate: float = 100,
    fmt: str = "float32",
    uid: str | None = None,
    source_id: str | None = None,
) -> bytes:
    parts = [
        "<?xml version='1.0'?><info>",
        f"<name>{name}</name>",
        f"<type>{type}</type>",
        f"<channel_count>{nchannels}</channel_count>",
        f"<nominal_srate>{srate}</nominal_srate>",
        f"<channel_format>{fmt}</channel_format>",
    ]
    if uid is not None:
        parts.append(f"<uid>{uid}</uid>")
    if source_id is not None:
        parts.append(f"<source_id>{source_id}</source_id>")
    parts.append("</info>")
    return chunk(2, "".join(parts).encode(), stream_id)


def clock_offset(stream_id: int, time: float, value: float) -> bytes:
    return chunk(4, struct.pack("<dd", time, value), stream_id)


def _numeric_payload(values, flag: bool, stamp: float, dtype) -> bytes:
    payload = b"\x01" if flag else b"\x00"
    if flag:
        payload += struct.pack("<d", stamp)
    payload += np.asarray(values, dtype=dtype.newbyteorder("<")).tobytes()
    return payload


def samples_chunk(
    stream_id: int,
    values,
    stamps,
    flags,
    *,
    fmt: str = "float32",
    nchannels: int | None = None,
) -> bytes:
    """Encode one samples chunk.

    ``values`` is a sequence of samples; each is a number, a sequence of numbers
    or a string.  ``flags`` says whether that sample carries an explicit time
    stamp in ``stamps``.
    """
    if nchannels is None:
        first = values[0] if len(values) else 0
        nchannels = 1 if np.isscalar(first) or isinstance(first, str) else len(first)
    parts = [varlen(len(values))]
    if fmt == "string":
        for value, stamp, flag in zip(values, stamps, flags):
            parts.append(b"\x01" if flag else b"\x00")
            if flag:
                parts.append(struct.pack("<d", stamp))
            channels = value if isinstance(value, (list, tuple)) else [value]
            for text in channels:
                encoded = str(text).encode()
                parts.append(varlen(len(encoded)))
                parts.append(encoded)
    else:
        dtype = {
            "double64": np.dtype("<f8"),
            "float32": np.dtype("<f4"),
            "int32": np.dtype("<i4"),
            "int16": np.dtype("<i2"),
            "int8": np.dtype("<i1"),
            "int64": np.dtype("<i8"),
        }[fmt]
        for value, stamp, flag in zip(values, stamps, flags):
            parts.append(_numeric_payload(value, flag, stamp, dtype))
    return chunk(3, b"".join(parts), stream_id)


def string_samples_chunk(stream_id, texts, stamps, flags, nchannels=1):
    return samples_chunk(
        stream_id, texts, stamps, flags, fmt="string", nchannels=nchannels
    )


def assemble(*parts: bytes) -> bytes:
    return b"XDF:" + b"".join(parts)
