"""
Versioned index of the links, images and link reference definitions in a
Markdown document.

The index is built on mistletoe's normal parser.  It records, for every
version of a document:

* every link and image in body text, with its source line and column,
* the normalized label of the material it cites (for reference links),
* every link reference definition in document order,
* which definition is actually in effect for a label.

A definition is *effective* when it is the first definition of its normalized
label in the document: later definitions with the same label are shadowed,
which matches how mistletoe resolves references.

Versions are immutable.  ``ReferenceIndex.apply_edits`` takes one batch of
edits expressed against the previous version and publishes exactly one new
version after all edits have been applied.  Unchanged blocks keep their
previously parsed inline tokens.  ``ReferenceIndex.verify`` re-parses a
version from scratch and checks the incremental result against it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, Sequence, Union

from mistletoe import block_token, core_tokens, token
from mistletoe import span_token
from mistletoe.core_tokens import normalize_label


__all__ = [
    'TextEdit',
    'LineColumnEdit',
    'Position',
    'SourceRange',
    'DefinitionRecord',
    'ReferenceRecord',
    'ResourceRecord',
    'DefinitionChange',
    'ChangeSet',
    'ParseStats',
    'VersionIndex',
    'VerificationResult',
    'ReferenceIndex',
    'MaterialIndex',
    'DocumentIndex',
]


@dataclass(frozen=True)
class Position:
    """A 1-based line and 1-based column in the source document."""

    line: int
    column: int

    def to_dict(self) -> dict[str, int]:
        return {'line': self.line, 'column': self.column}


@dataclass(frozen=True)
class SourceRange:
    """A source range from ``start`` (inclusive) to ``end`` (exclusive)."""

    start: Position
    end: Position

    @property
    def line(self) -> int:
        return self.start.line

    @property
    def column(self) -> int:
        return self.start.column

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {'start': self.start.to_dict(), 'end': self.end.to_dict()}


@dataclass(frozen=True)
class TextEdit:
    """
    Replace ``text[start:end]`` in the previous version.

    Offsets are Python string offsets: ``start`` is 0-based and ``end`` is
    exclusive.  A batch of edits is applied simultaneously, so all offsets
    refer to the same previous version.
    """

    start: int
    end: int
    text: str = ''

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError('invalid edit range: {!r}'.format((self.start, self.end)))

    @classmethod
    def insert(cls, offset: int, text: str) -> 'TextEdit':
        return cls(offset, offset, text)

    @classmethod
    def delete(cls, start: int, end: int) -> 'TextEdit':
        return cls(start, end, '')

    @classmethod
    def from_line_column(
        cls,
        text: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
        replacement: str = '',
    ) -> 'TextEdit':
        start = line_column_to_offset(text, start_line, start_column)
        end = line_column_to_offset(text, end_line, end_column)
        return cls(start, end, replacement)


@dataclass(frozen=True)
class LineColumnEdit:
    """
    An edit expressed in 1-based line/column coordinates.

    ``ReferenceIndex.apply_edits`` accepts these, ``TextEdit`` instances, and
    equivalent ``(start, end, replacement)`` tuples or mappings.
    """

    start_line: int
    start_column: int
    end_line: int
    end_column: int
    text: str = ''

    def resolve(self, document: str) -> TextEdit:
        return TextEdit.from_line_column(
            document,
            self.start_line,
            self.start_column,
            self.end_line,
            self.end_column,
            self.text,
        )


@dataclass(frozen=True)
class DefinitionRecord:
    """
    A link reference definition in one version.

    ``label`` is normalized.  ``raw_label`` preserves the source spelling.
    ``effective`` is true only for the first definition of its label; the
    other definitions are shadowed.
    """

    index: int
    label: str
    raw_label: str
    destination: str
    title: str
    range: SourceRange
    dest_type: str = 'uri'
    title_delimiter: Optional[str] = None
    effective: bool = True
    occurrence: int = 0
    shadowed_by: Optional[int] = None
    identity: int = 0

    def signature(self) -> tuple:
        return (
            self.label,
            self.raw_label,
            self.destination,
            self.title,
            self.dest_type,
            self.title_delimiter,
        )

    @property
    def line(self) -> int:
        return self.range.start.line

    @property
    def column(self) -> int:
        return self.range.start.column

    @property
    def end_line(self) -> int:
        return self.range.end.line

    @property
    def end_column(self) -> int:
        return self.range.end.column

    def to_dict(self) -> dict:
        return {
            'index': self.index,
            'label': self.label,
            'raw_label': self.raw_label,
            'destination': self.destination,
            'title': self.title,
            'range': self.range.to_dict(),
            'line': self.line,
            'column': self.column,
            'dest_type': self.dest_type,
            'effective': self.effective,
            'occurrence': self.occurrence,
            'shadowed_by': self.shadowed_by,
            'identity': self.identity,
        }


@dataclass(frozen=True)
class ReferenceRecord:
    """
    A link or image in body text.

    ``reference_kind`` is one of ``inline``, ``full``, ``collapsed``,
    ``shortcut`` or ``autolink``.  For reference links ``label`` is the
    normalized label looked up in the definition table, and ``definition``
    points at the effective definition.  Inline links and autolinks have no
    label and therefore no definition.
    """

    index: int
    kind: str
    reference_kind: str
    destination: str
    title: str
    range: SourceRange
    text: str = ''
    label: Optional[str] = None
    raw_label: Optional[str] = None
    definition: Optional[DefinitionRecord] = None
    block_line: Optional[int] = None
    block_end_line: Optional[int] = None

    @property
    def line(self) -> int:
        return self.range.start.line

    @property
    def column(self) -> int:
        return self.range.start.column

    @property
    def end_line(self) -> int:
        return self.range.end.line

    @property
    def end_column(self) -> int:
        return self.range.end.column

    @property
    def resource_key(self) -> tuple[str, str]:
        if self.label is not None:
            return ('label', self.label)
        return ('url', self.destination)

    def signature(self) -> tuple:
        return (
            self.kind,
            self.reference_kind,
            self.label,
            self.destination,
            self.title,
        )

    def to_dict(self) -> dict:
        return {
            'index': self.index,
            'kind': self.kind,
            'reference_kind': self.reference_kind,
            'destination': self.destination,
            'title': self.title,
            'text': self.text,
            'label': self.label,
            'raw_label': self.raw_label,
            'definition_index': None if self.definition is None else self.definition.index,
            'range': self.range.to_dict(),
            'line': self.line,
            'column': self.column,
            'block_line': self.block_line,
            'block_end_line': self.block_end_line,
        }


@dataclass(frozen=True)
class ResourceRecord:
    """An effective material definition together with the citations using it."""

    label: str
    definition: DefinitionRecord
    references: tuple[ReferenceRecord, ...] = ()

    @property
    def destination(self) -> str:
        return self.definition.destination

    @property
    def title(self) -> str:
        return self.definition.title

    def to_dict(self) -> dict:
        return {
            'label': self.label,
            'definition': self.definition.to_dict(),
            'references': [reference.to_dict() for reference in self.references],
        }


@dataclass(frozen=True)
class DefinitionChange:
    """A change in which definition is effective for a label."""

    label: str
    before: Optional[DefinitionRecord]
    after: Optional[DefinitionRecord]

    @property
    def destination_changed(self) -> bool:
        before = None if self.before is None else (self.before.destination, self.before.title)
        after = None if self.after is None else (self.after.destination, self.after.title)
        return before != after

    @property
    def effective_line_changed(self) -> bool:
        if self.before is None or self.after is None:
            return True
        return self.before.identity != self.after.identity

    def to_dict(self) -> dict:
        return {
            'label': self.label,
            'before': None if self.before is None else self.before.to_dict(),
            'after': None if self.after is None else self.after.to_dict(),
            'destination_changed': self.destination_changed,
            'effective_line_changed': self.effective_line_changed,
        }


@dataclass(frozen=True)
class ChangeSet:
    """The result of comparing two consecutive versions."""

    before_version: int
    after_version: int
    added_references: tuple[ReferenceRecord, ...] = ()
    removed_references: tuple[ReferenceRecord, ...] = ()
    changed_definitions: tuple[DefinitionChange, ...] = ()
    resources_to_recheck: tuple[str, ...] = ()
    url_resources_to_recheck: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.added_references
            or self.removed_references
            or self.changed_definitions
            or self.resources_to_recheck
            or self.url_resources_to_recheck
        )

    @property
    def added_links(self) -> tuple[ReferenceRecord, ...]:
        return tuple(item for item in self.added_references if item.kind == 'link')

    @property
    def removed_links(self) -> tuple[ReferenceRecord, ...]:
        return tuple(item for item in self.removed_references if item.kind == 'link')

    @property
    def added_images(self) -> tuple[ReferenceRecord, ...]:
        return tuple(item for item in self.added_references if item.kind == 'image')

    @property
    def removed_images(self) -> tuple[ReferenceRecord, ...]:
        return tuple(item for item in self.removed_references if item.kind == 'image')

    def to_dict(self) -> dict:
        return {
            'before_version': self.before_version,
            'after_version': self.after_version,
            'added_references': [item.to_dict() for item in self.added_references],
            'removed_references': [item.to_dict() for item in self.removed_references],
            'changed_definitions': [item.to_dict() for item in self.changed_definitions],
            'resources_to_recheck': list(self.resources_to_recheck),
            'url_resources_to_recheck': list(self.url_resources_to_recheck),
        }


@dataclass
class ParseStats:
    """Counters describing how much work one version required."""

    inline_parsed_blocks: int = 0
    reused_blocks: int = 0
    total_blocks: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            'inline_parsed_blocks': self.inline_parsed_blocks,
            'reused_blocks': self.reused_blocks,
            'total_blocks': self.total_blocks,
        }


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of comparing an indexed version with a full re-parse."""

    version: int
    ok: bool
    errors: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict:
        return {'version': self.version, 'ok': self.ok, 'errors': list(self.errors)}


