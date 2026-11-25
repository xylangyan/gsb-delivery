"""
    pygments.lexers.sql_jinja_audit
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Source preserving audit helpers for :class:`~.SqlJinjaLexer`.

    The regular ``SqlJinjaLexer`` token stream is intentionally identical to
    ``DelegatingLexer``'s two layer reading: the template layer is stripped
    first, the remaining SQL is lexed, and the template tokens are inserted
    back.  That is the right token classification, but it loses the fact that
    two SQL fragments separated by a template expression used to be one SQL
    token.

    This module keeps the original token stream and adds the missing lineage:

    * every token is accompanied by an exact ``[start, end)`` source interval;
    * all fragments belonging to one real SQL token share a ``sql_index``;
    * SQL literals get an occurrence based :class:`LiteralItem`, whose text is
      the concatenation of its SQL fragments only (template text is excluded);
    * template expressions remain separate, individually addressable regions;
    * identical literal texts remain separate items because items are keyed by
      occurrence, not by their text.

    The helper works on a supplied slice of source.  Window, fragment and
    round based helpers below never require the caller to concatenate a whole
    historical draft, and no eager per-occurrence token table is built.

    :copyright: Copyright 2006-present by the Pygments team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Iterator, Mapping, Optional, Sequence

from pygments.lexers.templates import SqlJinjaLexer
from pygments.token import Comment, Keyword, Literal, Name, Number, String, \
    Text, Token


__all__ = [
    'AuditDiff', 'AuditHistory', 'AuditResult', 'AuditToken',
    'Correction', 'FragmentMap', 'FragmentSpec', 'ItemChange', 'LiteralItem',
    'SegmentIndex', 'SourceInterval', 'SourceSpan', 'SpanRef',
    'ClassificationChange', 'TemplateReference', 'TokenChange', 'WindowSpec',
    'apply_correction', 'apply_corrections', 'audit', 'audit_fragments',
    'audit_history', 'audit_sql_jinja', 'audit_sql_template', 'audit_window',
    'build_audit', 'compare', 'compare_audits', 'diff', 'diff_audits',
    'lex_sql_jinja', 'lex_sql_template', 'lex_window',
    'correction_sheet', 'apply_correction_window', 'compare_classification',
    'compare_tokens',
    'reclassified_tokens', 'literal_items', 'literals', 'tokenize',
    'tokenize_with_spans', 'sequence_items', 'compare_sources',
    'compare_text',
]


# ---------------------------------------------------------------------------
# Small value objects
# ---------------------------------------------------------------------------

def _as_span(value) -> 'SourceSpan':
    if isinstance(value, SourceSpan):
        return value
    try:
        start, end = value
    except (TypeError, ValueError):
        raise TypeError(f'cannot use {value!r} as a source span') from None
    return SourceSpan(start, end)


@dataclass(frozen=True, eq=False)
class SourceSpan:
    """A half-open ``[start, end)`` interval in the audit source."""

    start: int
    end: int

    def __post_init__(self):
        if self.start < 0 or self.end < self.start:
            raise ValueError(f'invalid source span {self.start}:{self.end}')

    @property
    def length(self) -> int:
        return self.end - self.start

    def slice(self, text: str) -> str:
        return text[self.start:self.end]

    def overlaps(self, other: 'SourceSpan') -> bool:
        other = _as_span(other)
        return self.start < other.end and other.start < self.end

    def contains(self, other: 'SourceSpan') -> bool:
        other = _as_span(other)
        return self.start <= other.start and other.end <= self.end

    def as_tuple(self) -> tuple[int, int]:
        return (self.start, self.end)

    def __iter__(self):
        yield self.start
        yield self.end

    def __len__(self):
        return 2

    def __getitem__(self, index):
        return (self.start, self.end)[index]

    def __eq__(self, other):
        if isinstance(other, SourceSpan):
            return (self.start, self.end) == (other.start, other.end)
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return (self.start, self.end) == (other[0], other[1])
        return NotImplemented

    def __lt__(self, other):
        return (self.start, self.end) < _as_span(other).as_tuple()

    def __le__(self, other):
        return (self.start, self.end) <= _as_span(other).as_tuple()

    def __gt__(self, other):
        return (self.start, self.end) > _as_span(other).as_tuple()

    def __ge__(self, other):
        return (self.start, self.end) >= _as_span(other).as_tuple()

    def __hash__(self):
        return hash((self.start, self.end))


#: Alias kept for callers that use the word ``interval``.
SourceInterval = SourceSpan


@dataclass(frozen=True)
class SpanRef:
    """An interval, optionally tied to a fragment and a history round.

    ``start`` and ``end`` are absolute offsets when ``FragmentSpec.start`` was
    supplied.  ``local_start`` and ``local_end`` always refer to the fragment
    text itself.
    """

    round: Optional[int]
    fragment: Optional[str]
    start: int
    end: int
    local_start: int = 0
    local_end: int = 0

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(self.start, self.end)

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def round_no(self):
        return self.round

    @property
    def fragment_id(self):
        return self.fragment

    @property
    def fragment_key(self):
        """The caller's fragment id plus local offsets, for fragment lookup."""
        return (self.fragment, self.local_start, self.local_end)

    @property
    def source_span(self) -> SourceSpan:
        return self.span

    def as_tuple(self):
        return (self.round, self.fragment, self.start, self.end)


@dataclass(frozen=True)
class FragmentSpec:
    """One reusable piece of historical source.

    A round may consist of several fragments, joined without an artificial
    separator, so a token at an old fragment tail and a token at the next
    fragment head can still be read as one syntax construct.
    """

    text: str
    fragment: Optional[str] = None
    start: int = 0
    round: Optional[int] = None
    end: Optional[int] = None

    def __post_init__(self):
        if self.end is not None and self.end < self.start:
            raise ValueError('fragment end must not precede its start')

    @property
    def absolute_end(self) -> int:
        if self.end is not None:
            return self.end
        return self.start + len(self.text)

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(self.start, self.absolute_end)


@dataclass(frozen=True)
class WindowSpec:
    """A small source window to audit.

    ``text`` is already the slice, while ``start`` records its absolute
    position in the larger draft.  ``end`` is accepted for callers that prefer
    the conventional ``start:end`` notation; when present it is the absolute
    end of the window.
    """

    text: str
    round: Optional[int] = None
    start: int = 0
    end: Optional[int] = None
    fragment: Optional[str] = None

    def __post_init__(self):
        if self.end is not None and self.end < self.start:
            raise ValueError('window end must not precede its start')

    @property
    def absolute_end(self) -> int:
        return self.end if self.end is not None else self.start + len(self.text)

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(self.start, self.absolute_end)


@dataclass(frozen=True)
class AuditToken:
    """A token in the original source, with both source and lineage.

    The tuple ``(token_type, value)`` is exactly what
    ``SqlJinjaLexer.get_tokens_unprocessed`` emits.  When one real SQL token is
    interrupted by template text, the SQL lexer's token is split into several
    ``AuditToken`` objects; they all retain the same ``sql_index`` and receive
    consecutive ``segment_index`` values.

    ``part`` is ``'sql'`` for a SQL fragment and ``'template'`` for a
    template token.  Template tokens have a ``template_index`` and SQL tokens
    have a ``sql_index``; the inactive index is ``None``.
    """

    start: int
    end: int
    token_type: object
    value: str
    part: str = 'sql'
    sql_index: Optional[int] = None
    segment_index: Optional[int] = None
    template_index: Optional[int] = None
    round: Optional[int] = None
    fragment: Optional[str] = None
    source_start: Optional[int] = None
    source_end: Optional[int] = None

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(self.start, self.end)

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def type(self):
        return self.token_type

    @property
    def tokentype(self):
        return self.token_type

    @property
    def text(self) -> str:
        return self.value

    @property
    def is_sql(self) -> bool:
        return self.part == 'sql'

    @property
    def is_template(self) -> bool:
        return self.part == 'template'

    @property
    def absolute_span(self) -> SourceSpan:
        start = self.start if self.source_start is None else self.source_start
        end = self.end if self.source_end is None else self.source_end
        return SourceSpan(start, end)

    def record(self) -> dict:
        """A plain serialisable token record."""
        return {
            'start': self.start,
            'end': self.end,
            'type': str(self.token_type),
            'value': self.value,
            'part': self.part,
            'sql_index': self.sql_index,
            'segment_index': self.segment_index,
            'template_index': self.template_index,
            'round': self.round,
            'fragment': self.fragment,
        }

    @property
    def source_span(self) -> SourceSpan:
        return self.span

    @property
    def offset(self) -> int:
        return self.start

    @property
    def end_offset(self) -> int:
        return self.end

    @property
    def is_literal(self) -> bool:
        return self.token_type in Literal

    def slice(self, text: str) -> str:
        return text[self.start:self.end]

    def as_unprocessed(self) -> tuple[int, object, str]:
        return (self.start, self.token_type, self.value)

    def as_span_tuple(self) -> tuple[int, int, object, str]:
        return (self.start, self.end, self.token_type, self.value)

    def __iter__(self):
        # Deliberately matches Pygments' (index, token_type, value) triples, so
        # ``for index, token_type, value in result.tokens`` works unchanged.
        yield self.start
        yield self.token_type
        yield self.value

    def __getitem__(self, index):
        return self.as_unprocessed()[index]


