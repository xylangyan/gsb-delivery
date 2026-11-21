"""
双审校例句 gloss 回执工作流的端到端测试。

场景对应审校需求：
- 导出材料展示位置、原 gloss、正文与候选的实际 ID/文字；
- 两位审校的独立决定与同格双依据、重投幂等；
- 同文不同候选 ID 的冲突按 ID 报告且整单隔离；
- 撤回后按基线恢复、他人依据保留；
- 词序变动、现有 gloss 被改、候选文字被改的过期单整单拒绝；
- 重开数据集后的实际词位对齐与 CLDF 校验。
"""
import logging

import pytest

from pycldf import TextCorpus
from pycldf.review import (
    ReviewEngine, ReviewReceipt, ReviewDecision,
    GLOSS_KIND, TRANSLATION_KIND, ACCEPTED, REJECTED, WITHDRAWN,
    REVIEWS_TABLE, DECISIONS_TABLE, CANDIDATES_TABLE, ensure_review_schema,
)

CANDIDATE_TEXT = 'candidate translation'
ORIGINAL_TRANSLATION = 'original translation'


def build_dataset(tmp_path):
    ds = TextCorpus.in_dir(tmp_path)
    ensure_review_schema(ds)
    examples = [
        {
            'ID': 'e1', 'Language_ID': 'l1', 'Primary_Text': 'a b a',
            'Analyzed_Word': ['a', 'b', 'a'],
            'Gloss': ['G1', '…', 'G3orig'],
            'Translated_Text': ORIGINAL_TRANSLATION,
        },
        # 同字面但 ID 不同的另一条例句。
        {
            'ID': 'e2', 'Language_ID': 'l1', 'Primary_Text': 'a b a',
            'Analyzed_Word': ['a', 'b', 'a'],
            'Gloss': ['G1', '…', 'G3orig'],
            'Translated_Text': ORIGINAL_TRANSLATION,
        },
    ]
    candidates = [
        {'ID': 'T1', 'Example_ID': 'e1', 'Translated_Text': CANDIDATE_TEXT},
        {'ID': 'T2', 'Example_ID': 'e1', 'Translated_Text': CANDIDATE_TEXT},
    ]
    ds.write(ExampleTable=examples)
    ds[CANDIDATES_TABLE].write(candidates)
    return ds


def make_sheet(engine, example_id='e1'):
    """模拟审校拿到材料后据此填写的回执基线。"""
    material = engine.export_material(example_id)
    example = engine.example(example_id)
    return dict(
        example_id=example_id,
        position_count=len(material['positions']),
        original_words=list(example['Analyzed_Word']),
        original_glosses=list(example['Gloss']),
        original_translation=example['Translated_Text'],
    )


def gloss_receipt(receipt_id, reviewer, sheet, position, proposed, original=None):
    return ReviewReceipt(
        id=receipt_id, reviewer=reviewer, example_id=sheet['example_id'],
        position_count=sheet['position_count'],
        original_words=sheet['original_words'],
        original_glosses=sheet['original_glosses'],
        original_translation=sheet['original_translation'],
        decisions=[ReviewDecision(
            position=position, kind=GLOSS_KIND,
            original_value=sheet['original_glosses'][position]
            if original is None else original,
            proposed_gloss=proposed)])


def jia_receipt(receipt_id, sheet):
    """甲：确认第二位、补第三位、选候选 T1。"""
    return ReviewReceipt(
        id=receipt_id, reviewer='jia', example_id=sheet['example_id'],
        position_count=sheet['position_count'],
        original_words=sheet['original_words'],
        original_glosses=sheet['original_glosses'],
        original_translation=sheet['original_translation'],
        decisions=[
            ReviewDecision(position=1, kind=GLOSS_KIND,
                           original_value='…', proposed_gloss='B'),
            ReviewDecision(position=2, kind=GLOSS_KIND,
                           original_value='G3orig', proposed_gloss='G3'),
            ReviewDecision(position=-1, kind=TRANSLATION_KIND,
                           original_value=ORIGINAL_TRANSLATION,
                           proposed_text=CANDIDATE_TEXT,
                           selected_candidate_id='T1'),
        ])


def yi_receipt(receipt_id, sheet):
    """乙：只确认第二位 gloss。"""
    return gloss_receipt(receipt_id, 'yi', sheet, 1, 'B', original='…')


def current_glosses(engine, example_id='e1'):
    return list(engine.example(example_id)['Gloss'])