def line_column_to_offset(document: str, line: int, column: int) -> int:
    """
    Convert a 1-based line and 1-based column to a string offset.

    A column one past the end of a line maps to that line's newline, which is
    convenient for expressing "insert at end of line" edits.
    """
    if line < 1 or column < 1:
        raise ValueError('line and column must be 1-based: {!r}'.format((line, column)))
    offset = 0
    current = 1
    while current < line:
        newline = document.find('\n', offset)
        if newline < 0:
            raise ValueError('line {} is past the end of the document'.format(line))
        offset = newline + 1
        current += 1
    line_end = document.find('\n', offset)
    if line_end < 0:
        line_end = len(document)
    if column - 1 > line_end - offset:
        raise ValueError(
            'column {} is past the end of line {}'.format(column, line)
        )
    return offset + column - 1


def offset_to_line_column(document: str, offset: int) -> Position:
    """Convert a string offset to a 1-based line and 1-based column."""
    if offset < 0 or offset > len(document):
        raise ValueError('offset out of range: {!r}'.format(offset))
    line = document.count('\n', 0, offset) + 1
    line_start = document.rfind('\n', 0, offset) + 1
    return Position(line, offset - line_start + 1)


class VersionIndex:
    """
    Immutable index for one version of a document.

    All positions are the positions in this version, even when the version is
    consulted long after later saves.  Use :meth:`references_for`,
    :meth:`definition_for` and :attr:`resources` to look up material data.
    """

    def __init__(
        self,
        version: int,
        text: str,
        definitions: Sequence[DefinitionRecord],
        references: Sequence[ReferenceRecord],
        footnotes: Mapping[str, tuple[str, str]],
        stats: Optional[ParseStats] = None,
        _entries: Optional[Sequence['_BlockEntry']] = None,
    ) -> None:
        self.version = version
        self.text = text
        self.definitions = tuple(definitions)
        self.references = tuple(references)
        self.footnotes = dict(footnotes)
        self.stats = stats or ParseStats()
        self._entries = tuple(_entries or ())

        effective: dict[str, DefinitionRecord] = {}
        for definition in self.definitions:
            if definition.effective:
                effective[definition.label] = definition
        self._effective_definitions = effective

        grouped: dict[str, list[ReferenceRecord]] = {}
        for reference in self.references:
            if reference.label is not None:
                grouped.setdefault(reference.label, []).append(reference)
        self._references_by_label = {key: tuple(value) for key, value in grouped.items()}
        self._resources = {
            label: ResourceRecord(label, definition, self._references_by_label.get(label, ()))
            for label, definition in effective.items()
        }

    @property
    def effective_definitions(self) -> Mapping[str, DefinitionRecord]:
        """Effective definition per normalized label."""
        return dict(self._effective_definitions)

    @property
    def resources(self) -> Mapping[str, ResourceRecord]:
        """Effective material definitions keyed by normalized label."""
        return dict(self._resources)

    @property
    def links(self) -> tuple[ReferenceRecord, ...]:
        return tuple(item for item in self.references if item.kind == 'link')

    @property
    def images(self) -> tuple[ReferenceRecord, ...]:
        return tuple(item for item in self.references if item.kind == 'image')

    def cite(self, label: str) -> Optional[DefinitionRecord]:
        """Alias for :meth:`definition_for`."""
        return self.definition_for(label)

    def resolve(self, label: str) -> Optional[DefinitionRecord]:
        """Alias for :meth:`definition_for`."""
        return self.definition_for(label)

    def references_to(self, label: str) -> tuple[ReferenceRecord, ...]:
        """Alias for :meth:`references_for`."""
        return self.references_for(label)

    def definition_for(self, label: str) -> Optional[DefinitionRecord]:
        """Return the definition actually in effect for ``label``."""
        return self._effective_definitions.get(normalize_label(label))

    def references_for(self, label: str) -> tuple[ReferenceRecord, ...]:
        """Return body citations of ``label`` (normalized internally)."""
        return self._references_by_label.get(normalize_label(label), ())

    def definitions_for(self, label: str) -> tuple[DefinitionRecord, ...]:
        """Return every definition of ``label``, effective or shadowed."""
        key = normalize_label(label)
        return tuple(item for item in self.definitions if item.label == key)

    def reference_at(self, line: int, column: int) -> Optional[ReferenceRecord]:
        """Return the link/image starting at a 1-based line/column, if any."""
        for reference in self.references:
            start = reference.range.start
            if start.line == line and start.column == column:
                return reference
        return None

    def definition_at(self, line: int, column: int = 1) -> Optional[DefinitionRecord]:
        """Return the definition starting at a 1-based line/column, if any."""
        for definition in self.definitions:
            start = definition.range.start
            if start.line == line and start.column == column:
                return definition
        return None

    def resource_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(item.resource_key for item in self.references)

    def to_dict(self) -> dict:
        return {
            'version': self.version,
            'definitions': [item.to_dict() for item in self.definitions],
            'references': [item.to_dict() for item in self.references],
            'effective_definitions': {
                label: item.to_dict()
                for label, item in self._effective_definitions.items()
            },
            'resources': {
                label: item.to_dict() for label, item in self._resources.items()
            },
            'stats': self.stats.to_dict(),
        }

    def changes_from(self, previous: 'VersionIndex') -> ChangeSet:
        """Compare this version with ``previous`` ignoring pure line shifts."""
        return _compare_versions(previous, self)