@dataclass(frozen=True)
class TemplateReference:
    """One template region, e.g. ``{{ depot }}`` or ``{% if x %}``."""

    index: int
    start: int
    end: int
    text: str
    kind: str
    tokens: tuple[AuditToken, ...]
    variable_names: tuple[str, ...] = ()
    round: Optional[int] = None
    fragment: Optional[str] = None

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(self.start, self.end)

    @property
    def variables(self) -> tuple[str, ...]:
        return self.variable_names

    @property
    def name(self) -> Optional[str]:
        return self.variable_names[0] if self.variable_names else None

    @property
    def variable(self) -> Optional[str]:
        return self.name

    @property
    def source_span(self) -> SourceSpan:
        return self.span

    @property
    def is_variable(self) -> bool:
        return self.kind == 'variable'

    @property
    def is_block(self) -> bool:
        return self.kind == 'block'

    @property
    def is_comment(self) -> bool:
        return self.kind == 'comment'

    @property
    def closed(self) -> bool:
        if self.kind == 'text':
            return True
        if not self.tokens:
            return False
        last = self.tokens[-1]
        if self.kind == 'comment':
            return last.value.endswith('#}')
        closing = {'variable': '}}', 'block': '%}'}.get(self.kind)
        if closing is None:
            return False
        return (last.token_type in Comment.Preproc and last.value == closing)

    @property
    def unfinished(self) -> bool:
        return not self.closed

    @property
    def complete(self) -> bool:
        return self.closed


@dataclass(frozen=True)
class LiteralItem:
    """One real SQL literal token, assembled from all of its source pieces.

    ``text`` is the pre-split SQL word core: only SQL fragments are joined.
    ``template_references`` and ``template_parts`` describe what was inserted
    between the fragments and are deliberately not part of ``text``.
    ``item_id`` is the occurrence number in this audit; two literals with the
    same text therefore remain two distinct items.
    """

    item_id: int
    literal_index: int
    sql_index: int
    token_type: object
    text: str
    segments: tuple[AuditToken, ...]
    template_parts: tuple[AuditToken, ...] = ()
    template_references: tuple[TemplateReference, ...] = ()
    start: int = 0
    end: int = 0
    round: Optional[int] = None
    fragment: Optional[str] = None
    window_start: int = 0

    @property
    def uid(self) -> str:
        prefix = '' if self.round is None else f'{self.round}:'
        return f'{prefix}{self.item_id}'

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(self.start, self.end)

    @property
    def source_spans(self) -> tuple[SourceSpan, ...]:
        return tuple(seg.span for seg in self.segments)

    @property
    def absolute_span(self) -> SourceSpan:
        return SourceSpan(self.window_start + self.start,
                          self.window_start + self.end)

    @property
    def absolute_spans(self) -> tuple[SourceSpan, ...]:
        return tuple(SourceSpan(self.window_start + seg.start,
                                self.window_start + seg.end)
                     for seg in self.segments)

    @property
    def source_intervals(self) -> tuple[SourceSpan, ...]:
        return self.source_spans

    @property
    def fixed_text(self) -> str:
        return self.text

    @property
    def value(self) -> str:
        return self.text

    @property
    def word(self) -> str:
        return self.text

    @property
    def type(self):
        return self.token_type

    @property
    def segment_spans(self) -> tuple[SourceSpan, ...]:
        return tuple(seg.span for seg in self.segments)

    @property
    def template_text(self) -> str:
        return ''.join(ref.text for ref in self.template_references)

    @property
    def variables(self) -> tuple[str, ...]:
        names = []
        for ref in self.template_references:
            names.extend(ref.variable_names)
        return tuple(names)

    @property
    def variable_names(self) -> tuple[str, ...]:
        return self.variables

    @property
    def occurrence(self) -> int:
        return self.item_id

    @property
    def parts(self) -> tuple[AuditToken, ...]:
        return self.segments

    @property
    def template_spans(self) -> tuple[SourceSpan, ...]:
        return tuple(ref.span for ref in self.template_references)

    @property
    def rounds(self) -> tuple[int, ...]:
        rv = []
        for segment in self.segments:
            if segment.round is not None and segment.round not in rv:
                rv.append(segment.round)
        return tuple(rv)

    @property
    def fragments(self) -> tuple[str, ...]:
        rv = []
        for segment in self.segments:
            if segment.fragment is not None and segment.fragment not in rv:
                rv.append(segment.fragment)
        return tuple(rv)

    @property
    def content(self) -> str:
        """The fixed text without the outer SQL quote delimiters."""
        text = self.text
        if (len(text) >= 2 and self.token_type in String
                and text[0] == text[-1] and text[0] in ('\'', '"')):
            return text[1:-1].replace(text[0] * 2, text[0])
        return text

    @property
    def literal(self) -> str:
        return self.content

    @property
    def raw(self) -> str:
        return self.text


@dataclass(frozen=True)
class ItemChange:
    """Difference for one occurrence based literal item."""

    kind: str
    old: Optional[LiteralItem] = None
    new: Optional[LiteralItem] = None

    @property
    def changed(self) -> bool:
        return self.kind == 'changed'

    @property
    def added(self) -> bool:
        return self.kind == 'added'

    @property
    def removed(self) -> bool:
        return self.kind == 'removed'

    @property
    def unchanged(self) -> bool:
        return self.kind == 'unchanged'

    @property
    def item(self) -> Optional[LiteralItem]:
        return self.new if self.new is not None else self.old


@dataclass(frozen=True)
class TokenChange:
    """Difference for one token of the visible two layer reading."""

    kind: str
    old: Optional[AuditToken] = None
    new: Optional[AuditToken] = None

    @property
    def token(self) -> Optional[AuditToken]:
        return self.new if self.new is not None else self.old

    @property
    def reclassified(self) -> bool:
        return self.kind == 'reclassified'

    @property
    def changed(self) -> bool:
        return self.kind in ('changed', 'reclassified')

    @property
    def same_text(self) -> bool:
        return (self.old is not None and self.new is not None
                and self.old.value == self.new.value)

    @property
    def old_type(self):
        return self.old.token_type if self.old is not None else None

    @property
    def new_type(self):
        return self.new.token_type if self.new is not None else None


@dataclass(frozen=True)
class ClassificationChange:
    """A run of unchanged source characters whose classification changed.

    ``old_span`` and ``new_span`` may differ in length when a correction
    shifted the source.  ``text`` is the unchanged source text shared by both
    sides.  ``old_types`` / ``new_types`` list the distinct token types and
    layer kinds seen inside the run, so a comment that was split into several
    template tokens is still reported as one reclassified region.
    """

    old_span: SourceSpan
    new_span: SourceSpan
    text: str
    old_types: tuple[tuple[object, str], ...]
    new_types: tuple[tuple[object, str], ...]

    @property
    def old_type(self):
        return self.old_types[0][0] if self.old_types else None

    @property
    def new_type(self):
        return self.new_types[0][0] if self.new_types else None

    @property
    def old_part(self):
        return self.old_types[0][1] if self.old_types else None

    @property
    def new_part(self):
        return self.new_types[0][1] if self.new_types else None

    @property
    def reclassified(self) -> bool:
        return True


@dataclass(frozen=True)
class AuditDiff:
    """Occurrence aware comparison of two audits."""

    old: 'AuditResult'
    new: 'AuditResult'
    changes: tuple[ItemChange, ...]

    @property
    def added(self) -> tuple[LiteralItem, ...]:
        return tuple(c.new for c in self.changes if c.kind == 'added')

    @property
    def removed(self) -> tuple[LiteralItem, ...]:
        return tuple(c.old for c in self.changes if c.kind == 'removed')

    @property
    def changed(self) -> tuple[ItemChange, ...]:
        return tuple(c for c in self.changes if c.kind == 'changed')

    @property
    def unchanged(self) -> tuple[LiteralItem, ...]:
        return tuple(c.new for c in self.changes if c.kind == 'unchanged')

    @property
    def changed_items(self) -> tuple[ItemChange, ...]:
        return self.changed

    @property
    def added_items(self) -> tuple[LiteralItem, ...]:
        return self.added

    @property
    def removed_items(self) -> tuple[LiteralItem, ...]:
        return self.removed

    @property
    def unchanged_items(self) -> tuple[LiteralItem, ...]:
        return self.unchanged

    @property
    def items(self) -> tuple[ItemChange, ...]:
        return self.changes

    @property
    def has_changes(self) -> bool:
        return any(c.kind != 'unchanged' for c in self.changes)

    @property
    def token_changes(self) -> tuple[TokenChange, ...]:
        """Full token-level diff, including reclassified template tokens."""
        return compare_tokens(self.old, self.new)

    @property
    def token_diff(self) -> tuple[TokenChange, ...]:
        return self.token_changes

    @property
    def reclassified(self) -> tuple[TokenChange, ...]:
        return tuple(change for change in self.token_changes
                     if change.kind == 'reclassified')

    @property
    def classification_changes(self) -> tuple[ClassificationChange, ...]:
        """Runs of unchanged characters whose layer/type classification moved."""
        return compare_classification(self.old, self.new)

    @property
    def reclassified_spans(self) -> tuple[ClassificationChange, ...]:
        return self.classification_changes

    def for_round(self, round_no: int) -> tuple[ItemChange, ...]:
        rv = []
        for change in self.changes:
            item = change.item
            if item is not None and item.round == round_no:
                rv.append(change)
        return tuple(rv)

    def __iter__(self):
        return iter(self.changes)


