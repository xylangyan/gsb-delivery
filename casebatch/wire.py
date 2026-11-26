from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

CRLF_RE = re.compile(rb"\r\n|\r|\n")


def normalize_append_bytes(data: bytes) -> bytes:
    """Match imaplib/IMAPClient.append newline normalization."""
    return CRLF_RE.sub(b"\r\n", data)


def quote_mailbox(folder: Any) -> bytes:
    if isinstance(folder, str):
        try:
            from ..imapclient.imap_utf7 import encode as encode_utf7
        except ImportError:  # pragma: no cover - supports standalone file use
            from imapclient.imap_utf7 import encode as encode_utf7

        raw = encode_utf7(folder)
    elif isinstance(folder, bytearray):
        raw = bytes(folder)
    else:
        raw = bytes(folder)
    raw = raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    return b'"' + raw + b'"'


def flags_token(flags: Optional[Sequence[Any]]) -> bytes:
    if flags is None:
        values: Sequence[Any] = ()
    elif isinstance(flags, (str, bytes)):
        values = (flags,)
    else:
        values = tuple(flags)

    parts: List[str] = []
    for flag in values:
        if isinstance(flag, bytes):
            parts.append(flag.decode("ascii"))
        else:
            parts.append(str(flag))
    return ("(" + " ".join(parts) + ")").encode("ascii")


@dataclass(frozen=True)
class PreparedMessage:
    length: int
    flags: bytes = b"()"
    date: Optional[bytes] = None
    source: str = ""
    case_id: Any = None
    raw_length: Optional[int] = None
    normalized_sha256: Optional[str] = None
    data: bytes = b""


@dataclass
class WirePart:
    begin: int
    end: int
    kind: str
    message_index: Optional[int] = None
    case_id: Any = None
    source: str = ""


@dataclass
class BudgetBoundary:
    boundary_id: str
    offset: int
    sent_bytes: int
    message_index: Optional[int] = None


@dataclass
class BudgetViolation:
    ok: bool
    boundary_id: Optional[str] = None
    limit_kind: Optional[str] = None
    limit: Optional[int] = None
    sent_bytes: Optional[int] = None
    overflow_by: int = 0
    message_number: Optional[int] = None
    case_id: Any = None
    source: str = ""
    part: Optional[str] = None
    byte_in_part: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "boundary": self.boundary_id,
            "limit_kind": self.limit_kind,
            "limit": self.limit,
            "sent_bytes": self.sent_bytes,
            "overflow_by": self.overflow_by,
            "message_number": self.message_number,
            "case_id": self.case_id,
            "source": self.source,
            "part": self.part,
            "byte_in_part": self.byte_in_part,
        }


@dataclass
class WireReport:
    total_bytes: int
    literal_plus: bool
    parts: List[WirePart] = field(default_factory=list)
    boundaries: List[BudgetBoundary] = field(default_factory=list)
    length_fields: List[Dict[str, Any]] = field(default_factory=list)
    wire_preview: Optional[bytes] = None

    def _part_at(self, offset: int) -> Optional[WirePart]:
        for part in self.parts:
            if part.begin <= offset < part.end:
                return part
        if self.parts and offset == self.parts[-1].end:
            return self.parts[-1]
        return None

    def _culprit(self, offset: int) -> Dict[str, Any]:
        part = self._part_at(offset)
        if part is None:
            return {"part": "unknown", "global_offset": offset}
        return {
            "part": part.kind,
            "byte_in_part": offset - part.begin + 1,
            "global_offset": offset,
            "message_number": part.message_index + 1
            if part.message_index is not None
            else None,
            "case_id": part.case_id,
            "source": part.source,
        }

    def check_budget(
        self,
        burst_limit: Optional[int],
        command_limit: Optional[int],
    ) -> BudgetViolation:
        previous = 0
        for boundary in self.boundaries:
            interval_bytes = boundary.offset - previous
            if burst_limit is not None and interval_bytes > burst_limit:
                culprit_offset = previous + burst_limit
                culprit = self._culprit(culprit_offset)
                return BudgetViolation(
                    ok=False,
                    boundary_id=boundary.boundary_id,
                    limit_kind="between_responses",
                    limit=burst_limit,
                    sent_bytes=interval_bytes,
                    overflow_by=interval_bytes - burst_limit,
                    message_number=culprit.get("message_number"),
                    case_id=culprit.get("case_id"),
                    source=culprit.get("source", ""),
                    part=culprit.get("part"),
                    byte_in_part=culprit.get("byte_in_part"),
                )
            previous = boundary.offset

        if command_limit is not None and self.total_bytes > command_limit:
            culprit = self._culprit(command_limit)
            return BudgetViolation(
                ok=False,
                boundary_id="command_total",
                limit_kind="command",
                limit=command_limit,
                sent_bytes=self.total_bytes,
                overflow_by=self.total_bytes - command_limit,
                message_number=culprit.get("message_number"),
                case_id=culprit.get("case_id"),
                source=culprit.get("source", ""),
                part=culprit.get("part"),
                byte_in_part=culprit.get("byte_in_part"),
            )

        return BudgetViolation(ok=True)