class ReferenceIndex:
    """
    Versioned index for a Markdown document.

    Typical use::

        index = ReferenceIndex(markdown_text)
        index.apply_edits([(10, 20, 'replacement')])
        index.changes.resources_to_recheck

    Edits are always expressed against the version that was current when
    ``apply_edits`` is called.  A failed batch leaves the index untouched.

    Each call to :meth:`apply_edits` applies the whole batch to the previous
    version and publishes exactly one new :class:`VersionIndex`.  There is no
    observable intermediate state.  Previously published versions are never
    modified; they keep their own text and their own line/column positions.
    """

    def __init__(self, text: Union[str, Path]) -> None:
        if isinstance(text, Path):
            text = text.read_text()
        self._text = str(text)
        self._versions: list[VersionIndex] = []
        self._changes: list[ChangeSet] = []
        first = _build_version(self._text, version=0, previous=None)
        self._versions.append(first)

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> 'ReferenceIndex':
        return cls(Path(path))

    @property
    def index(self) -> VersionIndex:
        """The current version index."""
        return self._versions[-1]

    @property
    def current(self) -> VersionIndex:
        """The current version index."""
        return self._versions[-1]

    @property
    def text(self) -> str:
        """The current version's source text."""
        return self._text

    @property
    def versions(self) -> tuple[VersionIndex, ...]:
        """Every published version, oldest first."""
        return tuple(self._versions)

    def version(self, number: int) -> VersionIndex:
        """Return a previously confirmed version, with its own positions."""
        if number < 0:
            number += len(self._versions)
        return self._versions[number]

    @property
    def changes(self) -> Optional[ChangeSet]:
        """Changes introduced by the most recent save, if any."""
        return self._changes[-1] if self._changes else None

    def changes_between(self, before: int, after: int) -> ChangeSet:
        """Compare any two published versions."""
        return _compare_versions(self.version(before), self.version(after))

    def apply_edits(
        self,
        edits: Iterable[Union[TextEdit, LineColumnEdit, Sequence, Mapping]],
    ) -> VersionIndex:
        """
        Apply one batch of edits and publish one new version.

        ``edits`` may contain :class:`TextEdit`, :class:`LineColumnEdit`,
        ``(start, end, replacement)`` tuples, or mappings with ``start`` and
        ``end`` keys (and optional ``text``).  All edits refer to the current
        version and are applied simultaneously.

        Raises ``ValueError`` for invalid or overlapping ranges without
        changing the index.
        """
        resolved = [_coerce_edit(edit, self._text) for edit in edits]
        new_text, translator = _apply_edits(self._text, resolved)
        previous = self.index
        new_version = _build_version(
            new_text, version=previous.version + 1, previous=previous, translator=translator)
        change = _compare_versions(previous, new_version)
        self._text = new_text
        self._versions.append(new_version)
        self._changes.append(change)
        return new_version

    def update(self, edits: Iterable[Union[TextEdit, LineColumnEdit, Sequence, Mapping]]) -> VersionIndex:
        """Alias for :meth:`apply_edits`."""
        return self.apply_edits(edits)

    def apply(self, edits: Iterable[Union[TextEdit, LineColumnEdit, Sequence, Mapping]]) -> VersionIndex:
        """Alias for :meth:`apply_edits`."""
        return self.apply_edits(edits)

    def save(self, edits: Iterable[Union[TextEdit, LineColumnEdit, Sequence, Mapping]]) -> VersionIndex:
        """Alias for :meth:`apply_edits`."""
        return self.apply_edits(edits)

    def verify(self, version: Optional[Union[int, VersionIndex]] = None) -> VerificationResult:
        """Re-parse a version from scratch and compare it with the index."""
        if version is None:
            target = self.index
        elif isinstance(version, VersionIndex):
            target = version
        else:
            target = self.version(version)
        return _verify_version(target)

    def verify_all(self) -> tuple[VerificationResult, ...]:
        """Verify every version in the history."""
        return tuple(_verify_version(item) for item in self._versions)