class FragmentMap:
    """Map offsets in a joined round back to original fragment intervals.

    ``audit_fragments`` joins only the fragments that belong to the requested
    round, so lexical state can flow from an old fragment tail into the next
    fragment head without the caller ever concatenating the whole draft.

    ``separator`` is optional.  When supplied, it is inserted between fragments
    in the joined text but is not attributed to either fragment, so intervals
    returned by :meth:`locate` never include separator characters.
    """

    def __init__(self, fragments: Sequence[FragmentSpec], separator: str = ''):
        self.fragments = tuple(fragments)
        self.separator = separator
        self._starts = []
        self._specs = []
        combined = 0
        for index, spec in enumerate(self.fragments):
            self._starts.append(combined)
            self._specs.append(spec)
            combined += len(spec.text)
            if separator and index < len(self.fragments) - 1:
                combined += len(separator)
        self.length = combined

    def __bool__(self):
        return bool(self.fragments)

    def locate(self, start: int, end: int,
               round_no: Optional[int] = None) -> tuple[SpanRef, ...]:
        """Return original intervals covering ``[start, end)`` in joined text."""
        if start == end:
            return ()
        rv = []
        for index, (combined_start, spec) in enumerate(
                zip(self._starts, self._specs)):
            combined_end = combined_start + len(spec.text)
            if combined_end <= start:
                continue
            if combined_start >= end:
                break
            local_start = max(start, combined_start) - combined_start
            local_end = min(end, combined_end) - combined_start
            if local_start >= local_end:
                continue
            sfrag = spec.fragment
            sround = spec.round if spec.round is not None else round_no
            rv.append(SpanRef(
                round=sround,
                fragment=sfrag,
                start=spec.start + local_start,
                end=spec.start + local_end,
                local_start=local_start,
                local_end=local_end,
            ))
        return tuple(rv)

    def text_between(self, start: int, end: int) -> str:
        pieces = []
        for cs, spec in zip(self._starts, self._specs):
            spec_end = cs + len(spec.text)
            if spec_end <= start or cs >= end:
                continue
            pieces.append(spec.text[max(0, start - cs):min(len(spec.text),
                                                          end - cs)])
        return self.separator.join(pieces)


class SegmentIndex:
    """Bidirectional index over tokens, SQL words and literal items.

    ``by_sql_word`` is the requested lineage index: every SQL fragment from
    the same actual SQL token is in the same tuple, even when template text
    sits between the fragments.
    """

    def __init__(self, result: 'AuditResult'):
        self.result = result
        self.by_sql_word: dict[int, tuple[AuditToken, ...]] = {}
        self.by_template: dict[int, tuple[AuditToken, ...]] = {}
        self.by_span: dict[tuple[int, int, str], AuditToken] = {}
        self.by_item: dict[int, LiteralItem] = {}
        self.item_by_sql_word: dict[int, LiteralItem] = {}
        self.sql_word_by_segment: dict[tuple[int, int, str], int] = {}
        for token in result.tokens:
            self.by_span[(token.start, token.end, token.part)] = token
            if token.sql_index is not None:
                self.by_sql_word.setdefault(token.sql_index, ())
                self.by_sql_word[token.sql_index] += (token,)
                self.sql_word_by_segment[
                    (token.start, token.end, token.part)] = token.sql_index
            if token.template_index is not None:
                self.by_template.setdefault(token.template_index, ())
                self.by_template[token.template_index] += (token,)
        for item in result.items:
            self.by_item[item.item_id] = item
            self.item_by_sql_word[item.sql_index] = item

    @property
    def by_actual_sql_word(self) -> dict[int, tuple[AuditToken, ...]]:
        return self.by_sql_word

    @property
    def word_index(self) -> dict[int, tuple[AuditToken, ...]]:
        return self.by_sql_word

    def segments_for_sql(self, sql_index: int) -> tuple[AuditToken, ...]:
        return self.by_sql_word.get(sql_index, ())

    def group_of(self, segment_or_span) -> Optional[int]:
        """Return the actual SQL word id for one fragment or interval."""
        if isinstance(segment_or_span, AuditToken):
            return segment_or_span.sql_index
        if isinstance(segment_or_span, SourceSpan):
            key = (segment_or_span.start, segment_or_span.end, 'sql')
            return self.sql_word_by_segment.get(key)
        if isinstance(segment_or_span, tuple):
            return self.sql_word_by_segment.get(tuple(segment_or_span))
        if isinstance(segment_or_span, int):
            return segment_or_span if segment_or_span in self.by_sql_word else None
        return None

    def word_of(self, segment_or_span) -> Optional[int]:
        return self.group_of(segment_or_span)

    def segments(self, group_id: int) -> tuple[AuditToken, ...]:
        return self.segments_for_sql(group_id)

    def groups(self) -> tuple[tuple[AuditToken, ...], ...]:
        return tuple(self.by_sql_word[index] for index in sorted(self.by_sql_word))

    def equivalences(self, segment_or_span=None):
        """Map every SQL fragment interval to all fragments of its word.

        With an argument, restrict the result to the group containing that
        fragment or interval.  This is the join index used by auditors that
        start from one visible SQL piece and need the other pieces.
        """
        if segment_or_span is not None:
            group_id = self.group_of(segment_or_span)
            if group_id is None:
                return {}
            group = self.by_sql_word.get(group_id, ())
            return {(tok.start, tok.end): group for tok in group}
        rv = {}
        for group in self.by_sql_word.values():
            for token in group:
                rv[(token.start, token.end)] = group
        return rv

    def siblings(self, segment_or_span) -> tuple[AuditToken, ...]:
        """All fragments of the same word except the one supplied."""
        group = self.same_word(segment_or_span)
        if isinstance(segment_or_span, AuditToken):
            return tuple(tok for tok in group if tok is not segment_or_span)
        return group

    def segments_for_template(self, template_index: int) -> tuple[AuditToken, ...]:
        return self.by_template.get(template_index, ())

    def same_word(self, segment_or_span) -> tuple[AuditToken, ...]:
        """Return every fragment of the same real SQL token."""
        sql_index = None
        if isinstance(segment_or_span, AuditToken):
            sql_index = segment_or_span.sql_index
        elif isinstance(segment_or_span, SourceSpan):
            token = self.by_span.get(
                (segment_or_span.start, segment_or_span.end, 'sql'))
            if token is not None:
                sql_index = token.sql_index
        elif isinstance(segment_or_span, tuple):
            token = self.by_span.get(tuple(segment_or_span))
            if token is not None:
                sql_index = token.sql_index
        elif isinstance(segment_or_span, int):
            sql_index = segment_or_span
        if sql_index is None:
            return ()
        return self.segments_for_sql(sql_index)

    def item_for_segment(self, segment: AuditToken) -> Optional[LiteralItem]:
        if segment.sql_index is None:
            return None
        return self.item_by_sql_word.get(segment.sql_index)

    def item_for_sql_word(self, sql_index: int) -> Optional[LiteralItem]:
        return self.item_by_sql_word.get(sql_index)

    def item_at(self, start: int, end: Optional[int] = None) -> Optional[LiteralItem]:
        for item in self.result.items:
            if item.start <= start and (end is None or end <= item.end):
                return item
        return None


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def _is_literal_token(token_type) -> bool:
    return token_type in Literal


def _is_template_open_value(value: str) -> bool:
    return value in ('{{', '{%')


def _is_opening(token: AuditToken) -> bool:
    return (token.part == 'template' and token.token_type in Comment.Preproc
            and _is_template_open_value(token.value))


def _is_comment_token(token: AuditToken) -> bool:
    """``{# ... #}`` is emitted whole by the template lexer as ``Comment``."""
    return (token.part == 'template' and token.token_type in Comment
            and token.value.startswith('{#'))


def _template_kind(first_token: AuditToken) -> str:
    value = first_token.value
    if value.startswith('{{'):
        return 'variable'
    if value.startswith('{%'):
        return 'block'
    if value.startswith('{#'):
        return 'comment'
    if first_token.token_type is Text:
        return 'text'
    return 'template'


def _group_template_tokens(tokens: Sequence[AuditToken]) -> list[list[AuditToken]]:
    """Split template-layer tokens into independently addressable regions.

    A region starts at ``{{`` or ``{%``.  ``{# ... #}`` arrives as a single
    ``Comment`` token and forms its own region.  A region also ends when the
    source has a gap, which keeps the grouping honest even if the template
    layer skips text.  A malformed opening with no matching close remains one
    region; that is exactly the visible classification of the original lexer.
    """
    groups: list[list[AuditToken]] = []
    current: list[AuditToken] = []
    previous_end = None
    inside = False
    for token in tokens:
        begin = (_is_opening(token) or _is_comment_token(token)
                 or not inside or token.start != previous_end)
        if current and begin:
            groups.append(current)
            current = []
            inside = False
        current.append(token)
        if _is_opening(token) or _is_comment_token(token):
            inside = True
        if (token.part == 'template' and token.token_type in Comment.Preproc
                and token.value in ('}}', '%}', '#}')):
            inside = False
        previous_end = token.end
    if current:
        groups.append(current)
    return groups


