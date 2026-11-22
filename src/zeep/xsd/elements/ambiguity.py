"""Backtracking parser for ambiguous ``xsd:choice`` declarations.

A single (``maxOccurs="1"``) ``xsd:choice`` nested inside an
``xsd:sequence`` is flattened away when the sequence exposes its elements.
That means parsing can no longer tell which branch of the choice produced a
given element.  When the branches share element prefixes this turns into a
greedy commit: the first matching branch is selected even when the rest of
the element sequence (e.g. a required sibling after the choice) can only be
satisfied by another branch.

The resolver re-introduces the choice/sequence structure for parsing and
performs a depth first search over the possible parses.  Every branch of a
single choice is tried against a *copy* of the remaining elements and the
rest of the enclosing sequence is parsed for every branch.  The complete
parses are then ranked: a parse which leaves elements unconsumed (because a
required sibling could no longer be satisfied) loses against one which
consumes the whole sequence.  Only when several distinct parses still match
the complete element sequence is the message considered ambiguous; every
surviving candidate is returned under :data:`AMBIGUOUS_KEY`.
"""

from collections import OrderedDict, deque

from lxml import etree

from zeep.exceptions import UnexpectedElementError
from zeep.xsd.elements import any as _any_module
from zeep.xsd.elements import element as _element_module
from zeep.xsd.elements import indicators as _indicators

AMBIGUOUS_KEY = "_ambiguous_choices"


def _Element():
    return _element_module.Element


def _Any():
    return _any_module.Any


def _Choice():
    return _indicators.Choice


def _Sequence():
    return _indicators.Sequence


def _All():
    return _indicators.All


def _Group():
    return _indicators.Group


def merge_parsed(target, extra):
    """Merge a parsed sub-result into the accumulated result.

    Duplicate element names are produced by the flattened representation
    (``item_2`` next to ``item_2__1``).  Scalar values are kept on first
    write while lists are extended, mirroring how the flattened parser
    accumulates repeated elements.
    """
    for key, value in extra.items():
        if key in target:
            current = target[key]
            if isinstance(current, list) and isinstance(value, list):
                current.extend(value)
            elif current is None:
                target[key] = value
        else:
            target[key] = value


def contains_single_choice(indicator):
    """Return whether the indicator tree contains a single ``xsd:choice``.

    Only choices with ``maxOccurs == 1`` can be resolved by backtracking.
    Unbounded choices keep their existing greedy parser.
    """
    if isinstance(indicator, _Choice()):
        return not indicator.accepts_multiple
    for child in indicator:
        if isinstance(child, (_Choice(), _Sequence(), _All(), _Group())):
            if contains_single_choice(child):
                return True
    return False


def element_flat_name(root_sequence, element):
    """Return the (possibly suffixed) flattened name of an element.

    The flattened sequence uses :class:`~zeep.xsd.utils.UniqueNameGenerator`
    to disambiguate duplicate names (``item_2`` -> ``item_2__1``).  Element
    objects are shared between the nested indicators and the flattened
    sequence, so identity lookup returns the exact key the value object
    expects.
    """
    for name, candidate in root_sequence.elements:
        if candidate is element:
            return name
    return element.attr_name


def _first_tag(xmlelements):
    if not xmlelements:
        return None
    return etree.QName(xmlelements[0].tag)


def _can_start_element(element, tag, schema):
    if tag is None:
        return False
    if tag.namespace and element.qname.namespace:
        if tag.namespace != element.qname.namespace and schema.settings.strict:
            return False
    return tag.localname == element.qname.localname


def _can_be_empty(element):
    """Return whether the node can legally produce zero XML elements.

    A required sequence which only contains optional children is itself
    empty-able, so the check recurses into sequence/all/group children.
    """
    if element.is_optional:
        return True
    if isinstance(element, (_Choice(),)):
        # Choice.is_optional is always True, kept explicit for clarity.
        return True
    if isinstance(element, _Sequence()):
        return all(_can_be_empty(child) for child in element)
    if isinstance(element, _All()):
        return all(_can_be_empty(child) for child in element)
    if isinstance(element, _Group()):
        return _can_be_empty(element.child)
    return False