MaterialIndex = ReferenceIndex
DocumentIndex = ReferenceIndex


@dataclass
class _BlockEntry:
    key: tuple
    block: block_token.BlockToken
    children: tuple
    inline_matches: tuple
    inline_dependencies: tuple
    inline_map: Optional[tuple[tuple[int, int], ...]]
    footnote_count: int
    inline_leaf: bool
    start: int = 0


def _line_text(document: str, line_number: int) -> str:
    lines = document.splitlines(keepends=True)
    if line_number < 1 or line_number > len(lines):
        return ''
    return lines[line_number - 1].rstrip('\n')


def _leaf_blocks(root: block_token.BlockToken) -> Iterator[block_token.BlockToken]:
    for child in (root.children or []):
        if not isinstance(child, block_token.BlockToken):
            continue
        if isinstance(child, (block_token.Quote, block_token.List,
                              block_token.ListItem, block_token.Document)):
            yield from _leaf_blocks(child)
        elif isinstance(child, block_token.Table):
            yield from _leaf_blocks(child.header)
            for row in child.children:
                yield from _leaf_blocks(row)
        elif isinstance(child, block_token.TableRow):
            for cell in child.children:
                yield cell
        else:
            yield child


def _block_key(block: block_token.BlockToken) -> tuple:
    if hasattr(block, 'inline_content'):
        return (block.__class__.__name__, block.inline_content)
    if isinstance(block, (block_token.BlockCode, block_token.CodeFence)):
        return (block.__class__.__name__, getattr(block, 'language', ''), block.content)
    if isinstance(block, block_token.HtmlBlock):
        return (block.__class__.__name__, block.content)
    return (block.__class__.__name__, getattr(block, 'content', None))


def _build_version(
    text: str,
    version: int,
    previous: Optional[VersionIndex],
    translator: Optional['_OffsetTranslator'] = None,
) -> VersionIndex:
    stats = ParseStats()
    skeleton, definitions = _parse_skeleton(text)
    definitions = _assign_definition_identities(definitions, previous, translator)
    entries = _collect_entries(skeleton, text, previous, stats, translator)
    _attach_reused_entries(entries)
    _parse_missing_inline(entries, skeleton)
    references = _collect_references(entries, definitions)
    stats.total_blocks = len(entries)
    return VersionIndex(
        version,
        text,
        definitions,
        references,
        skeleton.footnotes,
        stats=stats,
        _entries=entries,
    )


def _parse_skeleton(text: str) -> tuple[block_token.Document, tuple[DefinitionRecord, ...]]:
    previous_suppress = span_token._suppress_inline_parsing
    previous_recorder = core_tokens._definition_recorder
    previous_root = token._root_node
    recorded: list[tuple[int, int, int, int, tuple]] = []
    span_token._suppress_inline_parsing = True
    core_tokens._definition_recorder = lambda *args: recorded.append(args)
    try:
        document = block_token.Document(text)
    finally:
        span_token._suppress_inline_parsing = previous_suppress
        core_tokens._definition_recorder = previous_recorder
        token._root_node = previous_root
    return document, _make_definitions(recorded)