_KEYWORDS_WITH_EXPRESSION = {
    'if', 'for', 'set', 'include', 'import', 'from', 'with', 'block',
    'macro', 'call', 'filter', 'extends', 'do', 'raw',
}


def _variable_names(group: Sequence[AuditToken]) -> tuple[str, ...]:
    """Best-effort variable names for one template region.

    For control blocks, skip the leading keyword (``if``, ``for``, ``set``)
    and punctuation so the reported names are the expression identifiers.
    ``loop``/``block`` are Jinja builtins and are reported as names only when
    they are the sole identifier in a variable expression.
    """
    tokens = list(group)
    names = []
    seen_keyword = False
    for token in tokens:
        if token.token_type in Keyword:
            if token.value in _KEYWORDS_WITH_EXPRESSION and not seen_keyword:
                seen_keyword = True
            continue
        if (token.token_type in Name.Variable and token.value
                and not token.value.startswith('.')
                and token.value.isidentifier()):
            names.append(token.value)
        elif token.token_type in Name.Builtin and not names:
            names.append(token.value)
    # De-duplicate while preserving order.
    rv = []
    for name in names:
        if name not in rv:
            rv.append(name)
    return tuple(rv)


def _make_template_references(groups: Sequence[Sequence[AuditToken]],
                              source: str,
                              round_no: Optional[int],
                              fragment: Optional[str]) -> tuple[TemplateReference, ...]:
    """Build addressable regions from already grouped template tokens."""
    refs = []
    for index, group in enumerate(groups):
        start = group[0].start
        end = group[-1].end
        names = _variable_names(group)
        refs.append(TemplateReference(
            index=index,
            start=start,
            end=end,
            text=source[start:end],
            kind=_template_kind(group[0]),
            tokens=tuple(group),
            variable_names=names,
            round=round_no,
            fragment=fragment,
        ))
    return tuple(refs)


def _coalesce_chunks(lexer: SqlJinjaLexer, text: str):
    """Return ``(stripped_sql, chunks, template_tokens)``.

    ``chunks`` are ``(stripped_start, stripped_end, source_start, source_end)``.
    NULL/empty template tokens are ignored, just as the delegating lexer does.
    """
    buffered = ''
    chunks = []
    template_tokens = []
    for index, token_type, value in lexer.language_lexer.get_tokens_unprocessed(text):
        if not value:
            continue
        if token_type is lexer.needle:
            if (chunks and chunks[-1][1] == len(buffered)
                    and chunks[-1][3] == index):
                old = chunks[-1]
                chunks[-1] = (old[0], len(buffered) + len(value),
                              old[2], index + len(value))
            else:
                chunks.append((len(buffered), len(buffered) + len(value),
                               index, index + len(value)))
            buffered += value
        else:
            template_tokens.append((index, token_type, value))
    return buffered, chunks, template_tokens


def _audit_sql_token_stream(lexer: SqlJinjaLexer, buffered: str, chunks):
    """Yield ``(source_start, source_end, token_type, value, sql_index,
    segment_index)`` for the SQL layer.

    Root-layer tokens that cross a chunk boundary are split at every boundary,
    retaining one ``sql_index`` for all of their pieces.
    """
    chunk_index = 0
    sql_index = 0
    for buffer_start, token_type, value in lexer.root_lexer.get_tokens_unprocessed(buffered):
        if not value:
            continue
        buffer_end = buffer_start + len(value)
        while (chunk_index < len(chunks)
               and chunks[chunk_index][1] <= buffer_start):
            chunk_index += 1
        cursor = chunk_index
        segment_index = 0
        while cursor < len(chunks) and chunks[cursor][0] < buffer_end:
            chunk_start, chunk_end, source_start, _source_end = chunks[cursor]
            piece_start = max(buffer_start, chunk_start)
            piece_end = min(buffer_end, chunk_end)
            if piece_start < piece_end:
                value_start = piece_start - buffer_start
                value_end = piece_end - buffer_start
                yield (source_start + piece_start - chunk_start,
                       source_start + piece_end - chunk_start,
                       token_type, value[value_start:value_end],
                       sql_index, segment_index)
                segment_index += 1
            cursor += 1
        sql_index += 1


def build_audit(text: str, *, lexer: Optional[SqlJinjaLexer] = None,
                source_id: Optional[str] = None,
                round: Optional[int] = None,
                fragment: Optional[str] = None,
                window_start: int = 0,
                fragment_map: Optional[FragmentMap] = None,
                require_sql_literals: bool = True) -> 'AuditResult':
    """Audit one source slice without changing its lexical classification.

    The returned ``tokens`` reproduce ``SqlJinjaLexer.get_tokens_unprocessed``
    one-for-one in ``(token_type, value)`` order.  Each token additionally has
    an exact source interval and lineage metadata.
    """
    if not isinstance(text, str):
        raise TypeError('text must be a str')
    lexer = lexer or SqlJinjaLexer(stripnl=False, ensurenl=False)
    buffered, chunks, template_tokens = _coalesce_chunks(lexer, text)

    tokens: list[AuditToken] = []
    for (start, end, token_type, value,
         sql_index, segment_index) in _audit_sql_token_stream(lexer, buffered, chunks):
        tokens.append(AuditToken(
            start=start, end=end, token_type=token_type, value=value,
            part='sql', sql_index=sql_index, segment_index=segment_index,
            round=round, fragment=fragment,
            source_start=window_start + start,
            source_end=window_start + end,
        ))

    template_audit_tokens = [
        AuditToken(
            start=start, end=start + len(value), token_type=token_type,
            value=value, part='template', round=round, fragment=fragment,
            source_start=window_start + start,
            source_end=window_start + start + len(value),
        )
        for start, token_type, value in template_tokens
    ]
    tokens.extend(template_audit_tokens)
    tokens.sort(key=lambda tok: (tok.start, tok.end, tok.part != 'sql'))

    template_groups = _group_template_tokens(template_audit_tokens)
    group_index_by_token = {}
    for index, group in enumerate(template_groups):
        for token in group:
            group_index_by_token[(token.start, token.end, token.part)] = index
    indexed_tokens = []
    for token in tokens:
        if token.part == 'template':
            index = group_index_by_token.get(
                (token.start, token.end, token.part))
            if index is not None:
                token = AuditToken(
                    start=token.start, end=token.end,
                    token_type=token.token_type, value=token.value,
                    part=token.part, sql_index=token.sql_index,
                    segment_index=token.segment_index,
                    template_index=index,
                    round=round, fragment=fragment,
                    source_start=token.source_start,
                    source_end=token.source_end,
                )
        indexed_tokens.append(token)
    tokens = indexed_tokens
    indexed_by_position = {
        (token.start, token.end, token.part): token for token in tokens
        if token.part == 'template'
    }
    indexed_groups = [
        [indexed_by_position[(token.start, token.end, token.part)]
         for token in group]
        for group in template_groups
    ]
    references = _make_template_references(
        indexed_groups, text, round, fragment)

    by_sql_word: dict[int, list[AuditToken]] = {}
    for token in tokens:
        if token.sql_index is not None:
            by_sql_word.setdefault(token.sql_index, []).append(token)

    items: list[LiteralItem] = []
    for sql_index in sorted(by_sql_word):
        segments = tuple(by_sql_word[sql_index])
        if not segments or not _is_literal_token(segments[0].token_type):
            continue
        item_text = ''.join(segment.value for segment in segments)
        first, last = segments[0], segments[-1]
        interior_refs = []
        interior_tokens = []
        for reference in references:
            if reference.start < first.end or reference.end > last.start:
                continue
            if any(reference.span.overlaps(segment.span) for segment in segments):
                continue
            interior_refs.append(reference)
            interior_tokens.extend(reference.tokens)
        item = LiteralItem(
            item_id=len(items),
            literal_index=len(items),
            sql_index=sql_index,
            token_type=segments[0].token_type,
            text=item_text,
            segments=segments,
            template_parts=tuple(interior_tokens),
            template_references=tuple(interior_refs),
            start=first.start,
            end=last.end,
            round=round,
            fragment=fragment,
            window_start=window_start,
        )
        items.append(item)

    return AuditResult(
        source=text,
        tokens=tuple(tokens),
        items=tuple(items),
        references=references,
        source_id=source_id,
        round=round,
        fragment=fragment,
        window_start=window_start,
        window_end=window_start + len(text),
        fragment_map=fragment_map,
        require_sql_literals=require_sql_literals,
        lexer=lexer,
    )


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

def _reference_record(ref: 'TemplateReference') -> dict:
    return {
        'index': ref.index,
        'start': ref.start,
        'end': ref.end,
        'text': ref.text,
        'kind': ref.kind,
        'variables': list(ref.variable_names),
        'closed': ref.closed,
        'round': ref.round,
        'fragment': ref.fragment,
    }