def test_export_material_shows_positions_gloss_text_and_candidates(tmp_path):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    material = engine.export_material('e1')

    assert material['Example_ID'] == 'e1'
    assert material['Primary_Text'] == 'a b a'
    assert material['Translated_Text'] == ORIGINAL_TRANSLATION
    assert [p['position'] for p in material['positions']] == [0, 1, 2]
    assert [p['word'] for p in material['positions']] == ['a', 'b', 'a']
    assert [p['gloss'] for p in material['positions']] == ['G1', '…', 'G3orig']
    assert {c['ID'] for c in material['candidates']} == {'T1', 'T2'}
    assert all(c['Translated_Text'] == CANDIDATE_TEXT
               for c in material['candidates'])


@pytest.mark.parametrize('order', [('jia', 'yi'), ('yi', 'jia')])
def test_two_reviewers_dual_evidence_and_resubmit_is_idempotent(tmp_path, order):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    sheet = make_sheet(engine)

    receipts = {'jia': jia_receipt('r-jia', sheet),
                'yi': yi_receipt('r-yi', sheet)}
    for who in order:
        result = engine.submit(receipts[who])
        assert result.ok

    example = engine.example('e1')
    assert current_glosses(engine) == ['G1', 'B', 'G3']
    assert example['Translated_Text'] == CANDIDATE_TEXT

    evidence = engine.evidence('e1')
    slot1 = {(e['reviewer'], e['value']) for e in evidence[(GLOSS_KIND, 1)]}
    assert slot1 == {('jia', 'B'), ('yi', 'B')}
    assert len(evidence[(GLOSS_KIND, 2)]) == 1
    assert evidence[(TRANSLATION_KIND, -1)][0]['candidate_id'] == 'T1'

    decision_rows_before = len(list(ds.iter_rows(DECISIONS_TABLE)))
    # 乙重投内容完全相同的一单：幂等，不新增依据。
    resubmit = engine.submit(yi_receipt('r-yi-again', sheet))
    assert resubmit.ok
    assert resubmit.receipt_id == 'r-yi'
    assert len(list(ds.iter_rows(DECISIONS_TABLE))) == decision_rows_before
    assert len(engine.evidence('e1')[(GLOSS_KIND, 1)]) == 2


def test_distinct_candidate_ids_same_text_conflict_and_isolation(tmp_path):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    sheet = make_sheet(engine)
    assert engine.submit(jia_receipt('r-jia', sheet)).ok

    # 乙另提一单：选显示相同但 ID 不同的 T2，并附带此前没改过的第零位补写。
    yi_t2 = ReviewReceipt(
        id='r-yi-t2', reviewer='yi', example_id='e1',
        position_count=sheet['position_count'],
        original_words=sheet['original_words'],
        original_glosses=sheet['original_glosses'],
        original_translation=sheet['original_translation'],
        decisions=[
            ReviewDecision(position=-1, kind=TRANSLATION_KIND,
                           original_value=ORIGINAL_TRANSLATION,
                           proposed_text=CANDIDATE_TEXT,
                           selected_candidate_id='T2'),
            ReviewDecision(position=0, kind=GLOSS_KIND,
                           original_value='G1', proposed_gloss='G1-edited'),
        ])
    result = engine.submit(yi_t2)

    assert not result.ok
    candidate_conflicts = [r for r in result.reasons if r['scope'] == 'candidate']
    assert len(candidate_conflicts) == 1
    conflict = candidate_conflicts[0]
    assert conflict['candidate_id'] == 'T2'
    assert conflict['expected'] == 'T1'
    assert conflict['actual'] == 'T2'
    assert 'same translation text' in conflict['detail']

    # 整单不生效：附带补写的词位、译文及其它待写词位都不变。
    assert current_glosses(engine) == ['G1', 'B', 'G3']
    assert engine.example('e1')['Translated_Text'] == CANDIDATE_TEXT
    # 同字面的另一条例句完全不动。
    assert current_glosses(engine, 'e2') == ['G1', '…', 'G3orig']
    assert engine.example('e2')['Translated_Text'] == ORIGINAL_TRANSLATION
    assert engine.receipts('e1')[-1].status == REJECTED


def test_withdrawal_restores_baselines_and_keeps_other_evidence(tmp_path):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    sheet = make_sheet(engine)
    engine.submit(jia_receipt('r-jia', sheet))
    engine.submit(yi_receipt('r-yi', sheet))

    # 先撤回甲：第二位乙仍确认而保留，第三位恢复审校前原值，译文恢复，候选行仍在。
    engine.withdraw('r-jia')
    assert current_glosses(engine) == ['G1', 'B', 'G3orig']
    assert engine.example('e1')['Translated_Text'] == ORIGINAL_TRANSLATION
    assert {c['ID'] for c in engine.candidates_for('e1')} == {'T1', 'T2'}
    assert engine.receipts('e1')[0].status == WITHDRAWN

    # 再撤乙：只有第二位随之恢复为未标注。
    engine.withdraw('r-yi')
    assert current_glosses(engine) == ['G1', '…', 'G3orig']
    assert engine.example('e1')['Translated_Text'] == ORIGINAL_TRANSLATION