def _make_definitions(recorded: Sequence[tuple[int, int, int, int, tuple]]) -> tuple[DefinitionRecord, ...]:
    definitions: list[DefinitionRecord] = []
    effective: dict[str, int] = {}
    occurrences: dict[str, int] = {}
    for index, (start_line, start_column, end_line, end_column, match) in enumerate(recorded):
        raw_label, destination, title, dest_type, title_delimiter = match
        label = normalize_label(raw_label)
        occurrence = occurrences.get(label, 0)
        occurrences[label] = occurrence + 1
        is_effective = label not in effective
        if is_effective:
            effective[label] = index
        definitions.append(DefinitionRecord(
            index=index,
            label=label,
            raw_label=raw_label,
            destination=span_token.EscapeSequence.strip(destination.strip()),
            title=span_token.EscapeSequence.strip(title),
            range=SourceRange(
                Position(start_line, start_column),
                Position(end_line, end_column),
            ),
            dest_type=dest_type,
            title_delimiter=title_delimiter,
            effective=is_effective,
            occurrence=occurrence,
            shadowed_by=None if is_effective else effective[label],
        ))
    return tuple(definitions)


def _assign_definition_identities(
    definitions: Sequence[DefinitionRecord],
    previous: Optional[VersionIndex],
    translator: Optional['_OffsetTranslator'] = None,
) -> tuple[DefinitionRecord, ...]:
    if previous is None or not previous.definitions:
        return tuple(
            _replace_definition(item, identity=item.index) for item in definitions
        )
    old_definitions = list(previous.definitions)
    matched: dict[int, int] = {}
    used: set[int] = set()

    if translator is not None:
        old_map = _LineMap(previous.text)
        new_map = _LineMap(translator.after)
        old_by_offset: dict[int, list[DefinitionRecord]] = {}
        for old in old_definitions:
            old_by_offset.setdefault(
                old_map.offset(old.range.start.line, old.range.start.column),
                [],
            ).append(old)
        for position, item in enumerate(definitions):
            offset = new_map.offset(item.range.start.line, item.range.start.column)
            source = translator.source_of(offset)
            if source is None:
                continue
            for old in old_by_offset.get(source, ()):
                if old.identity in used or old.label != item.label:
                    continue
                matched[position] = old.identity
                used.add(old.identity)
                break
        # A definition edited in place is still the same definition line even
        # though the replaced range no longer maps back to the previous text.
        for position, item in enumerate(definitions):
            if position in matched:
                continue
            if not translator.replaced(item.range.start.line, item.range.start.column):
                continue
            for old in old_definitions:
                if old.identity in used or old.label != item.label:
                    continue
                if old.range.start.line != item.range.start.line:
                    continue
                matched[position] = old.identity
                used.add(old.identity)
                break

    unmatched_new = [position for position in range(len(definitions)) if position not in matched]
    unmatched_old = [item for item in old_definitions if item.identity not in used]
    matcher = difflib.SequenceMatcher(
        None,
        [item.signature() for item in unmatched_old],
        [definitions[position].signature() for position in unmatched_new],
        autojunk=False,
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != 'equal':
            continue
        for offset in range(min(i2 - i1, j2 - j1)):
            matched[unmatched_new[j1 + offset]] = unmatched_old[i1 + offset].identity
            used.add(unmatched_old[i1 + offset].identity)

    next_identity = max((item.identity for item in old_definitions), default=-1) + 1
    result: list[DefinitionRecord] = []
    for position, item in enumerate(definitions):
        identity = matched.get(position)
        if identity is None:
            identity = next_identity
            next_identity += 1
        result.append(_replace_definition(item, identity=identity))
    return tuple(result)


def _replace_definition(definition: DefinitionRecord, **changes) -> DefinitionRecord:
    values = {
        'index': definition.index,
        'label': definition.label,
        'raw_label': definition.raw_label,
        'destination': definition.destination,
        'title': definition.title,
        'range': definition.range,
        'dest_type': definition.dest_type,
        'title_delimiter': definition.title_delimiter,
        'effective': definition.effective,
        'occurrence': definition.occurrence,
        'shadowed_by': definition.shadowed_by,
        'identity': definition.identity,
    }
    values.update(changes)
    return DefinitionRecord(**values)


def _collect_entries(
    document: block_token.Document,
    text: str,
    previous: Optional[VersionIndex],
    stats: ParseStats,
    translator: Optional['_OffsetTranslator'] = None,
) -> list[_BlockEntry]:
    line_offsets = _line_offsets(text)
    entries: list[_BlockEntry] = []
    for block in _leaf_blocks(document):
        inline_leaf = hasattr(block, 'inline_content')
        key = _block_key(block)
        inline_map = _build_inline_map(block, text) if inline_leaf else None
        start = _block_offset(block, line_offsets)
        entries.append(_BlockEntry(
            key=key,
            block=block,
            children=tuple(block.children or ()),
            inline_matches=tuple(getattr(block, 'inline_matches', ())),
            inline_dependencies=tuple(getattr(block, 'inline_dependencies', ())),
            inline_map=inline_map,
            footnote_count=len(document.footnotes),
            inline_leaf=inline_leaf,
            start=start,
        ))
    if previous is None:
        stats.inline_parsed_blocks = sum(1 for item in entries if item.inline_leaf)
        return entries

    old_entries = previous._entries
    if translator is not None:
        matches = _match_by_delta(old_entries, entries, translator, document.footnotes)
    else:
        matches = _match_by_sequence(old_entries, entries, document.footnotes)
    for position, entry in enumerate(entries):
        old = matches.get(position)
        if old is None:
            if entry.inline_leaf:
                stats.inline_parsed_blocks += 1
        else:
            entry.children = old.children
            entry.inline_matches = old.inline_matches
            entry.inline_dependencies = old.inline_dependencies
            entry.inline_map = entry.inline_map or old.inline_map
            stats.reused_blocks += 1
    return entries


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for index, character in enumerate(text):
        if character == '\n':
            offsets.append(index + 1)
    return offsets


def _block_offset(block: block_token.BlockToken, line_offsets: Sequence[int]) -> int:
    line = getattr(block, 'line_number', None) or 1
    return line_offsets[min(max(line - 1, 0), len(line_offsets) - 1)]


def _match_by_delta(
    old_entries: Sequence[_BlockEntry],
    new_entries: Sequence[_BlockEntry],
    translator: '_OffsetTranslator',
    footnotes: Mapping[str, tuple[str, str]],
) -> dict[int, _BlockEntry]:
    old_by_start: dict[int, list[_BlockEntry]] = {}
    for entry in old_entries:
        old_by_start.setdefault(entry.start, []).append(entry)
    used: set[int] = set()
    matches: dict[int, _BlockEntry] = {}
    for position, entry in enumerate(new_entries):
        old_start = translator.source_of(entry.start)
        if old_start is None:
            continue
        for candidate in old_by_start.get(old_start, ()):
            if id(candidate) in used:
                continue
            if candidate.key != entry.key:
                continue
            if candidate.inline_leaf and not _dependencies_valid(candidate, footnotes):
                continue
            matches[position] = candidate
            used.add(id(candidate))
            break
    return matches


def _match_by_sequence(
    old_entries: Sequence[_BlockEntry],
    new_entries: Sequence[_BlockEntry],
    footnotes: Mapping[str, tuple[str, str]],
) -> dict[int, _BlockEntry]:
    matcher = difflib.SequenceMatcher(
        None,
        [item.key for item in old_entries],
        [item.key for item in new_entries],
        autojunk=False,
    )
    matches: dict[int, _BlockEntry] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != 'equal':
            continue
        for offset in range(min(i2 - i1, j2 - j1)):
            old = old_entries[i1 + offset]
            if old.inline_leaf and not _dependencies_valid(old, footnotes):
                continue
            matches[j1 + offset] = old
    return matches


def _dependencies_valid(entry: _BlockEntry, footnotes: Mapping[str, tuple[str, str]]) -> bool:
    if (entry.footnote_count == 0) != (len(footnotes) == 0) and '[' in _entry_text(entry):
        return False
    for label, result in entry.inline_dependencies:
        if footnotes.get(label) != result:
            return False
    return True


def _entry_text(entry: _BlockEntry) -> str:
    block = entry.block
    if hasattr(block, 'inline_content'):
        return block.inline_content
    return getattr(block, 'content', '')


def _attach_reused_entries(entries: Sequence[_BlockEntry]) -> None:
    for entry in entries:
        if entry.inline_leaf and entry.children:
            entry.block.children = entry.children
            entry.block.inline_matches = list(entry.inline_matches)
            entry.block.inline_dependencies = list(entry.inline_dependencies)
        elif not entry.inline_leaf and entry.children:
            entry.block.children = entry.children


def _parse_missing_inline(entries: Sequence[_BlockEntry], document: block_token.Document) -> None:
    previous_suppress = span_token._suppress_inline_parsing
    previous_sink = core_tokens._inline_sink
    previous_dependencies = core_tokens._inline_dependencies
    previous_root = token._root_node
    span_token._suppress_inline_parsing = False
    token._root_node = document
    try:
        for entry in entries:
            if not entry.inline_leaf or entry.children:
                continue
            block = entry.block
            sink: list[tuple[object, object]] = []
            dependencies: list[tuple[str, object]] = []
            core_tokens._inline_sink = sink
            core_tokens._inline_dependencies = dependencies
            try:
                block.children = span_token.tokenize_inner(block.inline_content)
            finally:
                core_tokens._inline_sink = previous_sink
                core_tokens._inline_dependencies = previous_dependencies
            block.inline_matches = sink
            block.inline_dependencies = dependencies
            entry.children = tuple(block.children or ())
            entry.inline_matches = tuple(sink)
            entry.inline_dependencies = tuple(dependencies)
    finally:
        span_token._suppress_inline_parsing = previous_suppress
        core_tokens._inline_sink = previous_sink
        core_tokens._inline_dependencies = previous_dependencies
        token._root_node = previous_root


def _collect_references(
    entries: Sequence[_BlockEntry],
    definitions: Sequence[DefinitionRecord],
) -> tuple[ReferenceRecord, ...]:
    effective = {item.label: item for item in definitions if item.effective}
    references: list[ReferenceRecord] = []
    for entry in entries:
        if not entry.inline_map:
            continue
        block = entry.block
        for token_object, match in entry.inline_matches:
            start_line, start_column = _map_position(entry.inline_map, match.start())
            end_line, end_column = _map_end_position(entry.inline_map, match.end())
            dest_type = getattr(token_object, 'dest_type', None)
            if dest_type in ('uri', 'angle_uri'):
                reference_kind = 'inline'
                label = None
                raw_label = None
            elif dest_type == 'full':
                reference_kind = 'full'
                raw_label = getattr(token_object, 'label', None)
                label = normalize_label(raw_label) if raw_label is not None else None
            else:
                reference_kind = dest_type or 'shortcut'
                raw_label = match.group(1)
                label = normalize_label(raw_label) if raw_label else None
            destination = getattr(token_object, 'target', getattr(token_object, 'src', ''))
            title = getattr(token_object, 'title', '')
            references.append(ReferenceRecord(
                index=len(references),
                kind='image' if isinstance(token_object, span_token.Image) else 'link',
                reference_kind=reference_kind,
                destination=destination,
                title=title,
                text=match.group(1),
                label=label,
                raw_label=raw_label,
                definition=effective.get(label) if label is not None else None,
                range=SourceRange(
                    Position(start_line, start_column + 1),
                    Position(end_line, end_column + 1),
                ),
                block_line=block.line_number,
                block_end_line=getattr(block, 'end_line_number', None),
            ))
    return tuple(references)


def _map_position(inline_map: Sequence[tuple[int, int]], index: int) -> tuple[int, int]:
    if not inline_map:
        return 1, 0
    if index < len(inline_map):
        return inline_map[index]
    return inline_map[-1]


def _map_end_position(inline_map: Sequence[tuple[int, int]], index: int) -> tuple[int, int]:
    if not inline_map:
        return 1, 0
    if index <= 0:
        return inline_map[0]
    line, column = inline_map[min(index, len(inline_map)) - 1]
    return line, column + 1


def _build_inline_map(
    block: block_token.BlockToken,
    document: str,
) -> Optional[tuple[tuple[int, int], ...]]:
    if not hasattr(block, 'inline_content'):
        return None
    content = block.inline_content
    start_line = block.line_number or 1
    end_line = getattr(block, 'end_line_number', None) or start_line
    raw_lines = [
        _line_text(document, number)
        for number in range(start_line, end_line + 1)
    ]
    if isinstance(block, block_token.TableCell):
        raw = raw_lines[0] if raw_lines else ''
        index = raw.find(content) if content else -1
        if index >= 0:
            return _map_cell_content(raw[index:], content, start_line, index)
        source_start = getattr(block, 'source_start', None)
        if source_start is not None:
            return _map_cell_content(raw[source_start:], content, start_line, source_start)
    return _align_lines(content, raw_lines, start_line)


def _align_lines(
    content: str,
    raw_lines: Sequence[str],
    start_line: int,
) -> tuple[tuple[int, int], ...]:
    content_lines = content.split('\n')
    if len(content_lines) > len(raw_lines):
        return tuple((start_line, column) for column in range(len(content)))
    mapping: list[tuple[int, int]] = []
    for offset, content_line in enumerate(content_lines):
        raw = raw_lines[offset]
        line = start_line + offset
        if content_line:
            index = raw.find(content_line)
        else:
            index = -1
        if index < 0:
            stripped = raw.lstrip()
            index = len(raw) - len(stripped)
        if offset:
            mapping.append((line - 1, len(raw_lines[offset - 1])))
        mapping.extend((line, index + column) for column in range(len(content_line)))
    return tuple(mapping)


def _map_cell_content(
    cell_text: str,
    content: str,
    line: int,
    start_column: int,
) -> tuple[tuple[int, int], ...]:
    built: list[str] = []
    mapping: list[tuple[int, int]] = []
    index = 0
    while index < len(cell_text):
        character = cell_text[index]
        if character == '\\' and index + 1 < len(cell_text) and cell_text[index + 1] == '|':
            built.append('|')
            mapping.append((line, start_column + index))
            index += 2
            continue
        built.append(character)
        mapping.append((line, start_column + index))
        index += 1
    built_text = ''.join(built)
    if built_text.startswith(content):
        return tuple(mapping[:len(content)])
    index = built_text.find(content)
    if index >= 0:
        return tuple(mapping[index:index + len(content)])
    return tuple(mapping[:len(content)])


def _coerce_edit(
    edit: Union[TextEdit, LineColumnEdit, Sequence, Mapping],
    document: str,
) -> TextEdit:
    if isinstance(edit, TextEdit):
        return edit
    if isinstance(edit, LineColumnEdit):
        return edit.resolve(document)
    if isinstance(edit, Mapping):
        if 'start_line' in edit:
            return LineColumnEdit(
                int(edit['start_line']),
                int(edit['start_column']),
                int(edit['end_line']),
                int(edit['end_column']),
                str(edit.get('text', edit.get('replacement', ''))),
            ).resolve(document)
        return TextEdit(
            int(edit['start']),
            int(edit['end']),
            str(edit.get('text', edit.get('replacement', ''))),
        )
    values = list(edit)
    if len(values) == 2:
        values.append('')
    if len(values) != 3:
        raise ValueError('edit must have two or three values: {!r}'.format(edit))
    return TextEdit(int(values[0]), int(values[1]), str(values[2]))


def _apply_edits(document: str, edits: Sequence[TextEdit]) -> tuple[str, '_OffsetTranslator']:
    ordered = sorted(edits, key=lambda item: (item.start, item.end))
    previous_end = -1
    for edit in ordered:
        if edit.start < previous_end:
            raise ValueError('overlapping edits: {!r}'.format(ordered))
        previous_end = max(previous_end, edit.end)
    if ordered and ordered[-1].end > len(document):
        raise ValueError('edit range is past the end of the document')
    pieces: list[str] = []
    cursor = 0
    segments: list[tuple[int, int, int]] = []
    for edit in ordered:
        pieces.append(document[cursor:edit.start])
        replaced_at = sum(len(piece) for piece in pieces)
        pieces.append(edit.text)
        segments.append((edit.start, edit.end, replaced_at))
        cursor = edit.end
    pieces.append(document[cursor:])
    new_text = ''.join(pieces)
    translator = _OffsetTranslator(document, new_text, segments)
    return new_text, translator


class _LineMap:
    """Fast 1-based line/column to offset conversion for a fixed document."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = _line_offsets(text)

    def offset(self, line: int, column: int) -> int:
        index = min(max(line - 1, 0), len(self.starts) - 1)
        return self.starts[index] + max(column - 1, 0)


class _OffsetTranslator:
    """Map offsets in a new document back to surviving offsets in the old one."""

    def __init__(
        self,
        before: str,
        after: str,
        segments: Sequence[tuple[int, int, int]],
    ) -> None:
        self.before = before
        self.after = after
        self.segments = tuple(segments)
        self.survivors: list[tuple[int, int]] = []
        self._new_ranges: list[tuple[int, int, int, bool]] = []
        cursor = 0
        for start, end, replaced_at in self.segments:
            if start > cursor:
                length = start - cursor
                self.survivors.append((cursor, start))
                self._new_ranges.append((replaced_at - length, replaced_at, cursor, False))
            cursor = end
        if cursor < len(before):
            tail_length = len(before) - cursor
            self.survivors.append((cursor, len(before)))
            self._new_ranges.append((len(after) - tail_length, len(after), cursor, False))
        for start, end, replaced_at in self.segments:
            self._new_ranges.append((replaced_at, replaced_at + (end - start), start, True))

    def source_of(self, offset: int) -> Optional[int]:
        """Return the old offset a new offset descends from, if it survived."""
        for new_start, new_end, old_start, replaced in self._new_ranges:
            if new_start <= offset < new_end:
                if replaced:
                    return None
                return old_start + (offset - new_start)
        return None

    def replaced(self, line: int, column: int) -> bool:
        """Return whether a new line/column falls inside replaced text."""
        offset = _LineMap(self.after).offset(line, column)
        for new_start, new_end, _old_start, replaced in self._new_ranges:
            if replaced and new_start <= offset < new_end:
                return True
        return False


def _compare_versions(before: VersionIndex, after: VersionIndex) -> ChangeSet:
    before_definitions = before.effective_definitions
    after_definitions = after.effective_definitions
    changed_definitions: list[DefinitionChange] = []
    for label in sorted(set(before_definitions) | set(after_definitions)):
        old = before_definitions.get(label)
        new = after_definitions.get(label)
        if old is None or new is None:
            changed_definitions.append(DefinitionChange(label, old, new))
            continue
        if old.identity != new.identity or old.signature() != new.signature():
            changed_definitions.append(DefinitionChange(label, old, new))

    before_counts = _reference_counts(before.references)
    after_counts = _reference_counts(after.references)
    added_references = _count_difference(after.references, before_counts, after_counts)
    removed_references = _count_difference(before.references, after_counts, before_counts)

    labels_to_recheck = {item.label for item in changed_definitions if item.label is not None}
    url_to_recheck: set[str] = set()
    for reference in list(added_references) + list(removed_references):
        if reference.label is not None:
            labels_to_recheck.add(reference.label)
        else:
            url_to_recheck.add(reference.destination)
    return ChangeSet(
        before_version=before.version,
        after_version=after.version,
        added_references=tuple(added_references),
        removed_references=tuple(removed_references),
        changed_definitions=tuple(changed_definitions),
        resources_to_recheck=tuple(sorted(labels_to_recheck)),
        url_resources_to_recheck=tuple(sorted(url_to_recheck)),
    )


def _reference_counts(
    references: Sequence[ReferenceRecord],
) -> dict[tuple, tuple[int, list[ReferenceRecord]]]:
    counts: dict[tuple, tuple[int, list[ReferenceRecord]]] = {}
    for reference in references:
        signature = reference.signature()
        count, items = counts.get(signature, (0, []))
        items.append(reference)
        counts[signature] = (count + 1, items)
    return counts


def _count_difference(
    references: Sequence[ReferenceRecord],
    other_counts: Mapping[tuple, tuple[int, list[ReferenceRecord]]],
    own_counts: Mapping[tuple, tuple[int, list[ReferenceRecord]]],
) -> list[ReferenceRecord]:
    emitted: dict[tuple, int] = {}
    result: list[ReferenceRecord] = []
    for reference in references:
        signature = reference.signature()
        available = other_counts.get(signature, (0, []))[0]
        already = emitted.get(signature, 0)
        if already >= available:
            emitted[signature] = already + 1
            result.append(reference)
    return result


def _verify_version(version: VersionIndex) -> VerificationResult:
    full = _build_version(version.text, version=version.version, previous=None)
    errors: list[str] = []
    if _definition_signatures(full.definitions) != _definition_signatures(version.definitions):
        errors.append('definitions differ from a full re-parse')
    if _reference_signatures(full.references) != _reference_signatures(version.references):
        errors.append('references or ranges differ from a full re-parse')
    return VerificationResult(version.version, not errors, tuple(errors))


def _definition_signatures(definitions: Sequence[DefinitionRecord]) -> tuple:
    return tuple(
        (
            item.index,
            item.label,
            item.destination,
            item.title,
            item.effective,
            item.range.start.line,
            item.range.start.column,
            item.range.end.line,
            item.range.end.column,
        )
        for item in definitions
    )


def _reference_signatures(references: Sequence[ReferenceRecord]) -> tuple:
    return tuple(
        (
            item.kind,
            item.reference_kind,
            item.label,
            item.destination,
            item.title,
            item.range.start.line,
            item.range.start.column,
            item.range.end.line,
            item.range.end.column,
            None if item.definition is None else item.definition.index,
        )
        for item in references
    )