class AuditResult:
    """Exact token stream plus literal item and lineage indexes."""

    def __init__(self, *, source: str, tokens: tuple[AuditToken, ...],
                 items: tuple[LiteralItem, ...],
                 references: tuple[TemplateReference, ...],
                 source_id: Optional[str] = None,
                 round: Optional[int] = None,
                 fragment: Optional[str] = None,
                 window_start: int = 0,
                 window_end: Optional[int] = None,
                 fragment_map: Optional[FragmentMap] = None,
                 require_sql_literals: bool = True,
                 lexer: Optional[SqlJinjaLexer] = None):
        self.source = source
        self.tokens = tokens
        self.items = items
        self.references = references
        self.source_id = source_id
        self.round = round
        self.fragment = fragment
        self.window_start = window_start
        self.window_end = window_end if window_end is not None else window_start + len(source)
        self.fragment_map = fragment_map
        self.require_sql_literals = require_sql_literals
        self.lexer = lexer
        self.index = SegmentIndex(self)

    def __repr__(self):
        return (f'AuditResult(tokens={len(self.tokens)}, items={len(self.items)}, '
                f'round={self.round!r})')

    def __iter__(self) -> Iterator[AuditToken]:
        return iter(self.tokens)

    @property
    def segments(self) -> tuple[AuditToken, ...]:
        return self.tokens

    @property
    def token_stream(self) -> tuple[AuditToken, ...]:
        return self.tokens

    @property
    def audit_tokens(self) -> tuple[AuditToken, ...]:
        return self.tokens

    @property
    def literal_items(self) -> tuple[LiteralItem, ...]:
        return self.items

    @property
    def fixed_literals(self) -> tuple[LiteralItem, ...]:
        return self.items

    @property
    def audit_items(self) -> tuple[LiteralItem, ...]:
        return self.items

    @property
    def string_items(self) -> tuple[LiteralItem, ...]:
        return tuple(item for item in self.items if item.token_type in String)

    @property
    def numeric_items(self) -> tuple[LiteralItem, ...]:
        return tuple(item for item in self.items if item.token_type in Number)

    @property
    def lineage(self) -> dict[int, tuple[AuditToken, ...]]:
        return self.index.by_sql_word

    @property
    def sql_words(self) -> dict[int, tuple[AuditToken, ...]]:
        return self.index.by_sql_word

    @property
    def word_index(self) -> dict[int, tuple[AuditToken, ...]]:
        return self.index.by_sql_word

    @property
    def groups(self) -> tuple[tuple[AuditToken, ...], ...]:
        return self.index.groups()

    def group_of(self, segment_or_span) -> Optional[int]:
        return self.index.group_of(segment_or_span)

    def equivalences(self, segment_or_span=None):
        return self.index.equivalences(segment_or_span)

    def siblings(self, segment_or_span) -> tuple[AuditToken, ...]:
        return self.index.siblings(segment_or_span)

    @property
    def template_regions(self) -> tuple[TemplateReference, ...]:
        return self.references

    @property
    def unfinished_templates(self) -> tuple[TemplateReference, ...]:
        return tuple(ref for ref in self.references if ref.unfinished)

    @property
    def tokens_unprocessed(self) -> tuple[tuple[int, object, str], ...]:
        """The regular ``(index, token_type, value)`` triples."""
        return tuple((token.start, token.token_type, token.value)
                     for token in self.tokens)

    @property
    def token_spans(self) -> tuple[tuple[int, int, object, str], ...]:
        return tuple((token.start, token.end, token.token_type, token.value)
                     for token in self.tokens)

    def item(self, item_id) -> LiteralItem:
        if isinstance(item_id, LiteralItem):
            return item_id
        try:
            return self.index.by_item[int(item_id)]
        except (KeyError, TypeError, ValueError):
            for item in self.items:
                if item.uid == item_id:
                    return item
            raise KeyError(item_id)

    def items_for_round(self, round_no: Optional[int] = None) -> tuple[LiteralItem, ...]:
        if round_no is None:
            return self.items
        return tuple(item for item in self.items if item.round == round_no)

    def segments_for_sql(self, sql_index: int) -> tuple[AuditToken, ...]:
        return self.index.segments_for_sql(sql_index)

    def same_word(self, segment_or_span) -> tuple[AuditToken, ...]:
        return self.index.same_word(segment_or_span)

    def segments_for_template(self, template_index: int) -> tuple[AuditToken, ...]:
        return self.index.segments_for_template(template_index)

    def spans_for(self, item_id) -> tuple[SourceSpan, ...]:
        return self.item(item_id).source_spans

    def source_spans(self, item_id) -> tuple[SourceSpan, ...]:
        return self.spans_for(item_id)

    def original_spans(self, item_id) -> tuple[SpanRef, ...]:
        """All original (round, fragment, interval) pieces of an item."""
        item = self.item(item_id)
        if self.fragment_map is not None:
            rv = []
            for segment in item.segments:
                rv.extend(self.fragment_map.locate(
                    segment.start, segment.end, round_no=self.round))
            return tuple(rv)
        return tuple(
            SpanRef(round=self.round, fragment=self.fragment,
                    start=self.window_start + segment.start,
                    end=self.window_start + segment.end,
                    local_start=segment.start, local_end=segment.end)
            for segment in item.segments
        )

    def query(self, item_id):
        """Return the item together with all of its original intervals."""
        item = self.item(item_id)
        return item, self.original_spans(item)

    def fragments_for(self, item_id) -> tuple[SpanRef, ...]:
        return self.original_spans(item_id)

    def intervals_for(self, item_id) -> tuple[SourceSpan, ...]:
        return self.spans_for(item_id)

    def items_at(self, start: int, end: Optional[int] = None) -> tuple[LiteralItem, ...]:
        """All literal items whose source interval overlaps a window."""
        if end is None:
            end = start + 1
        return tuple(item for item in self.items
                     if item.start < end and start < item.end)

    def nearest_item(self, offset: int) -> Optional[LiteralItem]:
        """The item whose interval contains or is closest to ``offset``."""
        if not self.items:
            return None
        for item in self.items:
            if item.start <= offset < item.end:
                return item
        return min(self.items, key=lambda item: min(
            abs(offset - item.start), abs(offset - item.end)))

    def template_at(self, start: int) -> Optional[TemplateReference]:
        for reference in self.references:
            if reference.start <= start < reference.end:
                return reference
        return None

    def tokens_in(self, start: int, end: Optional[int] = None) -> tuple[AuditToken, ...]:
        if end is None:
            end = start
            start = 0
        return tuple(token for token in self.tokens
                     if token.start < end and start < token.end)

    def text_for(self, span: SourceSpan) -> str:
        return self.source[span.start:span.end]

    def audit_records(self) -> tuple[dict, ...]:
        """Compact audit records: fixed text plus separately addressable tags."""
        return tuple({
            'item_id': item.item_id,
            'uid': item.uid,
            'type': str(item.token_type),
            'fixed': item.text,
            'spans': [(span.start, span.end) for span in item.source_spans],
            'templates': [_reference_record(ref)
                          for ref in item.template_references],
        } for item in self.items)

    def as_dict(self, *, include_source: bool = False,
                absolute: bool = False) -> dict:
        def token_dict(token: AuditToken):
            record = token.record()
            if absolute:
                span = token.absolute_span
                record['start'], record['end'] = span.start, span.end
            return record

        def item_dict(item: LiteralItem):
            return {
                'item_id': item.item_id,
                'uid': item.uid,
                'sql_index': item.sql_index,
                'type': str(item.token_type),
                'text': item.text,
                'start': item.start,
                'end': item.end,
                'segments': [
                    {'start': s.start, 'end': s.end, 'value': s.value}
                    for s in item.segments
                ],
                'template_spans': [
                    {'start': ref.start, 'end': ref.end, 'text': ref.text,
                     'kind': ref.kind, 'variables': list(ref.variable_names)}
                    for ref in item.template_references
                ],
                'original_spans': [
                    {'round': ref.round, 'fragment': ref.fragment,
                     'start': ref.start, 'end': ref.end}
                    for ref in self.original_spans(item.item_id)
                ],
                'round': item.round,
                'fragment': item.fragment,
            }

        rv = {
            'source_id': self.source_id,
            'round': self.round,
            'fragment': self.fragment,
            'window': {'start': self.window_start, 'end': self.window_end},
            'tokens': [token_dict(token) for token in self.tokens],
            'items': [item_dict(item) for item in self.items],
            'template_regions': [_reference_record(ref)
                                 for ref in self.references],
        }
        if include_source:
            rv['source'] = self.source
        return rv


# ---------------------------------------------------------------------------
# Public convenience constructors
# ---------------------------------------------------------------------------

def audit_sql_template(text: str, **kwargs) -> AuditResult:
    """Audit one complete source slice with ``SqlJinjaLexer`` semantics."""
    return build_audit(text, **kwargs)


def audit_sql_jinja(text: str, **kwargs) -> AuditResult:
    return build_audit(text, **kwargs)


def audit(text: str, **kwargs) -> AuditResult:
    return build_audit(text, **kwargs)


lex_sql_template = audit_sql_template
lex_sql_jinja = audit_sql_jinja
tokenize = audit
tokenize_with_spans = audit


def literal_items(text: str, **kwargs) -> tuple[LiteralItem, ...]:
    return build_audit(text, **kwargs).items


