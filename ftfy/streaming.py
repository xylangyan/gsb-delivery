"""Disk-backed streaming equivalents of :func:`ftfy.fix_and_explain`.

The ordinary ftfy API fixes one complete segment in memory.  This module keeps
the source and intermediate passes in temporary files, so a whole volume can
have the same global decisions and explanation as one segment without holding
the volume in memory.
"""

from __future__ import annotations

import codecs
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable

import ftfy
from ftfy import fixes
from ftfy.badness import BADNESS_RE
from ftfy.chardata import (
    ALTERED_UTF8_RE,
    CHARMAP_ENCODINGS,
    C1_CONTROL_RE,
    LOSSY_UTF8_RE,
    UTF8_DETECTOR_RE,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from ftfy import ExplanationStep, TextFixerConfig

__all__ = [
    "ChunkIterator",
    "ChunkedFixer",
    "LargeTextFixer",
    "SpooledFixer",
    "SpooledTextFixer",
    "StreamFixResult",
    "StreamFixer",
    "StreamingFixer",
    "StreamingTextFixer",
    "VolumeFixer",
    "chunked_fix_and_explain",
    "fix_and_explain_chunked",
    "fix_and_explain_in_stream",
    "fix_and_explain_iter",
    "fix_and_explain_spooled",
    "fix_and_explain_stream",
    "fix_and_explain_streaming",
    "fix_book",
    "fix_large_text",
    "fix_stream",
    "fix_text_stream",
    "fix_volume",
    "fix_volume_and_explain",
    "iter_fix_and_explain",
    "iter_fixed_utf8",
    "spool_fix_and_explain",
    "stream_fix",
    "stream_fix_and_explain",
    "stream_fix_text",
    "stream_text",
    "streaming_fix_and_explain",
]

_SCAN_BUFFER_SIZE = 262_144
_DEFAULT_BUFFER_SIZE = 65_536
_MEMORY_LIMIT = 65_536
_REGEX_OVERLAP = 4_096


class StreamFixResult:
    """A completed spooled fix and a readable stream of its UTF-8 result."""

    def __init__(
        self,
        output_path: str | Path,
        explanation: list[ExplanationStep] | None,
        *,
        input_path: str | Path | None = None,
        work_dir: str | Path | None = None,
        config: TextFixerConfig | None = None,
        char_count: int | None = None,
    ) -> None:
        self.output_path = Path(output_path)
        self.explanation = explanation
        self.steps = explanation
        self.plan = explanation
        self.input_path = Path(input_path) if input_path is not None else None
        self.work_dir = Path(work_dir) if work_dir is not None else None
        self.config = config
        self.char_count = char_count

    def iter_bytes(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> Iterator[bytes]:
        """Yield the complete fixed text as UTF-8 byte chunks."""
        with self.output_path.open("rb") as output:
            while True:
                chunk = output.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def chunks(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> Iterator[bytes]:
        """Alias for :meth:`iter_bytes`."""
        return self.iter_bytes(chunk_size)

    def iter_utf8(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> Iterator[bytes]:
        """Alias for :meth:`iter_bytes`."""
        return self.iter_bytes(chunk_size)

    def iter_text(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> Iterator[str]:
        """Yield the fixed text as decoded string chunks."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogatepass")
        for chunk in self.iter_bytes(chunk_size):
            text_chunk = decoder.decode(chunk, final=False)
            if text_chunk:
                yield text_chunk
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail

    def read_bytes(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> bytes:
        """Read the whole result. Prefer :meth:`iter_bytes` for large volumes."""
        return b"".join(self.iter_bytes(chunk_size))

    def read_text(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> str:
        """Read the whole result as text. Prefer :meth:`iter_text` for large volumes."""
        return self.read_bytes(chunk_size).decode("utf-8", errors="surrogatepass")

    @property
    def bytes(self) -> bytes:
        return self.read_bytes()

    @property
    def utf8(self) -> bytes:
        return self.read_bytes()

    @property
    def text(self) -> str:
        return self.read_text()

    @property
    def fixed(self) -> str:
        return self.read_text()

    @property
    def fixed_text(self) -> str:
        return self.read_text()

    def to_explained_text(self) -> ftfy.ExplainedText:
        """Return the ordinary in-memory :class:`ftfy.ExplainedText` value."""
        return ftfy.ExplainedText(self.read_text(), self.explanation)

    def write_to(self, destination: str | Path | BinaryIO) -> Path | int:
        """Copy the fixed UTF-8 result to a path or binary file object."""
        if hasattr(destination, "write"):
            written = 0
            target = destination
            for chunk in self.iter_bytes():
                written += target.write(chunk)
            return written
        destination_path = Path(destination)
        shutil.copyfileobj(self.output_path.open("rb"), destination_path.open("wb"))
        return destination_path

    def __iter__(self) -> Iterator[bytes]:
        return self.iter_bytes()

    def __fspath__(self) -> str:
        return str(self.output_path)

    def __enter__(self) -> StreamFixResult:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Delete the temporary directory used by this result, if one exists."""
        if self.work_dir is not None and self.work_dir.exists():
            shutil.rmtree(self.work_dir, ignore_errors=True)

    def close(self) -> None:
        self.cleanup()


class _Workspace:
    def __init__(self, temp_dir: str | Path | None = None) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="ftfy-stream-", dir=temp_dir))
        self.counter = 0

    def new(self, suffix: str = ".txt") -> Path:
        self.counter += 1
        return self.path / f"pass-{self.counter:04d}{suffix}"

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def fix_volume(
    source: Any = None,
    config: TextFixerConfig | None = None,
    chunk_size: int = _DEFAULT_BUFFER_SIZE,
    buffer_size: int | None = None,
    temp_dir: str | Path | None = None,
    *,
    chunks: Iterable[Any] | None = None,
    read_size: int | None = None,
    block_size: int | None = None,
    directory: str | Path | None = None,
    tmp_dir: str | Path | None = None,
    tmpdir: str | Path | None = None,
    tmp_path: str | Path | None = None,
    dir: str | Path | None = None,
    memory_limit: int | None = _MEMORY_LIMIT,
    reader: Callable[[int], Any] | None = None,
    read: Callable[[int], Any] | None = None,
    output: str | Path | BinaryIO | None = None,
    output_path: str | Path | None = None,
    **kwargs: Any,
) -> StreamFixResult:
    """Fix one complete volume while spooling data and intermediates to disk.

    ``source`` may be an iterable of ``str``/``bytes`` chunks, a binary/text
    file object, a zero/one-argument reader callable, or a filesystem path.
    The source is consumed once; all later passes read temporary files.
    """
    if isinstance(source, ftfy.TextFixerConfig) and not isinstance(
        config, ftfy.TextFixerConfig
    ):
        source, config = config, source  # type: ignore[assignment]

    if hasattr(chunk_size, "__fspath__") or isinstance(chunk_size, str):
        temp_dir = chunk_size  # type: ignore[assignment]
        chunk_size = _DEFAULT_BUFFER_SIZE
    if buffer_size is not None and (
        hasattr(buffer_size, "__fspath__") or isinstance(buffer_size, str)
    ):
        temp_dir = buffer_size  # type: ignore[assignment]
        buffer_size = None
    if not isinstance(chunk_size, int) or chunk_size < 1:
        chunk_size = _DEFAULT_BUFFER_SIZE

    if chunks is not None and source is None:
        source = chunks
    if reader is not None:
        source = reader
    if read is not None:
        source = read

    source_chunk_size = read_size or chunk_size
    internal_buffer_size = buffer_size or block_size
    spool_dir = directory if directory is not None else temp_dir
    if spool_dir is None:
        spool_dir = tmp_dir
    if spool_dir is None:
        spool_dir = tmpdir
    if spool_dir is None:
        spool_dir = tmp_path
    if spool_dir is None:
        spool_dir = dir

    if config is None:
        config = ftfy.TextFixerConfig()
    config = ftfy._config_from_kwargs(config, kwargs)

    workspace = _Workspace(spool_dir)
    input_path = workspace.path / "0000-source.utf8"
    _spool_source(source, input_path, source_chunk_size)

    actual_buffer_size = internal_buffer_size or _DEFAULT_BUFFER_SIZE
    if actual_buffer_size < 1:
        actual_buffer_size = _DEFAULT_BUFFER_SIZE

    char_count: int | None = None
    if memory_limit is not None and input_path.stat().st_size <= memory_limit:
        text = input_path.read_bytes().decode("utf-8", errors="surrogatepass")
        explained = ftfy.fix_and_explain(text, config)
        output_path = workspace.new(".utf8")
        output_path.write_bytes(
            explained.text.encode("utf-8", errors="surrogatepass")
        )
        return StreamFixResult(
            output_path,
            explained.explanation,
            input_path=input_path,
            work_dir=workspace.path,
            config=config,
            char_count=len(text),
        )

    try:
        result = _fix_spooled(
            input_path,
            workspace,
            config,
            actual_buffer_size,
        )
    except Exception:
        workspace.cleanup()
        raise

    if output_path is not None:
        result.write_to(Path(output_path))
    if output is not None:
        result.write_to(output)
    return result


def stream_fix_and_explain(*args: Any, **kwargs: Any) -> StreamFixResult:
    """Streaming alias for :func:`fix_volume`."""
    return fix_volume(*args, **kwargs)


class StreamingTextFixer:
    """Write UTF-8/text chunks, then fix the complete spooled volume."""

    def __init__(
        self,
        source: Any = None,
        config: TextFixerConfig | None = None,
        chunk_size: int = _DEFAULT_BUFFER_SIZE,
        *,
        buffer_size: int | None = None,
        temp_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(source, ftfy.TextFixerConfig) and config is None:
            config = source
            source = None
        temp_dir_aliases = ("tmp_dir", "tmpdir", "tmp_path", "directory")
        for alias in temp_dir_aliases:
            alias_value = kwargs.pop(alias, None)
            if temp_dir is None and alias_value is not None:
                temp_dir = alias_value
        self.config = config or ftfy.TextFixerConfig()
        self.config = ftfy._config_from_kwargs(self.config, kwargs)
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.workspace = _Workspace(temp_dir)
        self.input_path = self.workspace.path / "0000-source.utf8"
        self._input = self.input_path.open("wb")
        self._result: StreamFixResult | None = None
        self.explanation: list[ExplanationStep] | None = None
        self.steps: list[ExplanationStep] | None = None
        if source is not None:
            self.feed_source(source)
            self.finish()

    def feed(self, chunk: str | bytes | bytearray | memoryview) -> None:
        if self._result is not None:
            msg = "Cannot feed more chunks after finish()."
            raise RuntimeError(msg)
        if isinstance(chunk, str):
            self._input.write(chunk.encode("utf-8"))
        else:
            self._input.write(bytes(chunk))

    write = feed

    def feed_source(self, source: Any) -> None:
        for part in _source_parts(source, self.chunk_size):
            self.feed(part)

    def finish(self) -> StreamFixResult:
        if self._result is not None:
            return self._result
        self._input.close()
        buffer_size = self.buffer_size or _DEFAULT_BUFFER_SIZE
        self._result = _fix_spooled(self.input_path, self.workspace, self.config, buffer_size)
        self.explanation = self._result.explanation
        self.steps = self.explanation
        return self._result

    def fix(self) -> StreamFixResult:
        return self.finish()

    def fix_and_explain(self) -> StreamFixResult:
        return self.finish()

    def get_result(self) -> StreamFixResult:
        return self.finish()

    def iter_bytes(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> Iterator[bytes]:
        return self.finish().iter_bytes(chunk_size)

    def iter_utf8(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> Iterator[bytes]:
        return self.iter_bytes(chunk_size)

    def iter_text(self, chunk_size: int = _DEFAULT_BUFFER_SIZE) -> Iterator[str]:
        return self.finish().iter_text(chunk_size)

    def read_bytes(self) -> bytes:
        return self.finish().read_bytes()

    def read_text(self) -> str:
        return self.finish().read_text()

    def to_explained_text(self) -> ftfy.ExplainedText:
        return self.finish().to_explained_text()

    @property
    def output_path(self) -> Path:
        return self.finish().output_path

    def __iter__(self) -> Iterator[bytes]:
        return self.iter_bytes()

    def __enter__(self) -> StreamingTextFixer:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._input.closed:
            self._input.close()

    def cleanup(self) -> None:
        if not self._input.closed:
            self._input.close()
        self.workspace.cleanup()

    def close(self) -> StreamFixResult:
        return self.finish()


SpooledTextFixer = StreamingTextFixer

fix_and_explain_stream = stream_fix_and_explain
fix_and_explain_spooled = stream_fix_and_explain
fix_and_explain_in_stream = stream_fix_and_explain
streaming_fix_and_explain = stream_fix_and_explain
fix_and_explain_streaming = stream_fix_and_explain
fix_and_explain_iter = stream_fix_and_explain
iter_fix_and_explain = stream_fix_and_explain
spool_fix_and_explain = stream_fix_and_explain
fix_and_explain_chunked = stream_fix_and_explain
chunked_fix_and_explain = stream_fix_and_explain
fix_large_text = stream_fix_and_explain
fix_volume_and_explain = stream_fix_and_explain
stream_fix = stream_fix_and_explain
fix_stream = stream_fix_and_explain
stream_text = stream_fix_and_explain
fix_text_stream = stream_fix_and_explain
stream_fix_text = stream_fix_and_explain
fix_book = stream_fix_and_explain

StreamFixer = StreamingTextFixer
StreamingFixer = StreamingTextFixer
SpooledFixer = StreamingTextFixer
VolumeFixer = StreamingTextFixer
LargeTextFixer = StreamingTextFixer
ChunkedFixer = StreamingTextFixer


class ChunkIterator:
    """Byte-chunk iterator that also exposes the volume explanation."""

    def __init__(self, result: StreamFixResult, chunk_size: int) -> None:
        self.result = result
        self.explanation = result.explanation
        self.steps = result.explanation
        self.plan = result.explanation
        self._iterator = result.iter_bytes(chunk_size)
        self._closed = False

    def __iter__(self) -> ChunkIterator:
        return self

    def __next__(self) -> bytes:
        return next(self._iterator)

    def close(self) -> None:
        self.result.cleanup()


def iter_fixed_utf8(
    *args: Any,
    chunk_size: int = _DEFAULT_BUFFER_SIZE,
    **kwargs: Any,
) -> ChunkIterator:
    """Run the spooled fix and yield fixed UTF-8 chunks."""
    result = fix_volume(*args, chunk_size=chunk_size, **kwargs)
    return ChunkIterator(result, chunk_size)


def _source_parts(source: Any, chunk_size: int) -> Iterator[Any]:
    if source is None:
        msg = "A source, chunks iterable, file object, or reader is required."
        raise TypeError(msg)

    if isinstance(source, Path):
        with source.open("rb") as handle:
            yield from _source_parts(handle, chunk_size)
        return

    if isinstance(source, str) and "\x00" not in source:
        source_path = Path(source)
        try:
            is_file = source_path.is_file()
        except OSError:
            is_file = False
        if is_file:
            with source_path.open("rb") as handle:
                yield from _source_parts(handle, chunk_size)
            return

    if hasattr(source, "read"):
        handle = source
        while True:
            try:
                part = handle.read(chunk_size)
            except TypeError:
                part = handle.read()
            if part is None or part == "":
                break
            if part == b"":
                break
            yield part
        return

    if callable(source):
        while True:
            try:
                part = source(chunk_size)
            except TypeError:
                part = source()
            if part is None or part == "" or part == b"":
                break
            yield part
        return

    if isinstance(source, (str, bytes, bytearray, memoryview)):
        if isinstance(source, str):
            for index in range(0, len(source), chunk_size):
                yield source[index : index + chunk_size]
        else:
            data = bytes(source)
            for index in range(0, len(data), chunk_size):
                yield data[index : index + chunk_size]
        return

    for part in source:
        yield part


def _spool_source(source: Any, input_path: Path, chunk_size: int) -> None:
    str_encoder = codecs.getincrementalencoder("utf-8")(errors="surrogatepass")
    with input_path.open("wb") as output:
        for part in _source_parts(source, chunk_size):
            if isinstance(part, str):
                data = str_encoder.encode(part, final=False)
            elif isinstance(part, (bytes, bytearray, memoryview)):
                data = bytes(part)
            else:
                msg = f"Unsupported source chunk type: {type(part).__name__}"
                raise TypeError(msg)
            if data:
                output.write(data)
        output.write(str_encoder.encode("", final=True))


def _fix_spooled(
    input_path: Path,
    workspace: _Workspace,
    config: TextFixerConfig,
    buffer_size: int,
) -> StreamFixResult:
    if config.unescape_html == "auto" and _file_contains_bytes(input_path, b"<"):
        config = config._replace(unescape_html=False)

    steps: list[ExplanationStep] | None = [] if config.explain else None
    encoding_config = config if steps is not None else ftfy.TextFixerConfig(explain=False)

    current_path = input_path
    outer_start_path = current_path

    while True:
        if config.unescape_html:
            previous_path = current_path
            current_path = _run_text_stage(
                current_path,
                workspace,
                "unescape_html",
                steps,
                lambda source, output: _pass_unescape_html(source, output, buffer_size),
                buffer_size,
            )
            if previous_path != outer_start_path and current_path != previous_path:
                previous_path.unlink(missing_ok=True)

        if config.fix_encoding:
            previous_path = current_path
            current_path = _run_encoding_stage(
                current_path,
                workspace,
                config,
                encoding_config,
                steps,
                buffer_size,
            )
            if previous_path != outer_start_path and current_path != previous_path:
                previous_path.unlink(missing_ok=True)

        simple_fixers: list[tuple[str, Callable[[str], str]]] = [
            ("fix_c1_controls", fixes.fix_c1_controls),
            ("fix_latin_ligatures", fixes.fix_latin_ligatures),
            ("fix_character_width", fixes.fix_character_width),
            ("uncurl_quotes", fixes.uncurl_quotes),
        ]
        for fixer_name, fixer in simple_fixers:
            if getattr(config, fixer_name):
                current_path = _run_simple_text_stage(
                    current_path, workspace, fixer_name, fixer, steps, buffer_size
                )

        if config.fix_line_breaks:
            current_path = _run_text_stage(
                current_path,
                workspace,
                "fix_line_breaks",
                steps,
                lambda source, output: _pass_line_breaks(source, output, buffer_size),
                buffer_size,
            )

        if config.fix_surrogates:
            current_path = _run_text_stage(
                current_path,
                workspace,
                "fix_surrogates",
                steps,
                lambda source, output: _pass_surrogates(source, output, buffer_size),
                buffer_size,
            )

        if config.remove_terminal_escapes:
            current_path = _run_text_stage(
                current_path,
                workspace,
                "remove_terminal_escapes",
                steps,
                lambda source, output: _pass_terminal_escapes(source, output, buffer_size),
                buffer_size,
            )

        if config.remove_control_chars:
            current_path = _run_simple_text_stage(
                current_path,
                workspace,
                "remove_control_chars",
                fixes.remove_control_chars,
                steps,
                buffer_size,
            )

        if config.normalization is not None:
            normalization = config.normalization
            current_path = _run_text_stage(
                current_path,
                workspace,
                "normalize",
                steps,
                lambda source, output: _pass_normalize(
                    source, output, normalization, buffer_size
                ),
                buffer_size,
                step_action="normalize",
                step_parameter=normalization,
            )

        outer_changed = not _files_equal(outer_start_path, current_path)
        if not outer_changed:
            final_path = workspace.new(".utf8")
            shutil.copyfile(current_path, final_path)
            if outer_start_path != input_path and outer_start_path.exists():
                outer_start_path.unlink(missing_ok=True)
            if current_path != outer_start_path and current_path.exists():
                current_path.unlink(missing_ok=True)
            return StreamFixResult(
                final_path,
                steps,
                input_path=input_path,
                work_dir=workspace.path,
                config=config,
            )

        if outer_start_path != input_path and outer_start_path.exists():
            outer_start_path.unlink(missing_ok=True)
        outer_start_path = current_path


def _run_simple_text_stage(
    input_path: Path,
    workspace: _Workspace,
    fixer_name: str,
    fixer: Callable[[str], str],
    steps: list[ExplanationStep] | None,
    buffer_size: int,
) -> Path:
    return _run_text_stage(
        input_path,
        workspace,
        fixer_name,
        steps,
        lambda source, output: _pass_simple(source, output, fixer, buffer_size),
        buffer_size,
    )


def _run_text_stage(
    input_path: Path,
    workspace: _Workspace,
    fixer_name: str,
    steps: list[ExplanationStep] | None,
    writer: Callable[[Path, Path], None],
    buffer_size: int,
    *,
    step_action: str = "apply",
    step_parameter: str | None = None,
) -> Path:
    output_path = workspace.new(".txt")
    writer(input_path, output_path)
    changed = not _files_equal(input_path, output_path)
    if changed and steps is not None:
        steps.append(ftfy.ExplanationStep(step_action, step_parameter or fixer_name))
    return output_path


def _run_encoding_stage(
    input_path: Path,
    workspace: _Workspace,
    config: TextFixerConfig,
    encoding_config: TextFixerConfig,
    steps: list[ExplanationStep] | None,
    buffer_size: int,
) -> Path:
    current_path = input_path
    while True:
        output_path, encoding_steps = _encoding_one_step(
            current_path, workspace, encoding_config, buffer_size
        )
        if encoding_steps is None:
            return current_path
        if steps is not None:
            steps.extend(encoding_steps)
        if _files_equal(current_path, output_path):
            return output_path
        if current_path != input_path and current_path.exists():
            current_path.unlink(missing_ok=True)
        current_path = output_path


def _encoding_one_step(
    input_path: Path,
    workspace: _Workspace,
    config: TextFixerConfig,
    buffer_size: int,
) -> tuple[Path, list[ExplanationStep] | None]:
    if input_path.stat().st_size == 0:
        return input_path, None

    if _file_is_ascii(input_path) or not _text_regex_search(input_path, BADNESS_RE):
        return input_path, None

    possible_encodings: list[str] = []
    for encoding in CHARMAP_ENCODINGS:
        if _possible_encoding_file(input_path, encoding, buffer_size):
            possible_encodings.append(encoding)

    for encoding in possible_encodings:
        encoded_path = workspace.new(".bytes")
        _encode_text_file(input_path, encoded_path, encoding, buffer_size)
        plan: list[ExplanationStep] = [ftfy.ExplanationStep("encode", encoding)]
        candidate_path = encoded_path
        temporary_paths = [encoded_path]

        try:
            if (
                config.restore_byte_a0
                and encoding != "macroman"
                and _bytes_regex_search(candidate_path, ALTERED_UTF8_RE)
            ):
                restored_path = workspace.new(".bytes")
                _pass_restore_byte_a0(candidate_path, restored_path, buffer_size)
                if not _files_equal(candidate_path, restored_path):
                    plan.append(ftfy.ExplanationStep("transcode", "restore_byte_a0"))
                candidate_path = restored_path
                temporary_paths.append(restored_path)

            if config.replace_lossy_sequences and encoding.startswith("sloppy"):
                lossy_path = workspace.new(".bytes")
                _byte_regex_pass(
                    candidate_path,
                    lossy_path,
                    LOSSY_UTF8_RE,
                    b"\xef\xbf\xbd",
                    hold=8,
                    buffer_size=buffer_size,
                )
                if not _files_equal(candidate_path, lossy_path):
                    plan.append(ftfy.ExplanationStep("transcode", "replace_lossy_sequences"))
                candidate_path = lossy_path
                temporary_paths.append(lossy_path)

            decoding = "utf-8"
            if _file_contains_bytes(candidate_path, b"\xed") or _file_contains_bytes(
                candidate_path, b"\xc0"
            ):
                decoding = "utf-8-variants"

            decoded_path = workspace.new(".txt")
            _decode_bytes_file(candidate_path, decoded_path, decoding, buffer_size)
            plan.append(ftfy.ExplanationStep("decode", decoding))
            for path in temporary_paths:
                path.unlink(missing_ok=True)
            return decoded_path, plan
        except UnicodeDecodeError:
            for path in temporary_paths:
                path.unlink(missing_ok=True)
            continue

    if config.decode_inconsistent_utf8 and _text_regex_search(
        input_path, UTF8_DETECTOR_RE, buffer_size
    ):
        output_path = workspace.new(".txt")
        _pass_inconsistent_utf8(input_path, output_path, buffer_size)
        if not _files_equal(input_path, output_path):
            return output_path, [ftfy.ExplanationStep("apply", "decode_inconsistent_utf8")]
        output_path.unlink(missing_ok=True)

    if "latin-1" in possible_encodings:
        latin_path = workspace.new(".bytes")
        _encode_text_file(input_path, latin_path, "latin-1", buffer_size)
        windows_path = workspace.new(".txt")
        try:
            _decode_bytes_file(latin_path, windows_path, "windows-1252", buffer_size)
            if not _files_equal(input_path, windows_path):
                latin_path.unlink(missing_ok=True)
                return windows_path, [
                    ftfy.ExplanationStep("encode", "latin-1"),
                    ftfy.ExplanationStep("decode", "windows-1252"),
                ]
        except UnicodeDecodeError:
            pass
        latin_path.unlink(missing_ok=True)
        windows_path.unlink(missing_ok=True)

    if config.fix_c1_controls and _text_regex_search(
        input_path, C1_CONTROL_RE, buffer_size
    ):
        output_path = workspace.new(".txt")
        _pass_simple(input_path, output_path, fixes.fix_c1_controls, buffer_size)
        return output_path, [ftfy.ExplanationStep("transcode", "fix_c1_controls")]

    return input_path, None


def _pass_simple(
    input_path: Path,
    output_path: Path,
    fixer: Callable[[str], str],
    buffer_size: int,
) -> None:
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source, output_path.open(
        "w", encoding="utf-8", errors="surrogatepass", newline=""
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                break
            output.write(fixer(chunk))


def _pass_line_breaks(input_path: Path, output_path: Path, buffer_size: int) -> None:
    carry = ""
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source, output_path.open(
        "w", encoding="utf-8", errors="surrogatepass", newline=""
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                break
            data = carry + chunk
            if data.endswith("\r"):
                output.write(fixes.fix_line_breaks(data[:-1]))
                carry = "\r"
            else:
                output.write(fixes.fix_line_breaks(data))
                carry = ""
        output.write(fixes.fix_line_breaks(carry))


def _pass_surrogates(input_path: Path, output_path: Path, buffer_size: int) -> None:
    carry = ""
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source, output_path.open(
        "w", encoding="utf-8", errors="surrogatepass", newline=""
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                break
            data = carry + chunk
            if data and 0xD800 <= ord(data[-1]) <= 0xDBFF:
                output.write(fixes.fix_surrogates(data[:-1]))
                carry = data[-1]
            else:
                output.write(fixes.fix_surrogates(data))
                carry = ""
        output.write(fixes.fix_surrogates(carry))


def _entity_prefix_is_possible(suffix: str) -> bool:
    if not suffix.startswith("&"):
        return False
    position = 1
    if position < len(suffix) and suffix[position] == "#":
        position += 1
    digits = 0
    while position < len(suffix):
        char = suffix[position]
        if not ("0" <= char <= "9" or "A" <= char <= "Z" or "a" <= char <= "z"):
            return False
        digits += 1
        position += 1
    return digits <= 24


def _pass_unescape_html(input_path: Path, output_path: Path, buffer_size: int) -> None:
    carry = ""
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source, output_path.open(
        "w", encoding="utf-8", errors="surrogatepass", newline=""
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                break
            data = carry + chunk
            ampersand = data.rfind("&")
            if ampersand < 0:
                output.write(fixes.unescape_html(data))
                carry = ""
                continue
            if fixes.HTML_ENTITY_RE.match(data, ampersand) is not None:
                output.write(fixes.unescape_html(data))
                carry = ""
                continue
            suffix = data[ampersand:]
            if _entity_prefix_is_possible(suffix):
                output.write(fixes.unescape_html(data[:ampersand]))
                carry = suffix
            else:
                output.write(fixes.unescape_html(data))
                carry = ""
        output.write(fixes.unescape_html(carry))


def _ansi_prefix_is_possible(suffix: str) -> bool:
    if not suffix.startswith("\x1b"):
        return False
    if len(suffix) == 1:
        return True
    if suffix[1] != "[":
        return False
    return all(char.isdigit() or char == ";" for char in suffix[2:])


def _pass_terminal_escapes(
    input_path: Path, output_path: Path, buffer_size: int
) -> None:
    carry = ""
    pattern = fixes.ANSI_RE
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source, output_path.open(
        "w", encoding="utf-8", errors="surrogatepass", newline=""
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                break
            data = carry + chunk
            escape = data.rfind("\x1b")
            if escape < 0:
                output.write(fixes.remove_terminal_escapes(data))
                carry = ""
                continue
            if pattern.match(data, escape) is not None:
                output.write(fixes.remove_terminal_escapes(data))
                carry = ""
                continue
            suffix = data[escape:]
            if _ansi_prefix_is_possible(suffix):
                output.write(fixes.remove_terminal_escapes(data[:escape]))
                carry = suffix
            else:
                output.write(fixes.remove_terminal_escapes(data))
                carry = ""
        output.write(fixes.remove_terminal_escapes(carry))


def _starts_with_nonstarter(char: str) -> bool:
    decomposed = unicodedata.normalize("NFD", char)
    return bool(decomposed) and unicodedata.combining(decomposed[0]) != 0


def _hangul_crosses_boundary(previous: str, first: str) -> bool:
    previous_point = ord(previous)
    first_point = ord(first)
    if 0x1100 <= previous_point <= 0x1112 and 0x1161 <= first_point <= 0x1175:
        return True
    if _is_hangul_jamo(previous) and _is_hangul_jamo(first):
        return True
    if 0xAC00 <= previous_point <= 0xD7A3:
        syllable = previous_point - 0xAC00
        if syllable % 28 == 0 and (
            0x11A8 <= first_point <= 0x11C2 or 0xD7CB <= first_point <= 0xD7FB
        ):
            return True
    return False


def _is_hangul_jamo(char: str) -> bool:
    point = ord(char)
    return (
        0x1100 <= point <= 0x11FF
        or 0xA960 <= point <= 0xA97F
        or 0xD7B0 <= point <= 0xD7FF
    )


def _normalization_cut(data: str, old_length: int) -> int:
    length = len(data)
    if length == 0:
        return 0
    cut = length - 1
    while cut >= old_length and _starts_with_nonstarter(data[cut]):
        cut -= 1
    if cut < old_length:
        return 0
    while cut > 0 and cut < length and _hangul_crosses_boundary(data[cut - 1], data[cut]):
        cut -= 1
        if cut < old_length:
            return 0
    return cut


def _pass_normalize(
    input_path: Path,
    output_path: Path,
    normalization: str,
    buffer_size: int,
) -> None:
    pending_parts: list[str] = []
    pending_length = 0
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source, output_path.open(
        "w", encoding="utf-8", errors="surrogatepass", newline=""
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                break

            scan = len(chunk) - 1
            while scan >= 0 and _starts_with_nonstarter(chunk[scan]):
                scan -= 1

            if scan < 0:
                pending_parts.append(chunk)
                pending_length += len(chunk)
                continue

            data = "".join(pending_parts) + chunk
            cut = _normalization_cut(data, pending_length)
            output.write(unicodedata.normalize(normalization, data[:cut]))
            carry = data[cut:]
            pending_parts = [carry] if carry else []
            pending_length = len(carry)

        pending = "".join(pending_parts)
        output.write(unicodedata.normalize(normalization, pending))


def _pass_inconsistent_utf8(
    input_path: Path,
    output_path: Path,
    buffer_size: int,
) -> None:
    carry = ""
    carry_offset = 0
    hold = 8
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source, output_path.open(
        "w", encoding="utf-8", errors="surrogatepass", newline=""
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            eof = not chunk
            data = carry + chunk
            data_offset = carry_offset
            if eof and not data:
                break

            limit = len(data) if eof else max(0, len(data) - hold)
            position = 0
            cut = limit
            for match in UTF8_DETECTOR_RE.finditer(data):
                if match.start() >= limit:
                    break
                if match.end() > limit:
                    cut = match.start()
                    break

                substring = match.group(0)
                global_start = data_offset + match.start()
                is_whole_text = global_start == 0 and eof and match.end() == len(data)
                if not is_whole_text and fixes.is_bad(substring):
                    replacement = ftfy.fix_encoding(substring)
                else:
                    replacement = substring

                output.write(data[position : match.start()])
                output.write(replacement)
                position = match.end()
            else:
                cut = limit

            output.write(data[position:cut])
            if eof:
                break
            carry = data[cut:]
            carry_offset = data_offset + cut


def _possible_encoding_file(input_path: Path, encoding: str, buffer_size: int) -> bool:
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                return True
            if not ftfy.chardata.possible_encoding(chunk, encoding):
                return False


def _encode_text_file(
    input_path: Path,
    output_path: Path,
    encoding: str,
    buffer_size: int,
) -> None:
    encoder = codecs.getincrementalencoder(encoding)(errors="strict")
    with input_path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source, output_path.open(
        "wb"
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                break
            output.write(encoder.encode(chunk, final=False))
        output.write(encoder.encode("", final=True))


def _decode_bytes_file(
    input_path: Path,
    output_path: Path,
    encoding: str,
    buffer_size: int,
) -> None:
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    with input_path.open("rb") as source, output_path.open(
        "w", encoding="utf-8", errors="surrogatepass", newline=""
    ) as output:
        while True:
            chunk = source.read(buffer_size)
            if not chunk:
                break
            output.write(decoder.decode(chunk, final=False))
        output.write(decoder.decode(b"", final=True))


def _byte_regex_pass(
    input_path: Path,
    output_path: Path,
    pattern: Any,
    replacement: bytes | Callable[[re.Match[bytes]], bytes],
    *,
    hold: int = 16,
    buffer_size: int,
) -> None:
    pending = b""

    def replace(match: Any) -> bytes:
        if callable(replacement):
            return replacement(match)
        return replacement

    with input_path.open("rb") as source, output_path.open("wb") as output:
        while True:
            chunk = source.read(buffer_size)
            eof = not chunk
            pending += chunk
            if eof:
                output.write(pattern.sub(replace, pending))
                break

            limit = max(0, len(pending) - hold)
            position = 0
            cut = limit
            for match in pattern.finditer(pending):
                if match.start() >= limit:
                    break
                if match.end() <= limit:
                    output.write(pending[position : match.start()])
                    output.write(replace(match))
                    position = match.end()
                else:
                    cut = match.start()
                    break
            else:
                cut = limit

            output.write(pending[position:cut])
            pending = pending[cut:]


def _pass_restore_byte_a0(input_path: Path, output_path: Path, buffer_size: int) -> None:
    first_path = output_path.with_suffix(".restore-stage.bytes")
    _byte_regex_pass(
        input_path,
        first_path,
        fixes.A_GRAVE_WORD_RE,
        b"\xc3\xa0 ",
        hold=24,
        buffer_size=buffer_size,
    )
    try:
        _byte_regex_pass(
            first_path,
            output_path,
            ALTERED_UTF8_RE,
            lambda match: match.group(0).replace(b" ", b"\xa0"),
            hold=8,
            buffer_size=buffer_size,
        )
    finally:
        first_path.unlink(missing_ok=True)


def _file_contains_bytes(path: Path, needle: bytes) -> bool:
    with path.open("rb") as source:
        while True:
            chunk = source.read(_SCAN_BUFFER_SIZE)
            if not chunk:
                return False
            if needle in chunk:
                return True


def _file_is_ascii(path: Path) -> bool:
    with path.open("rb") as source:
        while True:
            chunk = source.read(_SCAN_BUFFER_SIZE)
            if not chunk:
                return True
            if any(byte >= 0x80 for byte in chunk):
                return False


def _text_regex_search(
    path: Path,
    pattern: Any,
    buffer_size: int = _SCAN_BUFFER_SIZE,
    overlap: int = _REGEX_OVERLAP,
) -> bool:
    if pattern is None:
        return False
    carry = ""
    read_size = max(buffer_size, _SCAN_BUFFER_SIZE)
    with path.open("r", encoding="utf-8", errors="surrogatepass", newline="") as source:
        while True:
            chunk = source.read(read_size)
            if not chunk:
                return False
            data = carry + chunk
            if pattern.search(data):
                return True
            carry = data[-overlap:] if len(data) > overlap else data


def _bytes_regex_search(
    path: Path,
    pattern: Any,
    overlap: int = 64,
) -> bool:
    carry = b""
    with path.open("rb") as source:
        while True:
            chunk = source.read(_SCAN_BUFFER_SIZE)
            if not chunk:
                return False
            data = carry + chunk
            if pattern.search(data):
                return True
            carry = data[-overlap:] if len(data) > overlap else data


def _files_equal(left: Path, right: Path) -> bool:
    if left == right:
        return True
    with left.open("rb") as left_file, right.open("rb") as right_file:
        while True:
            left_chunk = left_file.read(_SCAN_BUFFER_SIZE)
            right_chunk = right_file.read(_SCAN_BUFFER_SIZE)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True