def build_command_wire(
    messages: Sequence[PreparedMessage],
    folder: Any,
    *,
    tag: bytes = b"tag",
    literal_plus: bool = False,
    include_preview: bool = False,
) -> WireReport:
    """Build the exact byte stream and server-response checkpoints.

    The emission order intentionally mirrors IMAPClient._raw_command and
    _send_literal, including its separator placement.
    """
    if isinstance(tag, str):
        tag = tag.encode("ascii")

    report = WireReport(total_bytes=0, literal_plus=literal_plus)
    preview = bytearray() if include_preview else None
    line: List[bytes] = [tag, b"APPEND", quote_mailbox(folder)]

    def emit(
        chunk: bytes,
        kind: str,
        message_index: Optional[int] = None,
        case_id: Any = None,
        source: str = "",
    ) -> None:
        if not chunk:
            return
        begin = report.total_bytes
        end = begin + len(chunk)
        report.parts.append(
            WirePart(begin, end, kind, message_index, case_id, source)
        )
        report.total_bytes = end
        if preview is not None:
            preview.extend(chunk)

    arg_count = len(messages)
    for message_index, message in enumerate(messages):
        if message.flags is not None:
            line.append(message.flags)
        if message.date is not None:
            line.append(message.date)

        is_last_argument = message_index == arg_count - 1

        if line:
            emit(b" ".join(line), "command-line")
            line = []

        length_text = str(message.length).encode("ascii")
        plus = b"+" if literal_plus else b""
        marker = b" {" + length_text + plus + b"}\r\n"
        report.length_fields.append(
            {
                "message_number": message_index + 1,
                "case_id": message.case_id,
                "source": message.source,
                "length": message.length,
                "raw_length": message.raw_length,
                "marker": marker.decode("ascii"),
                "marker_bytes": len(marker),
                "length_digits": len(length_text),
            }
        )
        emit(
            marker,
            "literal-marker-plus" if literal_plus else "literal-marker",
            message_index,
            message.case_id,
            message.source,
        )

        if not literal_plus:
            report.boundaries.append(
                BudgetBoundary(
                    f"continuation:{message_index + 1}",
                    report.total_bytes,
                    report.total_bytes
                    - (
                        report.boundaries[-1].offset
                        if report.boundaries
                        else 0
                    ),
                    message_index + 1,
                )
            )

        if len(message.data) == message.length:
            data_chunk = message.data
        else:
            data_chunk = b"\0" * message.length
        emit(
            data_chunk,
            "literal-data",
            message_index,
            message.case_id,
            message.source,
        )

        if not is_last_argument:
            emit(
                b" ",
                "post-literal-separator",
                message_index,
                message.case_id,
                message.source,
            )

    if line:
        emit(b" ".join(line), "command-line")

    emit(b"\r\n", "command-end")
    report.boundaries.append(
        BudgetBoundary("command-complete", report.total_bytes, 0, None)
    )

    if include_preview:
        report.wire_preview = bytes(preview)
    return report