def literals(text: str, **kwargs) -> tuple[LiteralItem, ...]:
    return literal_items(text, **kwargs)


def sequence_items(text: str, **kwargs) -> tuple[LiteralItem, ...]:
    """Literal items in source order, with equal texts kept separate."""
    return literal_items(text, **kwargs)


def audit_window(text: str, *, start: int = 0, end: Optional[int] = None,
                 round: Optional[int] = None,
                 fragment: Optional[str] = None,
                 window_start: Optional[int] = None,
                 **kwargs) -> AuditResult:
    """Audit only ``text[start:end]`` and retain its place in the draft.

    ``window_start`` overrides the absolute position; when omitted ``start``
    is used as the absolute origin of the window.
    """
    if end is not None:
        window = text[start:end]
    else:
        window = text[start:]
    origin = start if window_start is None else window_start
    return build_audit(window, round=round, fragment=fragment,
                       window_start=origin, **kwargs)


lex_window = audit_window


def _coerce_fragment(item, round_no: Optional[int]) -> FragmentSpec:
    if isinstance(item, FragmentSpec):
        if item.round is None and round_no is not None:
            return FragmentSpec(item.text, item.fragment, item.start,
                                round_no, item.end)
        return item
    if isinstance(item, str):
        return FragmentSpec(item, round=round_no)
    if isinstance(item, Mapping):
        return FragmentSpec(
            text=item.get('text', ''),
            fragment=item.get('fragment'),
            start=item.get('start', 0),
            round=item.get('round', round_no),
            end=item.get('end'),
        )
    if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
        return FragmentSpec(item[0], fragment=item[1], round=round_no)
    raise TypeError(f'cannot use {item!r} as a fragment')


def audit_fragments(fragments: Iterable, *, round: Optional[int] = None,
                    source_id: Optional[str] = None,
                    separator: str = '') -> AuditResult:
    """Audit the fragments of one round, joined only for that round.

    Adjacent fragments are joined without adding a separator by default, so a
    syntax construct may begin at an old fragment tail and finish at the next
    fragment head.
    """
    specs = tuple(_coerce_fragment(item, round) for item in fragments)
    text = (separator.join(spec.text for spec in specs) if separator
            else ''.join(spec.text for spec in specs))
    fragment_map = FragmentMap(specs, separator)
    return build_audit(text, round=round, source_id=source_id,
                       fragment_map=fragment_map)


def audit_history(rounds: Iterable, *, source_id: Optional[str] = None) -> 'AuditHistory':
    """Build a lazy :class:`AuditHistory` from round specifications."""
    history = AuditHistory(source_id=source_id)
    for spec in rounds:
        if isinstance(spec, Mapping):
            round_no = spec.get('round')
            fragments = spec.get('fragments', spec.get('text', ''))
        elif isinstance(spec, tuple) and len(spec) == 2:
            round_no, fragments = spec
        else:
            round_no = getattr(spec, 'round', None)
            fragments = getattr(spec, 'fragments', getattr(spec, 'text', ''))
        if isinstance(fragments, str):
            history.add_round(round_no, [fragments])
        else:
            history.add_round(round_no, fragments)
    return history


# ---------------------------------------------------------------------------
# History and round lookup
# ---------------------------------------------------------------------------

class AuditHistory:
    """Lazy collection of round fragments.

    A round is materialised only when requested.  Reusing the same
    :class:`FragmentSpec` object across many rounds does not create a complete
    token table per occurrence; the caller asks for one round (or one window)
    at a time.  :meth:`window` slices each fragment separately and only then
    joins the selected piece, so it never concatenates the whole draft.
    """

    def __init__(self, source_id: Optional[str] = None):
        self.source_id = source_id
        self._rounds: dict[Optional[int], tuple[FragmentSpec, ...]] = {}
        self._fragments: dict[str, FragmentSpec] = {}
        self._cache: dict[Optional[int], AuditResult] = {}
        self._text_cache: dict[Optional[int], str] = {}
        self._order: list[Optional[int]] = []

    def __repr__(self):
        return f'AuditHistory(rounds={self._order!r})'

    def __len__(self):
        return len(self._rounds)

    def __contains__(self, round_no):
        return round_no in self._rounds

    @property
    def rounds(self):
        return tuple(self._order)

    def define_fragment(self, name: str, text: str, *,
                        start: int = 0) -> FragmentSpec:
        """Register a reusable fragment that rounds may reference by name.

        A history file often repeats the same short fragment across many
        rounds.  Register it once, then name it in each round; the text is
        stored once and each round records which fragment it referenced.
        """
        spec = FragmentSpec(text, fragment=name, start=start)
        self._fragments[name] = spec
        return spec

    def fragment(self, name: str) -> FragmentSpec:
        return self._fragments[name]

    def fragments(self) -> dict[str, FragmentSpec]:
        return dict(self._fragments)

    def rounds_referencing(self, fragment_name: str) -> tuple[int, ...]:
        """Rounds whose fragment list mentions the named fragment."""
        return tuple(
            round_no for round_no in self._order
            if any(spec.fragment == fragment_name
                   for spec in self._rounds[round_no])
        )

    def _coerce_round_item(self, item, round_no):
        if isinstance(item, str) and item in self._fragments:
            spec = self._fragments[item]
            return FragmentSpec(spec.text, spec.fragment, spec.start,
                                round_no, spec.end)
        return _coerce_fragment(item, round_no)

    def add_round(self, round_no, fragments, *, replace: bool = False) -> None:
        specs = tuple(self._coerce_round_item(item, round_no)
                      for item in fragments) \
            if not isinstance(fragments, (str, FragmentSpec, Mapping)) \
            else (self._coerce_round_item(fragments, round_no),)
        if round_no in self._rounds and not replace:
            raise ValueError(f'round {round_no!r} already exists')
        if round_no not in self._rounds:
            self._order.append(round_no)
        self._rounds[round_no] = specs
        self.invalidate(round_no)

    def set_round(self, round_no, fragments) -> None:
        self.add_round(round_no, fragments, replace=True)

    def round_specs(self, round_no) -> tuple[FragmentSpec, ...]:
        return self._rounds[round_no]

    def round_length(self, round_no) -> int:
        if round_no in self._text_cache:
            return len(self._text_cache[round_no])
        return sum(len(spec.text) for spec in self._rounds[round_no])

    def audit_round(self, round_no) -> AuditResult:
        if round_no in self._cache:
            return self._cache[round_no]
        specs = self._rounds[round_no]
        text = ''.join(spec.text for spec in specs)
        result = build_audit(
            text, round=round_no, source_id=self.source_id,
            fragment_map=FragmentMap(specs))
        self._cache[round_no] = result
        self._text_cache[round_no] = text
        return result

    audit = audit_round
    lex_round = audit_round

    def text(self, round_no) -> str:
        """The joined text of one round, materialised only on demand."""
        if round_no not in self._text_cache:
            self._text_cache[round_no] = ''.join(
                spec.text for spec in self._rounds[round_no])
        return self._text_cache[round_no]

    def audit_spanning(self, rounds: Iterable, *,
                       include: Optional[Mapping] = None) -> AuditResult:
        """Audit the fragments of several rounds as one logical stream.

        Only the selected rounds' fragments are joined.  This is how an old
        fragment tail can share one syntax construct with the next fragment
        head without concatenating the entire historical draft.
        """
        specs = []
        for round_no in rounds:
            selected = self._rounds[round_no]
            if include is not None and round_no in include:
                wanted = include[round_no]
                if isinstance(wanted, str):
                    wanted = (wanted,)
                wanted = set(wanted)
                selected = tuple(spec for spec in selected
                                 if spec.fragment in wanted)
            specs.extend(selected)
        return audit_fragments(specs, source_id=self.source_id)

    def window(self, round_no, start: int = 0, end: Optional[int] = None) -> AuditResult:
        """Audit only a slice of a round, without building the whole round.

        ``start`` and ``end`` are offsets in the round's logical joined text.
        Each fragment is sliced independently and only the selected pieces are
        joined, so unchanged fragments outside the window are never copied
        into one large string.
        """
        specs = self._rounds[round_no]
        total = self.round_length(round_no)
        if end is None:
            end = total
        if start < 0:
            start = max(0, total + start)
        if end < 0:
            end = max(0, total + end)
        start = min(start, total)
        end = min(end, total)
        if end < start:
            start, end = end, start
        sliced_specs = []
        pieces = []
        cursor = 0
        for spec in specs:
            spec_end = cursor + len(spec.text)
            lo = max(start, cursor)
            hi = min(end, spec_end)
            if lo < hi:
                local_lo = lo - cursor
                local_hi = hi - cursor
                piece = spec.text[local_lo:local_hi]
                sliced_specs.append(FragmentSpec(
                    piece, spec.fragment, spec.start + local_lo, spec.round,
                    (spec.start + local_hi
                     if spec.end is not None else None)))
                pieces.append(piece)
            cursor = spec_end
            if cursor >= end:
                break
        return build_audit(''.join(pieces), round=round_no,
                           source_id=self.source_id,
                           fragment_map=FragmentMap(sliced_specs),
                           window_start=start)

    def slice(self, round_no, start: int = 0, end: Optional[int] = None) -> AuditResult:
        return self.window(round_no, start, end)

    def item_spans(self, round_no, item_id) -> tuple[SpanRef, ...]:
        return self.audit_round(round_no).original_spans(item_id)

    def spans_for_item(self, round_no, item_id) -> tuple[SpanRef, ...]:
        return self.item_spans(round_no, item_id)

    def query_item(self, round_no, item_id):
        return self.audit_round(round_no).query(item_id)

    def find_item(self, round_no, item_id) -> LiteralItem:
        return self.audit_round(round_no).item(item_id)

    def iter_rounds(self) -> Iterator[AuditResult]:
        for round_no in self._order:
            yield self.audit_round(round_no)

    def all_spans_for(self, item_id,
                      rounds: Optional[Iterable[int]] = None) -> tuple[SpanRef, ...]:
        """Look up the same item id across rounds and union its intervals."""
        wanted = self._order if rounds is None else list(rounds)
        rv = []
        for round_no in wanted:
            if round_no not in self._rounds:
                continue
            result = self.audit_round(round_no)
            try:
                rv.extend(result.original_spans(item_id))
            except KeyError:
                continue
        return tuple(rv)

    def locate(self, round_no, item_id) -> tuple[SpanRef, ...]:
        return self.item_spans(round_no, item_id)

    def find_by_uid(self, uid: str) -> Optional[LiteralItem]:
        """Find an item from its round-qualified uid (``'3:1'``)."""
        round_no, _, item_id = str(uid).partition(':')
        if not _:
            return None
        try:
            round_key = int(round_no)
        except ValueError:
            round_key = round_no
        if round_key not in self._rounds:
            return None
        try:
            return self.audit_round(round_key).item(int(item_id))
        except (KeyError, ValueError):
            return None

    def spans_for_uid(self, uid: str) -> tuple[SpanRef, ...]:
        """Return every original interval behind a round-qualified uid."""
        round_no, _, item_id = str(uid).partition(':')
        if not _:
            raise ValueError('uid must look like "<round>:<item>"')
        return self.item_spans(int(round_no), int(item_id))

    def query_uid(self, uid: str):
        round_no, _, item_id = str(uid).partition(':')
        return self.query_item(int(round_no), int(item_id))

    def items_by_text(self, round_no) -> dict[str, tuple[LiteralItem, ...]]:
        """Group one round's items by text without merging occurrences."""
        rv: dict[str, list[LiteralItem]] = {}
        for item in self.audit_round(round_no).items:
            rv.setdefault(item.text, []).append(item)
        return {text: tuple(items) for text, items in rv.items()}

    def clear_cache(self):
        self._cache.clear()

    def invalidate(self, round_no=None):
        if round_no is None:
            self._cache.clear()
        else:
            self._cache.pop(round_no, None)