def _count_required(element):
    """Count required leaf elements the node still expects."""
    if _can_be_empty(element):
        return 0
    if isinstance(element, (_Sequence(), _All())):
        return sum(_count_required(child) for child in element)
    if isinstance(element, _Group()):
        return _count_required(element.child)
    # A required element (or other non-empty leaf) counts as one missing slot.
    return 1


def _combine(left_result, right_result):
    merged = OrderedDict(left_result)
    merge_parsed(merged, right_result)
    return merged


def _freeze(value):
    """Make a parsed result hashable so outcomes can be deduplicated."""
    if isinstance(value, dict):
        return tuple((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


class _Outcome:
    """One complete way of parsing (part of) an indicator tree."""

    __slots__ = ("result", "leftover", "missing")

    def __init__(self, result, leftover, missing=0):
        self.result = result
        # A private copy so sibling backtracking attempts cannot mutate it.
        self.leftover = deque(leftover)
        # Number of required elements which could not be filled because the
        # input ran out.  Used only to rank otherwise equivalent parses; the
        # legacy parser tolerates these (yielding None) rather than failing.
        self.missing = missing


class SequenceResolver:
    """Parse a sequence while keeping its nested choice structure intact."""

    def __init__(self, sequence, schema, context, root_sequence=None):
        self.sequence = sequence
        self.schema = schema
        self.context = context
        # The outermost sequence owns the flattened key space, so every leaf
        # element resolves its suffixed name against that sequence.
        self.root_sequence = root_sequence or sequence

    def resolve(self, xmlelements):
        """Return ``(consumed, result, candidates, leftover)``.

        ``candidates`` contains the distinct results of the parses which
        consume the most elements without leaving a required sibling behind.
        """
        initial = len(xmlelements)
        outcomes = self._parse_sequence(self.sequence, deque(xmlelements))

        if not outcomes:
            return 0, OrderedDict(), [], deque(xmlelements)

        # Rank parses first by the number of unconsumed elements (a parse
        # which leaves a required sibling unsatisfiable loses) and then by how
        # many required slots were left empty because the input ran out.
        outcomes.sort(key=lambda outcome: (len(outcome.leftover), outcome.missing))
        best_key = (len(outcomes[0].leftover), outcomes[0].missing)
        winners = [
            outcome
            for outcome in outcomes
            if (len(outcome.leftover), outcome.missing) == best_key
        ]

        # Deduplicate identical results while preserving order.
        distinct = []
        seen = set()
        for outcome in winners:
            signature = _freeze(outcome.result)
            if signature in seen:
                continue
            seen.add(signature)
            distinct.append(outcome)

        # The primary result is one complete, valid parse (the first winner).
        # Merging every winner would combine keys which no single branch can
        # produce together; the alternative parses are exposed as candidates
        # instead.
        primary = distinct[0]
        result = OrderedDict(primary.result)
        candidates = [OrderedDict(outcome.result) for outcome in distinct]

        consumed = initial - len(primary.leftover)
        leftover = primary.leftover
        return consumed, result, candidates, leftover

    def _parse_sequence(self, sequence, xmlelements):
        """Return every outcome of parsing a sequence from its first child."""
        nested = sequence.elements_nested
        return self._parse_children(sequence, nested, 0, xmlelements)

    def _parse_children(self, sequence, nested, index, xmlelements):
        if index >= len(nested):
            return [_Outcome(OrderedDict(), deque(xmlelements))]

        if not xmlelements:
            # The input ran out.  Empty-able children are skipped; required
            # ones are counted as missing rather than rejecting the whole
            # parse, matching the legacy parser which yields None for them.
            missing = 0
            for _unused in range(index, len(nested)):
                _remaining_name, remaining = nested[_unused]
                missing += _count_required(remaining)
            return [_Outcome(OrderedDict(), deque(xmlelements), missing)]

        _name, element = nested[index]
        head_outcomes = self._parse_indicator(element, _name, xmlelements)

        outcomes = []
        for head in head_outcomes:
            tails = self._parse_children(
                sequence, nested, index + 1, head.leftover
            )
            for tail in tails:
                outcomes.append(
                    _Outcome(
                        _combine(head.result, tail.result),
                        deque(tail.leftover),
                        head.missing + tail.missing,
                    )
                )
        return outcomes

    def _parse_indicator(self, element, name, xmlelements):
        """Return the possible outcomes of parsing one indicator node."""
        if isinstance(element, _Element()):
            return self._parse_element(element, xmlelements)

        if isinstance(element, _Any()):
            return self._parse_any(element, name, xmlelements)

        if isinstance(element, _Choice()):
            if not element.accepts_multiple:
                return self._parse_single_choice(element, xmlelements)
            return self._parse_legacy(element, name, xmlelements)

        if isinstance(element, _Sequence()):
            if not element.accepts_multiple:
                return self._parse_sequence(element, xmlelements)
            return self._parse_legacy(element, name, xmlelements)

        # All, Group and unbounded indicators keep their legacy parser.
        return self._parse_legacy(element, name, xmlelements)

    def _parse_element(self, element, xmlelements):
        tag = _first_tag(xmlelements)
        if not _can_start_element(element, tag, self.schema):
            # A required element which cannot start invalidates this parse
            # path; an optional one is simply skipped.
            if element.is_optional:
                return [_Outcome(OrderedDict(), deque(xmlelements))]
            return []

        local = deque(xmlelements)
        before = len(local)
        try:
            sub_result = element.parse_xmlelements(
                local, self.schema, name=None, context=self.context
            )
        except UnexpectedElementError:
            return []
        consumed = before - len(local)
        if not consumed:
            if element.is_optional:
                return [_Outcome(OrderedDict(), deque(xmlelements))]
            return []

        key = element_flat_name(self.root_sequence, element)
        return [_Outcome(OrderedDict([(key, sub_result)]), deque(local))]

    def _parse_any(self, element, name, xmlelements):
        local = deque(xmlelements)
        before = len(local)
        sub_result = element.parse_xmlelements(
            local, self.schema, name=name, context=self.context
        )
        consumed = before - len(local)
        if not consumed:
            return [_Outcome(OrderedDict(), deque(xmlelements))]
        # Any has no attr_name; the generated nested name is the value key.
        key = name or getattr(element, "attr_name", None)
        if key is None:
            return [_Outcome(OrderedDict(), deque(local))]
        return [
            _Outcome(OrderedDict([(key, sub_result)]), deque(local))
        ]

    def _parse_single_choice(self, choice, xmlelements):
        outcomes = []
        for name, branch in choice.elements_nested:
            outcomes.extend(self._parse_indicator(branch, name, deque(xmlelements)))

        # An optional choice (minOccurs=0) which matches nothing is simply
        # skipped, matching the legacy Choice parser.
        if not outcomes and choice.is_optional:
            return [_Outcome(OrderedDict(), deque(xmlelements))]
        return outcomes

    def _parse_legacy(self, element, name, xmlelements):
        local = deque(xmlelements)
        before = len(local)
        try:
            sub_result = element.parse_xmlelements(
                local, self.schema, name=name, context=self.context
            )
        except UnexpectedElementError:
            return []
        consumed = before - len(local)
        if not consumed:
            return [_Outcome(OrderedDict(), deque(xmlelements))]
        if not isinstance(sub_result, dict):
            sub_result = {}
        return [_Outcome(OrderedDict(sub_result), deque(local))]
