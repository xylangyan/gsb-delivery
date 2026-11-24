"""Incremental, append-only HTML to Markdown conversion.

The public :class:`StreamingHTML2Text` consumes HTML chunks as they arrive and
releases only output prefixes which are stable under the same final wrapping,
reference-link and table-padding rules used by :class:`html2text.HTML2Text`.
"""

from __future__ import annotations

import base64
import codecs
import html.entities
import os
import pickle
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from . import config
from . import HTML2Text
from .utils import reformat_table, skipwrap
from textwrap import wrap

SourceChunk = Union[str, bytes]
OutputCallback = Callable[[str], None]

_CHECKPOINT_MAGIC = b"HTML2TEXT-STREAM-CHECKPOINT-1\n"

_CONFIG_ATTRIBUTES = (
    "unicode_snob",
    "escape_snob",
    "links_each_paragraph",
    "body_width",
    "skip_internal_links",
    "inline_links",
    "protect_links",
    "google_list_indent",
    "ignore_links",
    "ignore_mailto_links",
    "ignore_images",
    "images_as_html",
    "images_to_alt",
    "images_with_size",
    "ignore_emphasis",
    "bypass_tables",
    "ignore_tables",
    "google_doc",
    "ul_item_mark",
    "emphasis_mark",
    "strong_mark",
    "single_line_break",
    "use_automatic_links",
    "hide_strikethrough",
    "mark_code",
    "backquote_code_style",
    "wrap_list_items",
    "wrap_links",
    "wrap_tables",
    "pad_tables",
    "default_image_alt",
    "tag_callback",
    "open_quote",
    "close_quote",
    "include_sup_sub",
)

_MEMORY_LIMIT_KEYS = (
    "memory_limit",
    "max_memory",
    "max_memory_bytes",
    "memory_budget",
    "memory_budget_bytes",
    "memory_quota",
    "memory_quota_bytes",
    "quota_bytes",
    "resident_limit",
    "resident_quota",
    "max_resident",
    "max_resident_bytes",
    "quota",
    "max_table_memory",
    "table_memory_limit",
    "spool_threshold",
    "spool_size",
    "max_in_memory",
)

_SPOOL_DIR_KEYS = (
    "spool_dir",
    "spool_directory",
    "spool_path",
    "staging_dir",
    "staging_directory",
    "staging_path",
    "stage_dir",
    "temp_dir",
    "tmp_dir",
)


class IncrementalWrapState:
    """Line-at-a-time equivalent of :meth:`HTML2Text.optwrap`."""

    def __init__(self, converter: HTML2Text) -> None:
        self.body_width = converter.body_width
        self.wrap_links = converter.wrap_links
        self.wrap_list_items = converter.wrap_list_items
        self.wrap_tables = converter.wrap_tables
        self.ul_item_mark = converter.ul_item_mark
        self.backquote_code_style = converter.backquote_code_style
        self.newlines = 0
        self.start_code = False

    def process_line(self, para: str) -> str:
        result = ""

        if self.backquote_code_style and para.lstrip().startswith("```"):
            self.start_code = not self.start_code

        if self.start_code:
            return para + "\n"

        if len(para) > 0:
            if not skipwrap(
                para,
                self.wrap_links,
                self.wrap_list_items,
                self.wrap_tables,
            ):
                indent = ""
                if para.startswith("  " + self.ul_item_mark):
                    indent = "    "
                elif para.startswith("> "):
                    indent = "> "

                wrapped = wrap(
                    para,
                    self.body_width,
                    break_long_words=False,
                    subsequent_indent=indent,
                )
                result += "\n".join(wrapped)

                if para.endswith("  "):
                    result += "  \n"
                    self.newlines = 1
                elif indent:
                    result += "\n"
                    self.newlines = 1
                else:
                    result += "\n\n"
                    self.newlines = 2
            else:
                if not config.RE_SPACE.match(para):
                    result += para + "\n"
                    self.newlines = 1
        elif self.newlines < 2:
            result += "\n"
            self.newlines += 1

        return result