# ---------------------------------------------------------------------------
# Correction sheets
# ---------------------------------------------------------------------------

def _template_opening_tokens(result: AuditResult) -> tuple[AuditToken, ...]:
    return tuple(
        token for token in result.tokens
        if token.part == 'template'
        and token.token_type in Comment.Preproc
        and token.value in ('{{', '{%')
    )


def _template_closing_tokens(result: AuditResult) -> tuple[AuditToken, ...]:
    return tuple(
        token for token in result.tokens
        if token.part == 'template'
        and token.token_type in Comment.Preproc
        and token.value in ('}}', '%}', '#}')
    )


@dataclass(frozen=True)
class Correction:
    """A replacement for one occurrence in a draft.

    Either specify an exact ``start``/``end`` span, or use ``occurrence`` with
    ``kind='template_close'`` to address the n-th template closing delimiter.
    ``round`` limits an occurrence search to one history round.  A correction
    never changes text outside its own span; re-lexing the edited source is
    what exposes any later token that the two layer reading reclassifies.
    """

    replacement: str
    start: Optional[int] = None
    end: Optional[int] = None
    occurrence: Optional[int] = None
    kind: str = 'span'
    round: Optional[int] = None
    fragment: Optional[str] = None
    source_id: Optional[str] = None

    def __post_init__(self):
        if self.start is None and self.occurrence is None:
            raise ValueError('correction needs a span or an occurrence')
        if self.start is not None and self.end is None:
            raise ValueError('a span correction needs both start and end')
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError('correction end must not precede its start')


def _resolve_correction(source: str, correction: Correction,
                        lexer: Optional[SqlJinjaLexer] = None,
                        round_no: Optional[int] = None) -> tuple[int, int, str]:
    if correction.start is not None:
        return correction.start, correction.end, correction.replacement
    result = build_audit(source, lexer=lexer, round=round_no)
    if correction.kind in ('template_close', 'closing', 'template_closing',
                           'close'):
        candidates = _template_closing_tokens(result)
    elif correction.kind in ('template', 'template_region', 'region'):
        candidates = tuple(
            AuditToken(ref.start, ref.end, Token, ref.text, part='template',
                       template_index=ref.index)
            for ref in result.references)
    elif correction.kind in ('template_open', 'open', 'opening'):
        candidates = tuple(
            token for token in result.tokens
            if token.part == 'template'
            and token.token_type in Comment.Preproc
            and token.value in ('{{', '{%'))
    elif correction.kind in ('literal', 'item'):
        candidates = tuple(
            AuditToken(item.start, item.end, item.token_type, item.text,
                       part='sql', sql_index=item.sql_index)
            for item in result.items)
    elif correction.kind in ('token', 'tokens'):
        candidates = result.tokens
    else:
        raise ValueError(f'unknown correction kind {correction.kind!r}')
    index = correction.occurrence
    if index is None or index < 0 or index >= len(candidates):
        raise IndexError(
            f'{correction.kind} occurrence {index!r} out of range '
            f'(found {len(candidates)})')
    chosen = candidates[index]
    return chosen.start, chosen.end, correction.replacement


def apply_correction(source: str, correction: Correction,
                     *, lexer: Optional[SqlJinjaLexer] = None,
                     audit_after: bool = True,
                     round: Optional[int] = None,
                     source_id: Optional[str] = None):
    """Apply one correction and re-lex the edited source.

    Re-lexing is deliberate: changing a template closing can move the boundary
    between the template and SQL layers, so unchanged characters after the
    edit may still receive a different classification.
    """
    round_no = correction.round if correction.round is not None else round
    start, end, replacement = _resolve_correction(
        source, correction, lexer, round_no)
    new_source = source[:start] + replacement + source[end:]
    if not audit_after:
        return new_source
    return new_source, build_audit(
        new_source, lexer=lexer, round=round_no,
        source_id=correction.source_id or source_id)


def apply_corrections(source: str, corrections: Iterable[Correction],
                      *, lexer: Optional[SqlJinjaLexer] = None,
                      audit_after: bool = True,
                      round: Optional[int] = None,
                      source_id: Optional[str] = None):
    """Apply a correction sheet from right to left and re-lex once at the end.

    Each correction is resolved against the *current* source, then applied in
    descending source order.  ``audit_after`` returns the edited source along
    with one fresh audit of it.
    """
    corrections = list(corrections)
    resolved = []
    for correction in corrections:
        round_no = correction.round if correction.round is not None else round
        resolved.append(_resolve_correction(source, correction, lexer, round_no))
    ordered = sorted(resolved)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError(
                f'corrections overlap: {previous[0]}:{previous[1]} and '
                f'{current[0]}:{current[1]}')
    for start, end, replacement in reversed(ordered):
        source = source[:start] + replacement + source[end:]
    if not audit_after:
        return source
    return source, build_audit(source, lexer=lexer, round=round,
                               source_id=source_id)


def apply_correction_window(source: str, correction: Correction, *,
                            context: int = 32,
                            lexer: Optional[SqlJinjaLexer] = None):
    """Apply a correction and audit only a small window around it.

    This is the audit-friendly form: the whole edited draft is not retained in
    a second audit object, only ``context`` characters on either side of the
    replaced span are re-lexed.
    """
    start, end, replacement = _resolve_correction(source, correction, lexer)
    new_source = source[:start] + replacement + source[end:]
    window_start = max(0, start - context)
    window_end = min(len(new_source), start + len(replacement) + context)
    result = build_audit(new_source[window_start:window_end],
                         lexer=lexer, window_start=window_start)
    return new_source, result


def correction_sheet(source: str, corrections: Iterable[Correction], *,
                     lexer: Optional[SqlJinjaLexer] = None,
                     context: Optional[int] = None,
                     round: Optional[int] = None,
                     source_id: Optional[str] = None):
    """Apply a whole sheet, optionally auditing only one narrow window."""
    corrections = list(corrections)
    if context is None:
        return apply_corrections(source, corrections, lexer=lexer,
                                 round=round, source_id=source_id)
    resolved = []
    for correction in corrections:
        round_no = correction.round if correction.round is not None else round
        resolved.append(_resolve_correction(source, correction, lexer, round_no))
    ordered = sorted(resolved)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError(
                f'corrections overlap: {previous[0]}:{previous[1]} and '
                f'{current[0]}:{current[1]}')
    if not resolved:
        window_start = 0
    else:
        window_start = max(0, min(item[0] for item in resolved) - context)
    for start, end, replacement in reversed(ordered):
        source = source[:start] + replacement + source[end:]
    original_end = max((item[1] for item in resolved), default=0)
    delta = sum(len(item[2]) - (item[1] - item[0]) for item in resolved)
    window_end = min(len(source), original_end + delta + context)
    result = build_audit(source[window_start:window_end], lexer=lexer,
                         round=round, source_id=source_id,
                         window_start=window_start)
    return source, result