def test_stale_word_order_change_rejects_whole_receipt(tmp_path):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    sheet = make_sheet(engine)

    engine.example('e1')['Analyzed_Word'] = ['a', 'b', 'x', 'a']
    engine._write_examples()

    result = engine.submit(jia_receipt('r-stale-order', sheet))
    assert not result.ok
    assert all(r['scope'] == 'position' for r in result.reasons)
    assert result.reasons[0]['expected'] == '3'
    assert result.reasons[0]['actual'] == '4'
    # 整单不生效。
    assert current_glosses(engine) == ['G1', '…', 'G3orig']
    assert engine.example('e1')['Translated_Text'] == ORIGINAL_TRANSLATION


def test_stale_same_length_reordered_word_rejects(tmp_path):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    sheet = make_sheet(engine)

    engine.example('e1')['Analyzed_Word'] = ['b', 'a', 'a']
    engine._write_examples()

    result = engine.submit(jia_receipt('r-stale-swap', sheet))
    assert not result.ok
    word_conflicts = [r for r in result.reasons if 'word at this position' in r['detail']]
    # 甲的回执只触及第一、二位；其中第一位的词由 b 变成 a。
    assert [r['position'] for r in word_conflicts] == [1]
    assert word_conflicts[0]['expected'] == 'b'
    assert word_conflicts[0]['actual'] == 'a'


def test_stale_existing_gloss_edited_rejects(tmp_path):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    sheet = make_sheet(engine)

    engine.example('e1')['Gloss'][2] = 'HIJACKED'
    engine._write_examples()

    result = engine.submit(jia_receipt('r-stale-gloss', sheet))
    assert not result.ok
    gloss_conflicts = [
        r for r in result.reasons
        if r['scope'] == 'position' and r['expected'] == 'G3orig']
    assert len(gloss_conflicts) == 1
    assert gloss_conflicts[0]['actual'] == 'HIJACKED'
    assert gloss_conflicts[0]['position'] == 2
    assert current_glosses(engine)[1] == '…'


def test_stale_candidate_text_edited_rejects(tmp_path):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    sheet = make_sheet(engine)

    engine._candidates['T1']['Translated_Text'] = 'edited candidate text'
    engine.write_candidates(list(engine._candidates.values()))

    result = engine.submit(jia_receipt('r-stale-cand', sheet))
    assert not result.ok
    candidate_conflicts = [r for r in result.reasons if r['scope'] == 'candidate']
    assert len(candidate_conflicts) == 1
    assert candidate_conflicts[0]['candidate_id'] == 'T1'
    assert candidate_conflicts[0]['expected'] == CANDIDATE_TEXT
    assert candidate_conflicts[0]['actual'] == 'edited candidate text'
    assert engine.example('e1')['Translated_Text'] == ORIGINAL_TRANSLATION


def test_reopen_persisted_dataset_keeps_alignment_and_validates(tmp_path):
    ds = build_dataset(tmp_path)
    engine = ReviewEngine(ds)
    sheet = make_sheet(engine)
    engine.submit(jia_receipt('r-jia', sheet))
    engine.submit(yi_receipt('r-yi', sheet))
    engine.withdraw('r-jia')

    # 重开语料与审校回执（指向目录中的具体元数据文件，而非模块默认元数据）。
    metadata = ds.directory / 'TextCorpus-metadata.json'
    reopened = TextCorpus.from_metadata(metadata)
    assert reopened.validate()
    reopened_engine = ReviewEngine(reopened)

    example = reopened_engine.example('e1')
    assert len(example['Analyzed_Word']) == len(example['Gloss']) == 3
    assert list(example['Gloss']) == ['G1', 'B', 'G3orig']
    assert example['Translated_Text'] == ORIGINAL_TRANSLATION

    receipts = {r.id: r for r in reopened_engine.receipts('e1')}
    assert receipts['r-jia'].status == WITHDRAWN
    assert receipts['r-yi'].status == ACCEPTED

    # 重开后撤回乙仍只影响第二位。
    reopened_engine.withdraw('r-yi')
    assert list(reopened_engine.example('e1')['Gloss']) == ['G1', '…', 'G3orig']

    assert reopened.validate(log=logging.getLogger('review-test'))