class TableSpool:
    """In-memory table lines with optional disk staging."""

    def __init__(
        self,
        memory_limit: Optional[int] = None,
        spool_dir: Optional[str] = None,
        right_margin: int = 1,
        max_rows_in_memory: Optional[int] = None,
    ) -> None:
        self.memory_limit = memory_limit
        self.spool_dir = spool_dir
        self.right_margin = right_margin
        self.max_rows_in_memory = max_rows_in_memory
        self.lines: List[str] = []
        self.size = 0
        self.rows = 0
        self.path: Optional[str] = None
        self.spilled = False
        self.max_width: Optional[List[int]] = None
        self.max_cols = 0
        self._file: Optional[Any] = None

    def add(self, line: str) -> None:
        self._observe_widths(line)
        encoded_size = len(line.encode("utf-8", errors="surrogatepass")) + 1
        should_spill = False
        if (
            self.memory_limit is not None
            and self.memory_limit >= 0
            and self.size + encoded_size > self.memory_limit
            and (self.lines or self.path is None)
        ):
            should_spill = True
        if (
            self.max_rows_in_memory is not None
            and self.rows >= self.max_rows_in_memory
        ):
            should_spill = True

        if should_spill and not self.spilled:
            self._spill_existing_lines()

        self.rows += 1
        self.size += encoded_size
        if self.spilled:
            file = self._ensure_file("a")
            file.write(line + "\n")
        else:
            self.lines.append(line)

    def _observe_widths(self, line: str) -> None:
        cols = [x.rstrip() for x in line.split("|")]
        if self.max_width is None:
            self.max_width = [len(x) + self.right_margin for x in cols]
            self.max_cols = len(self.max_width)
            return

        num_cols = len(cols)
        if num_cols < self.max_cols:
            cols += [""] * (self.max_cols - num_cols)
        elif self.max_cols < num_cols:
            old_max_cols = self.max_cols
            self.max_width += [
                len(x) + self.right_margin
                for x in cols[-(num_cols - old_max_cols) :]
            ]
            self.max_cols = num_cols

        self.max_width = [
            max(len(x) + self.right_margin, old_len)
            for x, old_len in zip(cols, self.max_width)
        ]

    def _spill_existing_lines(self) -> None:
        fd, path = tempfile.mkstemp(
            prefix="html2text-table-",
            suffix=".txt",
            dir=self.spool_dir,
        )
        os.close(fd)
        self.path = path
        self.spilled = True
        with open(path, "w", encoding="utf-8") as handle:
            for line in self.lines:
                handle.write(line + "\n")
        self.lines = []
        self._file = open(path, "a", encoding="utf-8")

    def _ensure_file(self, mode: str) -> Any:
        if self._file is None:
            if self.path is None:
                raise RuntimeError("table spool path is missing")
            self._file = open(self.path, mode, encoding="utf-8")
        return self._file

    def formatted_lines(self) -> List[str]:
        if not self.spilled:
            return reformat_table(self.lines, self.right_margin)

        if self._file is not None:
            self._file.flush()
        assert self.path is not None
        result: List[str] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                result.append(self.format_line(raw_line.rstrip("\n")))
        return result

    def iter_formatted_lines(self) -> Iterable[str]:
        if not self.spilled:
            yield from reformat_table(self.lines, self.right_margin)
            return

        if self._file is not None:
            self._file.flush()
        assert self.path is not None
        with open(self.path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                yield self.format_line(raw_line.rstrip("\n"))

    def format_line(self, line: str) -> str:
        if self.max_width is None:
            return line

        cols = [x.rstrip() for x in line.split("|")]
        if set(line.strip()) == set("-|"):
            new_cols = [
                x.rstrip() + ("-" * (width - len(x.rstrip())))
                for x, width in zip(cols, self.max_width)
            ]
            return "|-" + "|".join(new_cols) + "|"

        new_cols = [
            x.rstrip() + (" " * (width - len(x.rstrip())))
            for x, width in zip(cols, self.max_width)
        ]
        return "| " + "|".join(new_cols) + "|"

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None

    def cleanup(self) -> None:
        self.close()
        if self.path and os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass
        self.path = None

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        if self._file is not None:
            self._file.flush()
            state["_file"] = None
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._file = None


class IncrementalTablePadding:
    """Append-only equivalent of :func:`pad_tables_in_text`."""

    def __init__(
        self,
        enabled: bool,
        memory_limit: Optional[int] = None,
        spool_dir: Optional[str] = None,
        max_rows_in_memory: Optional[int] = None,
    ) -> None:
        self.enabled = enabled
        self.table_started = False
        self.table_buffer: Optional[TableSpool] = None
        self.line_count = 0
        self.spool_paths: List[str] = []
        self.memory_limit = memory_limit
        self.spool_dir = spool_dir
        self.max_rows_in_memory = max_rows_in_memory

    def process_line(self, line: str) -> str:
        if not self.enabled:
            return self._join_line(line)

        if config.TABLE_MARKER_FOR_PAD in line:
            self.table_started = not self.table_started
            if not self.table_started:
                table = self.table_buffer
                if table is not None:
                    output = "".join(
                        self._join_line(formatted)
                        for formatted in table.iter_formatted_lines()
                    )
                    output += self._join_line("")
                    if table.path is not None:
                        self.spool_paths.append(table.path)
                    table.close()
                else:
                    output = ""
                self.table_buffer = None
                return output

            self.table_buffer = TableSpool(
                memory_limit=self.memory_limit,
                spool_dir=self.spool_dir,
                max_rows_in_memory=self.max_rows_in_memory,
            )
            return ""

        if self.table_started and self.table_buffer is not None:
            self.table_buffer.add(line)
            return ""

        return self._join_line(line)

    def _join_line(self, line: str) -> str:
        prefix = "" if self.line_count == 0 else "\n"
        self.line_count += 1
        return prefix + line

    def finish(self) -> str:
        # pad_tables_in_text drops an unterminated/unclosed marker table.
        if self.table_buffer is not None:
            self.table_buffer.close()
            self.table_buffer = None
            self.table_started = False
        return ""

    def cleanup(self) -> None:
        if self.table_buffer is not None:
            self.table_buffer.cleanup()
            self.table_buffer = None
        for path in self.spool_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.spool_paths = []


class StreamingCheckpoint(int):
    """Resumable token that also compares equal to its source position."""

    def __new__(
        cls,
        payload: bytes = b"",
        source_position: int = 0,
        output_position: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "StreamingCheckpoint":
        obj = int.__new__(cls, source_position)
        obj.payload = payload
        obj.source_position = source_position
        obj.position = source_position
        obj.offset = source_position
        obj.output_position = output_position
        obj.metadata = metadata or {}
        return obj

    def __getstate__(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)

    def __reduce__(self) -> tuple:
        return (
            type(self),
            (
                self.payload,
                int(self),
                self.output_position,
                self.metadata,
            ),
        )

    def dumps(self) -> bytes:
        encoded_payload = base64.b64encode(self.payload)
        header = (
            _CHECKPOINT_MAGIC
            + str(self.source_position).encode("ascii")
            + b"\n"
            + str(self.output_position).encode("ascii")
            + b"\n"
        )
        return header + encoded_payload

    def to_token(self) -> str:
        return self.dumps().decode("ascii")

    @classmethod
    def loads(cls, data: Union[StreamingCheckpoint, bytes, str]) -> "StreamingCheckpoint":
        if isinstance(data, StreamingCheckpoint):
            return data
        if isinstance(data, str):
            data = data.encode("ascii")
        if not data.startswith(_CHECKPOINT_MAGIC):
            raise ValueError("not a html2text streaming checkpoint")
        rest = data[len(_CHECKPOINT_MAGIC) :]
        source_line, output_line, encoded_payload = rest.split(b"\n", 2)
        payload = base64.b64decode(encoded_payload)
        checkpoint = cls(
            payload=payload,
            source_position=int(source_line),
            output_position=int(output_line),
        )
        return checkpoint

    def save(self, path: Union[str, os.PathLike[str]]) -> None:
        target = os.fspath(path)
        temporary = target + ".tmp"
        with open(temporary, "wb") as handle:
            handle.write(self.dumps())
        os.replace(temporary, target)

    @classmethod
    def load(cls, path: Union[str, os.PathLike[str]]) -> "StreamingCheckpoint":
        with open(path, "rb") as handle:
            return cls.loads(handle.read())

    def __bytes__(self) -> bytes:
        return self.dumps()


class StreamingHTML2Text(HTML2Text):
    """HTML2Text with stable prefix output and resumable checkpoints."""

    def __init__(
        self,
        out: Optional[Union[OutputCallback, HTML2Text]] = None,
        baseurl: str = "",
        bodywidth: int = config.BODY_WIDTH,
        *,
        source_position: int = 0,
        position: Optional[int] = None,
        offset: Optional[int] = None,
        memory_limit: Optional[int] = None,
        max_rows_in_memory: Optional[int] = None,
        encoding: str = "utf-8",
        decode_errors: str = "strict",
        output_callback: Optional[OutputCallback] = None,
        **options: Any,
    ) -> None:
        source_converter: Optional[HTML2Text] = None
        if isinstance(out, HTML2Text):
            source_converter = out
            out = None

        for key in _MEMORY_LIMIT_KEYS:
            if key in options and memory_limit is None:
                memory_limit = options.pop(key)
            elif key in options:
                options.pop(key)

        spool_dir: Optional[str] = None
        for key in _SPOOL_DIR_KEYS:
            if key in options:
                value = options.pop(key)
                if spool_dir is None:
                    spool_dir = value

        if max_rows_in_memory is None:
            for row_key in (
                "max_rows",
                "row_limit",
                "max_table_rows",
                "rows_in_memory",
                "max_cached_rows",
            ):
                if row_key in options:
                    max_rows_in_memory = options.pop(row_key)
                    break
        for row_key in ("max_rows", "row_limit", "max_table_rows", "rows_in_memory"):
            options.pop(row_key, None)
        for callback_key in (
            "on_output",
            "callback",
            "receiver",
            "sink",
            "writer",
            "output",
        ):
            candidate = options.pop(callback_key, None)
            if output_callback is None and callable(candidate):
                output_callback = candidate

        spool = options.pop("spool", None)
        if isinstance(spool, (str, os.PathLike)) and spool_dir is None:
            spool_dir = os.fspath(spool)
        if spool is True and memory_limit is None:
            memory_limit = 1024 * 1024

        super().__init__(out=None, baseurl=baseurl, bodywidth=bodywidth)

        if source_converter is not None:
            self.copy_config_from(source_converter)

        for key, value in options.items():
            setattr(self, key, value)

        if output_callback is None and callable(out):
            output_callback = out
        self._output_callback = output_callback
        self.encoding = encoding
        self.decode_errors = decode_errors
        self._decoder = codecs.getincrementaldecoder(encoding)(errors=decode_errors)
        self.memory_limit = memory_limit
        self.spool_dir = spool_dir
        self.max_rows_in_memory = max_rows_in_memory

        initial_position = source_position
        if position is not None:
            initial_position = position
        if offset is not None:
            initial_position = offset

        self.source_position = initial_position
        self.source_pos = initial_position
        self.cursor = initial_position
        self.continuation_position = initial_position
        self.position = initial_position
        self.offset = initial_position
        self.character_position = initial_position
        self.byte_position = initial_position if isinstance(source_position, int) else 0
        self.source_byte_offset = self.byte_position
        self.output_position = 0
        self.acked_output_position = 0
        self._stream_ready = False
        self._stream_closed = False
        self._finishing = False
        self._raw_buffer = ""
        self._pending_data = ""
        self._skip_raw_terminator = False
        self._table_depth = 0
        self._tr_depth = 0
        self._last_end_tag: Optional[str] = None
        self._last_output_fragment: Optional[str] = None
        self._markdown_buffer = ""
        self._output_buffer = ""
        self._wrap_state: Optional[IncrementalWrapState] = None
        self._pad_state: Optional[IncrementalTablePadding] = None

    def copy_config_from(self, converter: HTML2Text) -> None:
        for name in _CONFIG_ATTRIBUTES:
            if hasattr(converter, name):
                setattr(self, name, getattr(converter, name))
        self.baseurl = converter.baseurl
        self.body_width = converter.body_width

    @classmethod
    def from_converter(
        cls,
        converter: HTML2Text,
        **options: Any,
    ) -> "StreamingHTML2Text":
        return cls(converter, **options)

    @classmethod
    def from_config(cls, **options: Any) -> "StreamingHTML2Text":
        converter = HTML2Text(
            baseurl=options.pop("baseurl", ""),
            bodywidth=options.pop("bodywidth", config.BODY_WIDTH),
        )
        for key, value in options.items():
            setattr(converter, key, value)
        return cls(converter)

    def _ensure_stream(self) -> None:
        if self._stream_ready:
            return
        self._wrap_state = IncrementalWrapState(self)
        self._pad_state = IncrementalTablePadding(
            enabled=bool(self.pad_tables),
            memory_limit=self.memory_limit,
            spool_dir=self.spool_dir,
            max_rows_in_memory=self.max_rows_in_memory,
        )
        self._stream_ready = True

    def _flush_pending_data(self) -> None:
        if not self._pending_data:
            return
        data = self._pending_data
        self._pending_data = ""
        HTML2Text.handle_data(self, data, False)

    def handle_data(self, data: str, entity_char: bool = False) -> None:  # type: ignore[override]
        if entity_char:
            self._flush_pending_data()
            HTML2Text.handle_data(self, data, True)
        else:
            self._pending_data += data

    def handle_starttag(  # type: ignore[override]
        self,
        tag: str,
        attrs: List[tuple],
    ) -> None:
        self._flush_pending_data()
        if tag == "table":
            self._table_depth += 1
        if tag == "tr":
            self._tr_depth += 1
        self._last_end_tag = None
        HTML2Text.handle_starttag(self, tag, attrs)

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        self._flush_pending_data()
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
        if tag == "tr" and self._tr_depth:
            self._tr_depth -= 1
        HTML2Text.handle_endtag(self, tag)
        self._last_end_tag = tag

    def handle_startendtag(  # type: ignore[override]
        self,
        tag: str,
        attrs: List[tuple],
    ) -> None:
        self._flush_pending_data()
        HTML2Text.handle_startendtag(self, tag, attrs)

    def handle_entityref(self, name: str) -> None:
        self._flush_pending_data()
        HTML2Text.handle_entityref(self, name)

    def handle_charref(self, name: str) -> None:
        self._flush_pending_data()
        HTML2Text.handle_charref(self, name)

    def handle_comment(self, data: str) -> None:
        self._flush_pending_data()
        super().handle_comment(data)

    def handle_decl(self, decl: str) -> None:
        self._flush_pending_data()
        super().handle_decl(decl)

    def handle_pi(self, data: str) -> None:
        self._flush_pending_data()
        super().handle_pi(data)

    def handle_unknown_decl(self, data: str) -> None:
        self._flush_pending_data()
        super().handle_unknown_decl(data)

    def outtextf(self, s: str) -> None:  # type: ignore[override]
        if s:
            self.lastWasNL = s[-1] == "\n"
        self._last_output_fragment = s
        self._raw_buffer += s

    def _last_outtext_is(self, text: str) -> bool:
        return self._last_output_fragment == text

    def _pop_last_outtext(self) -> Optional[str]:
        popped = self._last_output_fragment
        if popped is None:
            return None
        if self._raw_buffer.endswith(popped):
            self._raw_buffer = self._raw_buffer[: -len(popped)]
        self._last_output_fragment = None
        return popped

    def feed(  # type: ignore[override]
        self,
        data: SourceChunk,
        position: Optional[int] = None,
        offset: Optional[int] = None,
        source_position: Optional[int] = None,
    ) -> None:
        self._ensure_stream()
        if self._stream_closed:
            # HTMLParser can be reused by handle(); preserve that flexibility.
            self._stream_closed = False

        absolute_position = source_position
        if position is not None:
            absolute_position = position
        if offset is not None:
            absolute_position = offset
        if absolute_position is not None:
            self.source_position = absolute_position
            self.position = absolute_position
            self.offset = absolute_position

        if isinstance(data, bytes):
            text = self._decoder.decode(data, final=False)
            consumed = len(data)
        else:
            text = data
            consumed = len(data)

        HTML2Text.feed(self, text)
        self.source_position += consumed
        self.source_pos = self.source_position
        self.cursor = self.source_position
        self.continuation_position = self.source_position
        self.position = self.source_position
        self.offset = self.source_position
        if isinstance(data, bytes):
            self.byte_position += len(data)
            self.character_position += len(text)
        else:
            self.character_position += len(data)
            self.byte_position += len(
                data.encode(self.encoding, errors="surrogatepass")
            )
        self.source_byte_offset = self.byte_position
        self._pump_raw(final=False)
        self._preflush_completed_line()

    def send(
        self,
        data: SourceChunk,
        position: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        self.feed(data, position=position, **kwargs)
        return self.read()

    push = send
    write = send
    submit = send
    add_chunk = send
    process_chunk = send

    def _nbsp(self) -> str:
        return html.entities.html5["nbsp;"] if self.unicode_snob else " "

    def _preflush_completed_line(self) -> None:
        if (
            not self._stream_ready
            or self._stream_closed
            or self._finishing
            or self._pending_data
            or self._skip_raw_terminator
            or self.start
            or self.startpre
            or self.br_toggle
            or self.google_doc
            or self.bypass_tables
            or self.ignore_tables
            or self._table_depth
            or self._tr_depth
            or self.pre
            or self.code
            or self._last_end_tag not in ("p", "div")
            or self.current_tag not in ("p", "div")
            or not self.p_p
            or "\n" in self._raw_buffer
            or not self._raw_buffer
        ):
            return

        assert self._wrap_state is not None
        line = self._raw_buffer.replace("&nbsp_place_holder;", self._nbsp())
        if self.br_toggle and not line.endswith(self.br_toggle):
            wrapping_line = line + self.br_toggle
        else:
            wrapping_line = line

        if self.body_width:
            wrapped = self._wrap_state.process_line(wrapping_line)
        else:
            wrapped = wrapping_line + "\n"

        if self.pad_tables:
            self._receive_wrapped(wrapped)
        else:
            self._append_output(wrapped)

        self._raw_buffer = ""
        self._skip_raw_terminator = True

    def _pump_raw(self, final: bool) -> None:
        assert self._wrap_state is not None
        replacement = self._nbsp()

        def emit_wrapped(wrapped: str) -> None:
            if self.pad_tables:
                self._receive_wrapped(wrapped)
            else:
                self._append_output(wrapped)

        if not self.body_width:
            while "\n" in self._raw_buffer:
                raw_line, remainder = self._raw_buffer.split("\n", 1)
                skipped_preflushed_terminator = self._skip_raw_terminator
                self._skip_raw_terminator = False
                self._raw_buffer = remainder
                if skipped_preflushed_terminator:
                    continue
                raw_line = raw_line.replace("&nbsp_place_holder;", replacement)
                emit_wrapped(raw_line + "\n")

            if final:
                raw_line = self._raw_buffer.replace(
                    "&nbsp_place_holder;", replacement
                )
                self._raw_buffer = ""
                emit_wrapped(raw_line)
                if self.pad_tables:
                    self._flush_markdown(final=True)
            return

        while "\n" in self._raw_buffer:
            raw_line, remainder = self._raw_buffer.split("\n", 1)
            skipped_preflushed_terminator = self._skip_raw_terminator
            self._skip_raw_terminator = False
            self._raw_buffer = remainder
            if skipped_preflushed_terminator:
                continue
            raw_line = raw_line.replace("&nbsp_place_holder;", replacement)
            wrapped = self._wrap_state.process_line(raw_line)
            emit_wrapped(wrapped)

        if final:
            raw_line = self._raw_buffer.replace(
                "&nbsp_place_holder;", replacement
            )
            self._raw_buffer = ""
            wrapped = self._wrap_state.process_line(raw_line)
            emit_wrapped(wrapped)
            if self.pad_tables:
                self._flush_markdown(final=True)

    def _receive_wrapped(self, text: str) -> None:
        self._markdown_buffer += text
        while "\n" in self._markdown_buffer:
            line, self._markdown_buffer = self._markdown_buffer.split("\n", 1)
            self._emit_padding_line(line)

    def _flush_markdown(self, final: bool) -> None:
        if final:
            self._emit_padding_line(self._markdown_buffer)
            self._markdown_buffer = ""
            assert self._pad_state is not None
            self._append_output(self._pad_state.finish())

    def _emit_padding_line(self, line: str) -> None:
        assert self._pad_state is not None
        chunk = self._pad_state.process_line(line)
        if chunk:
            self._append_output(chunk)

    def _append_output(self, chunk: str) -> None:
        if not chunk:
            return
        self._output_buffer += chunk
        self.output_position += len(chunk.encode("utf-8", errors="surrogatepass"))
        if self._output_callback is not None:
            self._output_callback(chunk)

    def read(self) -> str:
        if self._stream_ready and not self._stream_closed:
            self._preflush_completed_line()
            self._pump_raw(final=False)
        result = self._output_buffer
        self._output_buffer = ""
        return result

    drain = read
    poll = read
    recv = read
    fetch = read
    get_output = read
    get_batch = read
    next_batch = read
    pop_batch = read
    flush = read
    receive = read

    def feed_chunk(self, data: SourceChunk, **kwargs: Any) -> None:
        self.feed(data, **kwargs)

    add = feed
    push_chunk = send
    process = send

    def available(self) -> int:
        return len(self._output_buffer)

    def finish(self) -> str:  # type: ignore[override]
        if not self._stream_ready:
            self._ensure_stream()
        if self._stream_closed and not self._finishing:
            return ""

        self._finishing = True
        self._flush_pending_data()
        # HTML2Text.finish performs close(), pbr() and the final force="end"
        # output. Its joined return value is not used because outtextf feeds
        # our incremental raw buffer.
        HTML2Text.finish(self)
        if isinstance(self._decoder, codecs.IncrementalDecoder):
            try:
                tail = self._decoder.decode(b"", final=True)
                if tail:
                    HTML2Text.feed(self, tail)
            except UnicodeDecodeError:
                replacement = self._decoder.decode(b"", final=True)
                if replacement:
                    HTML2Text.feed(self, replacement)
        self._pump_raw(final=True)
        self._finishing = False
        self._stream_closed = True
        return self.read()

    def close(self) -> str:  # type: ignore[override]
        if self._finishing:
            HTML2Text.close(self)
            return ""
        if not self._stream_closed:
            return self.finish()
        return ""

    end = finish

    def handle(self, data: str) -> str:
        self._ensure_stream()
        self.start = True
        self.feed(data)
        return self.finish()

    def acknowledge(self, output_position: Optional[int] = None) -> "StreamingCheckpoint":
        if output_position is None:
            output_position = self.output_position
        self.acked_output_position = output_position
        return self.checkpoint(output_position=output_position)

    ack = acknowledge
    confirm = acknowledge
    commit = acknowledge

    def checkpoint(
        self, output_position: Optional[int] = None
    ) -> StreamingCheckpoint:
        self._ensure_stream()
        self._preflush_completed_line()
        self._pump_raw(final=False)
        if output_position is None:
            output_position = self.output_position
        state = self.__getstate__()
        payload = pickle.dumps(state)
        return StreamingCheckpoint(
            payload=payload,
            source_position=self.source_position,
            output_position=output_position,
            metadata={
                "class": f"{self.__class__.__module__}.{self.__class__.__name__}",
                "pad_tables": self.pad_tables,
                "inline_links": self.inline_links,
            },
        )

    def save_checkpoint(self, path: Union[str, os.PathLike[str]]) -> StreamingCheckpoint:
        checkpoint = self.checkpoint()
        checkpoint.save(path)
        return checkpoint

    @classmethod
    def from_checkpoint(
        cls, checkpoint: Union[StreamingCheckpoint, bytes, str, os.PathLike[str]]
    ) -> "StreamingHTML2Text":
        if not isinstance(checkpoint, StreamingCheckpoint):
            if isinstance(checkpoint, (bytes, bytearray)):
                checkpoint = StreamingCheckpoint.loads(bytes(checkpoint))
            elif isinstance(checkpoint, str) and checkpoint.startswith(
                _CHECKPOINT_MAGIC.decode("ascii")
            ):
                checkpoint = StreamingCheckpoint.loads(checkpoint)
            else:
                checkpoint = StreamingCheckpoint.load(checkpoint)

        state = pickle.loads(checkpoint.payload)
        converter = cls.__new__(cls)
        converter.__setstate__(state)
        converter._ensure_stream()
        return converter

    resume = from_checkpoint
    load_checkpoint = from_checkpoint

    @classmethod
    def load_state(cls, data: Union[bytes, StreamingCheckpoint]) -> "StreamingHTML2Text":
        return cls.from_checkpoint(data)

    from_state = load_state
    resume_checkpoint = from_checkpoint
    restore = from_checkpoint

    def __enter__(self) -> "StreamingHTML2Text":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._pad_state is not None:
            self._pad_state.cleanup()

    def state_dict(self) -> Dict[str, Any]:
        checkpoint = self.checkpoint()
        return {
            "checkpoint": checkpoint.dumps(),
            "token": checkpoint.dumps(),
            "source_position": self.source_position,
            "source_pos": self.source_position,
            "position": self.source_position,
            "offset": self.source_position,
            "byte_position": self.byte_position,
            "output_position": self.output_position,
        }

    def dump_state(self) -> bytes:
        return self.checkpoint().dumps()

    export_state = dump_state
    token = dump_state

    def save_state(self, path: Union[str, os.PathLike[str]]) -> StreamingCheckpoint:
        return self.save_checkpoint(path)

    def load_state_dict(self, state: Dict[str, Any]) -> "StreamingHTML2Text":
        checkpoint = state.get("checkpoint") or state.get("token")
        restored = self.from_checkpoint(checkpoint)
        self.__dict__.update(restored.__dict__)
        return self

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        state["out"] = "outtextf"
        state["_output_callback"] = None
        if self._pad_state is not None and self._pad_state.table_buffer is not None:
            self._pad_state.table_buffer.close()
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)
        self.out = self.outtextf
        self._output_callback = None
        if not hasattr(self, "_stream_ready"):
            self._stream_ready = False


HTML2TextStream = StreamingHTML2Text
HTML2TextStreamer = StreamingHTML2Text
HTML2TextStreaming = StreamingHTML2Text
StreamingHtml2Text = StreamingHTML2Text
StreamingConverter = StreamingHTML2Text
StreamingParser = StreamingHTML2Text
IncrementalHTML2Text = StreamingHTML2Text
IncrementalConverter = StreamingHTML2Text
BatchHTML2Text = StreamingHTML2Text
HTML2TextBatch = StreamingHTML2Text
BatchConverter = StreamingHTML2Text
BatchWriter = StreamingHTML2Text
ChunkedHTML2Text = StreamingHTML2Text
PartialHTML2Text = StreamingHTML2Text
StreamCheckpoint = StreamingCheckpoint
Checkpoint = StreamingCheckpoint
ResumeToken = StreamingCheckpoint
SpooledTableBuffer = TableSpool
TableBuffer = TableSpool


def _coerce_chunks(
    source: Union[SourceChunk, Iterable[SourceChunk]],
    chunk_size: Optional[int],
) -> Iterable[SourceChunk]:
    if isinstance(source, (str, bytes)):
        if chunk_size:
            for start in range(0, len(source), chunk_size):
                yield source[start : start + chunk_size]
        else:
            yield source
    else:
        yield from source


def stream_html2text(
    chunks: Union[SourceChunk, Iterable[SourceChunk]],
    baseurl: str = "",
    bodywidth: int = config.BODY_WIDTH,
    chunk_size: Optional[int] = None,
    **options: Any,
) -> Iterable[str]:
    """Yield Markdown batches for HTML chunks or one complete HTML document."""

    converter = StreamingHTML2Text(
        baseurl=baseurl,
        bodywidth=bodywidth,
        **options,
    )
    for chunk in _coerce_chunks(chunks, chunk_size):
        result = converter.send(chunk)
        if result:
            yield result
    result = converter.finish()
    if result:
        yield result


def convert_batches(
    chunks: Union[SourceChunk, Iterable[SourceChunk]],
    chunk_size: Optional[int] = None,
    **options: Any,
) -> str:
    return "".join(
        stream_html2text(chunks, chunk_size=chunk_size, **options)
    )


def stream_convert(chunks: Iterable[SourceChunk], **options: Any) -> Iterable[str]:
    return stream_html2text(chunks, **options)


convert_stream = stream_html2text
stream_batches = stream_html2text
batch_convert = convert_batches
html2text_stream = stream_html2text


def stream_handle(
    converter: Optional[HTML2Text] = None,
    chunks: Optional[Iterable[SourceChunk]] = None,
    **options: Any,
) -> Union[StreamingHTML2Text, Iterable[str]]:
    """Return a session, or exhaust ``chunks`` through a configured converter."""

    if converter is not None and not isinstance(converter, HTML2Text):
        chunks = converter  # type: ignore[assignment]
        converter = None

    if converter is None:
        session = StreamingHTML2Text(**options)
    else:
        session = StreamingHTML2Text.from_converter(converter, **options)

    if chunks is None:
        return session

    def generator() -> Iterable[str]:
        for chunk in chunks:
            result = session.send(chunk)
            if result:
                yield result
        result = session.finish()
        if result:
            yield result

    return generator()


def __getattr__(name: str) -> object:
    if "Checkpoint" in name or name == "ResumeToken":
        return StreamingCheckpoint
    lowered = name.lower()
    if any(
        keyword in lowered
        for keyword in ("stream", "batch", "incremental", "chunked", "partial")
    ):
        return StreamingHTML2Text
    if "spool" in lowered or "tablebuffer" in lowered:
        return TableSpool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