# ---------------------------------------------------------------------------
# Diff of audit items
# ---------------------------------------------------------------------------

def _item_signature(item: LiteralItem):
    return item.token_type, item.text


def _distance(left: LiteralItem, right: LiteralItem) -> int:
    return abs(left.start - right.start) + abs(left.end - right.end)


def compare_audits(old: AuditResult, new: AuditResult, *,
                   by: str = 'occurrence') -> AuditDiff:
    """Compare literal items of two audits without merging equal texts.

    By default, matching is by occurrence and source order within each token
    type, not by text alone, so two identical literal texts remain two
    auditable occurrences.  Replacing one occurrence's text produces one
    ``changed`` entry rather than a removed/added pair; insertions and
    deletions shift only the remaining occurrences.

    With ``by='text'``, items are matched first by their full fixed text.
    Equal texts still produce separate item entries when they occur more than
    once; this mode is useful when a correction reorders lines but the actual
    fixed strings are unchanged.
    """
    if by not in ('occurrence', 'text'):
        raise ValueError("by must be 'occurrence' or 'text'")
    old_groups: dict[object, list[int]] = {}
    new_groups: dict[object, list[int]] = {}
    for index, item in enumerate(old.items):
        key = (item.token_type, item.text) if by == 'text' else item.token_type
        old_groups.setdefault(key, []).append(index)
    for index, item in enumerate(new.items):
        key = (item.token_type, item.text) if by == 'text' else item.token_type
        new_groups.setdefault(key, []).append(index)

    matches: list[tuple[int, int, str]] = []
    for key in old_groups:
        old_indices = old_groups.get(key, [])
        new_indices = new_groups.get(key, [])
        if not old_indices or not new_indices:
            continue
        old_texts = [old.items[index].text for index in old_indices]
        new_texts = [new.items[index].text for index in new_indices]
        matcher = SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
        for tag, old_lo, old_hi, new_lo, new_hi in matcher.get_opcodes():
            if tag == 'equal':
                for offset in range(old_hi - old_lo):
                    matches.append((old_indices[old_lo + offset],
                                    new_indices[new_lo + offset], 'unchanged'))
            elif tag == 'replace':
                paired = min(old_hi - old_lo, new_hi - new_lo)
                for offset in range(paired):
                    matches.append((old_indices[old_lo + offset],
                                    new_indices[new_lo + offset], 'changed'))
            # insert/delete are handled from the unmatched sets below

    matched_old = {old_index for old_index, _new_index, _kind in matches}
    matched_new = {new_index for _old_index, new_index, _kind in matches}
    changes: list[ItemChange] = []
    for old_index, new_index, kind in matches:
        changes.append(ItemChange(kind, old.items[old_index],
                                  new.items[new_index]))
    for old_index, old_item in enumerate(old.items):
        if old_index not in matched_old:
            changes.append(ItemChange('removed', old_item, None))
    for new_index, new_item in enumerate(new.items):
        if new_index not in matched_new:
            changes.append(ItemChange('added', None, new_item))
    changes.sort(key=lambda change: (
        change.new.start if change.new is not None else change.old.start,
        change.new.item_id if change.new is not None else change.old.item_id,
    ))
    return AuditDiff(old, new, tuple(changes))


diff_audits = compare_audits
diff = compare_audits
compare = compare_audits


def _classification_map(result: AuditResult):
    """Per-character ``(token_type, part)`` classification."""
    types = [None] * len(result.source)
    for token in result.tokens:
        for index in range(token.start, min(token.end, len(types))):
            types[index] = (token.token_type, token.part)
    return types


def compare_classification(old: AuditResult, new: AuditResult,
                           *, old_offset: int = 0, new_offset: int = 0
                           ) -> tuple[ClassificationChange, ...]:
    """Report unchanged characters that moved between token classes.

    Unlike :func:`compare_tokens`, this aligns by source characters, so it
    still catches a later comment that the correction splits into several
    template tokens even though no token value is identical on both sides.
    Only characters present unchanged in both drafts are considered; inserted
    or deleted text is not a reclassification.
    """
    if not old.source or not new.source:
        return ()
    if old.source == new.source:
        opcodes = [('equal', 0, len(old.source), 0, len(new.source))]
    else:
        matcher = SequenceMatcher(a=old.source, b=new.source, autojunk=False)
        opcodes = matcher.get_opcodes()
    old_types = _classification_map(old)
    new_types = _classification_map(new)
    changes = []
    for tag, old_lo, old_hi, new_lo, new_hi in opcodes:
        if tag != 'equal':
            continue
        cursor = 0
        length = min(old_hi - old_lo, new_hi - new_lo)
        while cursor < length:
            old_position = old_lo + cursor
            new_position = new_lo + cursor
            old_value = old_types[old_position]
            new_value = new_types[new_position]
            if old_value == new_value:
                cursor += 1
                continue
            run_end = cursor + 1
            while run_end < length:
                if (old_types[old_lo + run_end] != old_value
                        or new_types[new_lo + run_end] != new_value):
                    break
                run_end += 1
            old_span = SourceSpan(old_position + old_offset,
                                  old_lo + run_end + old_offset)
            new_span = SourceSpan(new_position + new_offset,
                                  new_lo + run_end + new_offset)
            old_found = []
            new_found = []
            for position in range(old_lo + cursor, old_lo + run_end):
                value = old_types[position]
                if value is not None and value not in old_found:
                    old_found.append(value)
            for position in range(new_lo + cursor, new_lo + run_end):
                value = new_types[position]
                if value is not None and value not in new_found:
                    new_found.append(value)
            changes.append(ClassificationChange(
                old_span=old_span,
                new_span=new_span,
                text=old.source[old_lo + cursor:old_lo + run_end],
                old_types=tuple(old_found),
                new_types=tuple(new_found),
            ))
            cursor = run_end
    return tuple(changes)


def compare_tokens(old: AuditResult, new: AuditResult, *,
                   offset: int = 0) -> tuple[TokenChange, ...]:
    """Compare the visible token streams, including reclassification.

    Tokens whose text did not change but whose template/SQL classification did
    are reported as ``reclassified``.  This is the check needed after a
    correction sheet replaces one template closing: characters later in the
    source may be unchanged and still move between the two layers.

    ``offset`` shifts old token positions before comparison, for the common
    case of comparing a windowed re-read against the same region of the old
    draft.
    """
    old_tokens = old.tokens
    new_tokens = new.tokens
    if offset:
        old_tokens = tuple(
            AuditToken(
                start=token.start + offset, end=token.end + offset,
                token_type=token.token_type, value=token.value,
                part=token.part, sql_index=token.sql_index,
                segment_index=token.segment_index,
                template_index=token.template_index, round=token.round,
                fragment=token.fragment,
                source_start=(token.source_start + offset
                              if token.source_start is not None else None),
                source_end=(token.source_end + offset
                            if token.source_end is not None else None),
            )
            for token in old_tokens
        )
    old_values = [token.value for token in old_tokens]
    new_values = [token.value for token in new_tokens]
    matcher = SequenceMatcher(a=old_values, b=new_values, autojunk=False)
    changes: list[TokenChange] = []
    for tag, old_lo, old_hi, new_lo, new_hi in matcher.get_opcodes():
        if tag == 'equal':
            for offset in range(old_hi - old_lo):
                old_token = old_tokens[old_lo + offset]
                new_token = new_tokens[new_lo + offset]
                kind = ('unchanged'
                        if (old_token.token_type == new_token.token_type
                            and old_token.part == new_token.part)
                        else 'reclassified')
                changes.append(TokenChange(kind, old_token, new_token))
        elif tag == 'replace':
            paired = min(old_hi - old_lo, new_hi - new_lo)
            for offset in range(paired):
                changes.append(TokenChange(
                    'changed', old_tokens[old_lo + offset],
                    new_tokens[new_lo + offset]))
            for index in range(old_lo + paired, old_hi):
                changes.append(TokenChange('removed', old_tokens[index], None))
            for index in range(new_lo + paired, new_hi):
                changes.append(TokenChange('added', None, new_tokens[index]))
        elif tag == 'delete':
            for index in range(old_lo, old_hi):
                changes.append(TokenChange('removed', old_tokens[index], None))
        elif tag == 'insert':
            for index in range(new_lo, new_hi):
                changes.append(TokenChange('added', None, new_tokens[index]))
    return tuple(changes)


def reclassified_tokens(old: AuditResult, new: AuditResult) -> tuple[TokenChange, ...]:
    return tuple(change for change in compare_tokens(old, new)
                 if change.kind == 'reclassified')


def compare_sources(old_source: str, new_source: str, **kwargs) -> AuditDiff:
    """Audit and compare two complete source slices."""
    return compare_audits(build_audit(old_source, **kwargs),
                          build_audit(new_source, **kwargs))


compare_text = compare_sources
