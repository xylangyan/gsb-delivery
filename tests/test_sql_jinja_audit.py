"""
    Tests for the SQL+Jinja source audit helpers
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    :copyright: Copyright 2006-present by the Pygments team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

import pytest

import solution
from pygments.lexers.sql_jinja_audit import (
    AuditHistory,
    ClassificationChange,
    Correction,
    FragmentSpec,
    apply_correction,
    apply_correction_window,
    apply_corrections,
    audit,
    audit_fragments,
    audit_history,
    audit_window,
    compare_audits,
    compare_classification,
    compare_tokens,
    correction_sheet,
    reclassified_tokens,
)
from pygments.lexers.templates import SqlJinjaLexer
from pygments.token import Comment, Literal, Name, String, Whitespace


SAMPLE = "SELECT 'receipt_{{ depot }}_daily' AS x"


def test_tokens_match_project_lexer_exactly():
    lexer = SqlJinjaLexer(stripnl=False, ensurenl=False)
    expected = [(token_type, value)
                for _index, token_type, value
                in lexer.get_tokens_unprocessed(SAMPLE)]
    result = audit(SAMPLE)
    assert [(token.token_type, token.value) for token in result.tokens] == expected
    for token in result.tokens:
        assert SAMPLE[token.start:token.end] == token.value


def test_literal_fragments_share_one_sql_word_identity():
    result = audit(SAMPLE)
    item = result.items[0]
    assert item.text == "'receipt__daily'"
    assert item.fixed_text == "'receipt__daily'"
    assert item.source_spans == ((7, 16), (27, 34))
    assert len(item.segments) == 2
    assert {segment.sql_index for segment in item.segments} == {item.sql_index}
    assert [segment.segment_index for segment in item.segments] == [0, 1]
    assert result.index.same_word(item.segments[0]) == item.segments
    assert result.group_of(item.segments[1]) == item.sql_index


def test_inserted_template_text_is_not_part_of_the_literal_item():
    result = audit(SAMPLE)
    item = result.items[0]
    assert 'depot' not in item.text
    assert '{{' not in item.text
    assert item.template_text == '{{ depot }}'
    assert [ref.variable_names for ref in item.template_references] == [('depot',)]
    assert item.template_spans == ((16, 27),)
    # The variable itself is still individually addressable.
    reference = result.references[0]
    assert (reference.start, reference.end) == (16, 27)
    assert result.segments_for_template(reference.index)
    assert result.template_at(19) is reference


def test_same_literal_text_remains_two_occurrences():
    source = "SELECT 'same', 'same'"
    result = audit(source)
    assert [item.text for item in result.items] == ["'same'", "'same'"]
    assert len({item.item_id for item in result.items}) == 2
    assert result.items[0].source_spans != result.items[1].source_spans


def test_two_split_literals_with_the_same_core_are_still_two_items():
    source = "SELECT 'a{{x}}b', 'a{{x}}b'"
    result = audit(source)
    assert len(result.items) == 2
    assert result.items[0].text == result.items[1].text == "'ab'"
    assert result.items[0].source_spans != result.items[1].source_spans


def test_unfinished_template_keeps_the_visible_classification():
    source = "SELECT 'a{{ x }}b"
    result = audit(source)
    assert [(token.token_type, token.value) for token in result.tokens]
    assert result.items == ()
    assert any(token.token_type is __import__(
        'pygments.token', fromlist=['Error']).Error
        for token in result.tokens)


def test_audit_does_not_change_offsets_when_a_template_is_unclosed():
    source = "SELECT 'a{{ x %}b' -- note {{ y }}"
    lexer = SqlJinjaLexer(stripnl=False, ensurenl=False)
    expected = [(token_type, value)
                for _index, token_type, value
                in lexer.get_tokens_unprocessed(source)]
    result = audit(source)
    assert [(token.token_type, token.value) for token in result.tokens] == expected
    for token in result.tokens:
        assert source[token.start:token.end] == token.value


def test_window_offsets_are_relative_to_the_slice_and_absolute_on_demand():
    source = "SELECT 1\n" + SAMPLE + "\nSELECT 2"
    start = source.index("'receipt_")
    window = audit_window(source, start=start, end=start + 27)
    item = window.items[0]
    assert window.source[item.start:item.end] != item.text
    absolute = item.absolute_span
    assert source[absolute.start:absolute.end].startswith("'receipt_")
    assert (absolute.start, absolute.end) == (item.start + start,
                                              item.end + start)


def test_fragment_index_joins_an_old_tail_to_the_next_fragment_head():
    fragments = [
        FragmentSpec("SELECT 'abc", fragment='r1-a', start=100, round=1),
        FragmentSpec("def' AS x", fragment='r1-b', start=500, round=1),
    ]
    result = audit_fragments(fragments)
    item = result.items[0]
    assert item.text == "'abcdef'"
    assert [(ref.fragment, ref.round, ref.start, ref.end)
            for ref in result.original_spans(item.item_id)] == [
        ('r1-a', 1, 107, 111),
        ('r1-b', 1, 500, 504),
    ]


def test_fragment_index_can_span_two_rounds_for_one_real_word():
    fragments = [
        FragmentSpec("SELECT 'abc", fragment='a', start=0, round=3),
        FragmentSpec("def' AS x", fragment='b', start=0, round=4),
    ]
    result = audit_fragments(fragments)
    spans = result.original_spans(result.items[0].item_id)
    assert [ref.round for ref in spans] == [3, 4]


def test_history_window_does_not_require_materialising_other_rounds():
    history = AuditHistory()
    history.add_round(1, [FragmentSpec("SELECT 'abc", 'f1', 0)])
    history.add_round(2, [FragmentSpec("def' AS x", 'f2', 0)])
    history.set_round(3, [FragmentSpec("SELECT 1", 'f3', 0)])
    assert history._cache == {}
    result = history.window(1, 7, 100)
    assert result.source == "'abc"
    assert history._cache == {}


def test_history_round_lookup_returns_all_original_intervals():
    history = audit_history([
        (1, [FragmentSpec("SELECT 'a{{x}}", fragment='a')]),
        (2, [FragmentSpec("b' AS x", fragment='b')]),
        (3, [FragmentSpec("SELECT 'a{{x}}b'", fragment='c')]),
    ])
    assert history.audit_round(1).items == ()
    assert history.audit_round(2).items == ()
    item = history.find_item(3, 0)
    assert item.text == "'ab'"
    spans = history.all_spans_for(0)
    assert [(ref.round, ref.fragment) for ref in spans] == [(3, 'c'), (3, 'c')]


def test_history_reuses_the_same_short_fragment_across_rounds():
    fragment = FragmentSpec("SELECT 'a{{x}}b'", fragment='shared')
    history = audit_history([(round_no, [fragment])
                             for round_no in range(1, 40)])
    assert len(history) == 39
    assert history.audit_round(1).items[0].text == "'ab'"
    assert history.audit_round(39).items[0].text == "'ab'"
    assert list(history._cache) == [1, 39]


def test_diff_keeps_equal_texts_as_separate_occurrences():
    old = audit("SELECT 'a', 'a'")
    new = audit("SELECT 'a', 'b'")
    diff = compare_audits(old, new)
    assert [change.kind for change in diff.changes] == ['unchanged', 'changed']
    assert diff.changes[1].old.item_id == 1
    assert diff.changes[1].new.item_id == 1


def test_diff_detects_insertion_without_matching_by_text():
    old = audit("SELECT 'a'")
    new = audit("SELECT 'a', 'a'")
    diff = compare_audits(old, new)
    assert [change.kind for change in diff.changes] == ['unchanged', 'added']


def test_correction_replaces_only_the_selected_template_closing():
    source = "SELECT 1 -- here {{ x }}\nSELECT 2 -- and {{ y }}"
    corrected, result = apply_correction(
        source, Correction('%}', occurrence=0, kind='template_close'))
    assert corrected.count('{{') == 2
    assert corrected.startswith("SELECT 1 -- here {{ x %}\n")
    assert corrected.endswith('-- and {{ y }}')
    assert result.source == corrected


def test_correction_can_reclassify_unchanged_later_characters():
    source = "SELECT 'a{{x}}b' -- note {{ y }}"
    corrected, after = apply_correction(
        source, Correction('%}', occurrence=0, kind='template_close'))
    assert corrected == "SELECT 'a{{x%}b' -- note {{ y }}"
    before = audit(source)
    reclassified = reclassified_tokens(before, after)
    assert reclassified
    assert all(change.same_text for change in reclassified)
    assert any(change.old.part == 'sql' and change.new.part == 'template'
               for change in reclassified)


def test_correction_without_changing_later_source_still_reclassifies():
    old = "SELECT 1 -- tail {{ x }}\nSELECT 2"
    new = "SELECT 1 -- tail {{ x %}\nSELECT 2"
    old_result = audit(old)
    new_result = audit(new)
    changes = [change for change in compare_tokens(old_result, new_result)
               if change.kind == 'reclassified']
    assert any(change.old.value == '\n' for change in changes)
    # The later SELECT characters are unchanged but may now be template text.
    assert any(change.old.value == 'SELECT' and change.new.part == 'template'
               for change in changes)


def test_windowed_correction_returns_only_a_small_audit():
    source = "SELECT 1 -- here {{ x }}\nSELECT 2"
    corrected, result = apply_correction_window(
        source, Correction('%}', occurrence=0, kind='template_close'),
        context=4)
    assert corrected.startswith('SELECT 1 -- here {{ x %}')
    assert result.window_end - result.window_start <= 20
    assert result.source == corrected[result.window_start:result.window_end]


def test_correction_sheet_applies_independent_edits():
    source = "SELECT '{{a}}' AS a, '{{b}}' AS b"
    corrected, result = apply_corrections(source, [
        Correction('}}', occurrence=0, kind='template_close'),
        Correction('}}', occurrence=1, kind='template_close'),
    ])
    assert corrected == "SELECT '{{a}}' AS a, '{{b}}' AS b"
    assert result.source == corrected


def test_token_stream_covers_the_source_exactly():
    for source in [
        SAMPLE,
        "{{ x }}",
        "SELECT 1",
        "{%- if x -%}\nSELECT 1\n{%- endif -%}",
        "SELECT 'a{{ x %}b' -- tail {{ y }}",
        "SELECT /* {{x}} */ 'z'",
    ]:
        result = audit(source)
        pos = 0
        for token in result.tokens:
            assert token.start >= pos
            assert source[token.start:token.end] == token.value
            pos = token.end
        assert pos == len(source)


def test_serialized_records_expose_fixed_text_and_variables():
    result = audit(SAMPLE)
    record = result.audit_records()[0]
    assert record['fixed'] == "'receipt__daily'"
    assert record['spans'] == [(7, 16), (27, 34)]
    assert record['templates'][0]['variables'] == ['depot']
    assert 'depot' not in record['fixed']


def test_public_exports_are_complete():
    import pygments.lexers.sql_jinja_audit as audit_module
    for name in audit_module.__all__:
        assert hasattr(audit_module, name), name


def test_lexer_methods_expose_the_audit():
    lexer = SqlJinjaLexer()
    result = lexer.audit(SAMPLE)
    assert result.items[0].text == "'receipt__daily'"
    tokens = list(lexer.get_tokens_with_spans(SAMPLE))
    assert tokens == list(result.tokens)
    assert lexer.get_literal_items(SAMPLE) == result.items


def test_solution_entry_point():
    result = solution.audit(SAMPLE)
    assert result.items[0].text == "'receipt__daily'"
    assert solution.audit_source(SAMPLE).items == result.items


def test_history_named_fragments_and_spanning_rounds():
    history = AuditHistory()
    history.define_fragment('head', "SELECT 'abc")
    history.define_fragment('tail', "def' AS x")
    history.add_round(1, ['head'])
    history.add_round(2, ['tail'])
    history.add_round(3, ['head', 'tail'])
    assert history.rounds_referencing('tail') == (2, 3)
    result = history.audit_spanning([1, 2])
    item = result.items[0]
    assert item.text == "'abcdef'"
    assert [(ref.round, ref.fragment) for ref in result.original_spans(0)] == [
        (1, 'head'), (2, 'tail')]


def test_complex_template_regions_keep_each_tag_addressable():
    source = "SELECT 'x{% if a %}y{% else %}z{% endif %}w'"
    result = audit(source)
    item = result.items[0]
    assert item.text == "'xyzw'"
    assert [ref.kind for ref in item.template_references] == [
        'block', 'block', 'block']
    assert len(result.references) == 3
    assert all(ref.start < ref.end for ref in result.references)


def test_windowed_correction_limits_relexing_to_a_small_slice():
    source = "SELECT 1\n" * 50 + "SELECT 'a{{x}}b' -- tail {{ y }}\n"
    start = source.index("{{x}}")
    corrected, result = correction_sheet(
        source,
        [Correction('}}', start=start + 2, end=start + 4)],
        context=10)
    assert corrected.count('{{') == 2
    assert result.window_end - result.window_start <= 40
    assert 0 <= result.window_start < len(corrected)


def test_token_iteration_matches_unprocessed_triples():
    result = audit(SAMPLE)
    assert [(index, token_type, value) for index, token_type, value
            in result.tokens] == list(result.tokens_unprocessed)


def test_unfinished_template_reference_is_reported():
    result = audit("SELECT 'a{{x' -- tail")
    assert result.unfinished_templates
    assert all(ref.unfinished for ref in result.unfinished_templates)
    assert result.unfinished_templates[0].kind == 'variable'


def test_template_reference_variable_extraction():
    result = audit("{% for item in items %}{{ item.name }}{% endfor %}")
    assert [ref.kind for ref in result.references] == [
        'block', 'variable', 'block']
    assert result.references[0].variable_names == ('item', 'items')
    assert result.references[1].variable_names == ('item',)


def test_overlapping_corrections_are_rejected():
    source = "SELECT '{{a}}'"
    with pytest.raises(ValueError):
        apply_corrections(source, [
            Correction('x', start=7, end=12),
            Correction('y', start=10, end=14),
        ])


def test_compare_tokens_with_window_offset():
    old = audit("SELECT 1 -- {{ x }}\nSELECT 2")
    new = audit("SELECT 1 -- {{ x %}\nSELECT 2")
    changes = compare_tokens(old, new)
    assert any(change.kind == 'reclassified' for change in changes)
    # A windowed re-read can be compared against the same old region.
    window = audit_window("SELECT 1 -- {{ x %}\nSELECT 2", start=0, end=20)
    offset_changes = compare_tokens(
        audit_window("SELECT 1 -- {{ x }}\nSELECT 2", start=0, end=20),
        window)
    assert offset_changes


def test_classification_diff_reports_unchanged_characters_that_moved():
    old = "SELECT 'a{{x}}b' -- note {{ y }}"
    new = "SELECT 'a{{x %}b' -- note {{ y }}"
    changes = compare_classification(audit(old), audit(new))
    assert changes
    assert all(isinstance(change, ClassificationChange) for change in changes)
    # The tail comment characters were not edited, but now read as template.
    moved = [change for change in changes
             if 'note' in change.text or change.text.strip() == '--']
    assert moved
    assert any(change.old_part == 'sql' and change.new_part == 'template'
               for change in moved)


def test_classification_diff_handles_window_offsets():
    old_source = "SELECT 1 -- {{ x }}\nSELECT 2"
    new_source = "SELECT 1 -- {{ x %}\nSELECT 2"
    old_window = audit_window(old_source, start=6, end=len(old_source))
    new_window = audit_window(new_source, start=6, end=len(new_source))
    changes = compare_classification(
        old_window, new_window, old_offset=6, new_offset=6)
    assert changes
    assert all(change.old_span.start >= 6 for change in changes)
