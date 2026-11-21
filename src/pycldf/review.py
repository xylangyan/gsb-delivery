"""
双审校例句 gloss 回执引擎。

在标准 CLDF ``ExampleTable`` 之外增加三张自定义表（候选译文、审校回执与
逐词位决定），记录提交时快照，从而支持：

- 两位审校各自留下独立决定，同一词位可同时保留多份依据；
- 按回执整体撤回，并恢复该次审校前的原值（仍有他人依据时保留现值）；
- 提交时的乐观并发校验：词序变动、现有 gloss 被改、候选译文文字被改都会整单拒绝；
- 两个不同候选即使文字完全相同，也按候选 ID 报告冲突。
"""
import datetime
import pathlib
import dataclasses
from typing import Optional

from pycldf.terms import term_uri

__all__ = [
    'ReviewEngine', 'ReviewReceipt', 'ReviewDecision', 'ConflictReason', 'ReceiptResult',
    'GLOSS_KIND', 'TRANSLATION_KIND', 'PENDING', 'ACCEPTED', 'REJECTED', 'WITHDRAWN',
    'ensure_review_schema',
]

GLOSS_KIND = 'gloss'
TRANSLATION_KIND = 'translation'

PENDING = 'pending'
ACCEPTED = 'accepted'
REJECTED = 'rejected'
WITHDRAWN = 'withdrawn'

REVIEWS_TABLE = 'reviews.csv'
DECISIONS_TABLE = 'review_decisions.csv'
CANDIDATES_TABLE = 'translation_candidates.csv'


def _norm(value) -> str:
    """把缺失值统一为空串，便于快照比对。"""
    return '' if value is None else str(value)


@dataclasses.dataclass
class ReviewDecision:
    """回执中针对单个词位（或译文）的决定及其提交时原样快照。"""
    position: int
    kind: str = GLOSS_KIND
    original_value: Optional[str] = None
    proposed_gloss: Optional[str] = None
    proposed_text: Optional[str] = None
    selected_candidate_id: Optional[str] = None

    def signature(self):
        return (
            self.position, self.kind, _norm(self.original_value),
            _norm(self.proposed_gloss), _norm(self.proposed_text),
            _norm(self.selected_candidate_id))

    def row_id(self, review_id):
        token = f'{review_id}-{self.kind}-{self.position}'
        return ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in token)

    def as_row(self, review_id):
        return {
            'ID': self.row_id(review_id),
            'Review_ID': review_id,
            'Position': self.position,
            'Kind': self.kind,
            'Original_Value': _norm(self.original_value),
            'Proposed_Gloss': _norm(self.proposed_gloss),
            'Proposed_Text': _norm(self.proposed_text),
            'Selected_Candidate_ID': _norm(self.selected_candidate_id),
        }

    @classmethod
    def from_row(cls, row):
        return cls(
            position=int(row['Position']),
            kind=row['Kind'],
            original_value=row['Original_Value'] or None,
            proposed_gloss=row['Proposed_Gloss'] or None,
            proposed_text=row['Proposed_Text'] or None,
            selected_candidate_id=row['Selected_Candidate_ID'] or None,
        )


@dataclasses.dataclass
class ReviewReceipt:
    """一份审校回执：针对某条例句的若干独立决定。"""
    id: str
    reviewer: str
    example_id: str
    position_count: int
    original_words: list
    original_glosses: list
    original_translation: Optional[str]
    decisions: list = dataclasses.field(default_factory=list)
    created_on: Optional[str] = None
    status: str = PENDING

    def signature(self):
        return (
            self.reviewer, self.example_id, self.position_count,
            tuple(_norm(w) for w in self.original_words),
            tuple(_norm(g) for g in self.original_glosses),
            _norm(self.original_translation),
            tuple(sorted(d.signature() for d in self.decisions)),
        )


@dataclasses.dataclass
class ConflictReason:
    """单条冲突/过期说明。"""
    scope: str  # position | translation | candidate
    detail: str
    position: Optional[int] = None
    expected: Optional[str] = None
    actual: Optional[str] = None
    candidate_id: Optional[str] = None

    def as_dict(self):
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ReceiptResult:
    """回执处理结果。"""
    receipt_id: str
    accepted: bool
    reasons: list = dataclasses.field(default_factory=list)
    evidence: list = dataclasses.field(default_factory=list)

    @property
    def ok(self):
        return self.accepted


def ensure_review_schema(dataset):
    """在 CLDF 数据集上补齐回执、决定与候选译文三张自定义表。"""
    existing = {str(t.url) for t in dataset.tables}
    created = []

    if CANDIDATES_TABLE not in existing:
        dataset.add_table(
            CANDIDATES_TABLE,
            term_uri('id'),
            {'name': 'Example_ID', 'datatype': 'string', 'required': True},
            {'name': 'Translated_Text', 'datatype': 'string'},
            primaryKey=['ID'],
        )
        dataset.add_foreign_key(
            CANDIDATES_TABLE, 'Example_ID', 'ExampleTable')
        created.append(CANDIDATES_TABLE)

    if REVIEWS_TABLE not in existing:
        dataset.add_table(
            REVIEWS_TABLE,
            term_uri('id'),
            {'name': 'Reviewer', 'datatype': 'string', 'required': True},
            {'name': 'Example_ID', 'datatype': 'string', 'required': True},
            {'name': 'Status', 'datatype': {
                'base': 'string',
                'format': 'pending|accepted|rejected|withdrawn'}},
            {'name': 'Created_On', 'datatype': 'string'},
            {'name': 'Position_Count', 'datatype': 'integer'},
            {'name': 'Original_Words', 'datatype': 'string', 'separator': '\t'},
            {'name': 'Original_Glosses', 'datatype': 'string', 'separator': '\t'},
            {'name': 'Original_Translation', 'datatype': 'string'},
            primaryKey=['ID'],
        )
        dataset.add_foreign_key(REVIEWS_TABLE, 'Example_ID', 'ExampleTable')
        created.append(REVIEWS_TABLE)

    if DECISIONS_TABLE not in existing:
        dataset.add_table(
            DECISIONS_TABLE,
            term_uri('id'),
            {'name': 'Review_ID', 'datatype': 'string', 'required': True},
            {'name': 'Position', 'datatype': 'integer', 'required': True},
            {'name': 'Kind', 'datatype': {
                'base': 'string', 'format': 'gloss|translation'}},
            {'name': 'Original_Value', 'datatype': 'string'},
            {'name': 'Proposed_Gloss', 'datatype': 'string'},
            {'name': 'Proposed_Text', 'datatype': 'string'},
            {'name': 'Selected_Candidate_ID', 'datatype': 'string'},
            primaryKey=['ID'],
        )
        dataset.add_foreign_key(DECISIONS_TABLE, 'Review_ID', REVIEWS_TABLE)
        dataset.add_foreign_key(
            DECISIONS_TABLE, 'Selected_Candidate_ID', CANDIDATES_TABLE)
        created.append(DECISIONS_TABLE)

    # 立即落表头，使尚未产生回执的数据集也能通过外键完整性校验。
    for table_name in created:
        dataset[table_name].write([])


class ReviewEngine:
    """在内存中操作 CLDF 例句并持久化回执/决定的引擎。"""

    def __init__(self, dataset):
        self.ds = dataset
        self._examples = {}
        for row in dataset.iter_rows('ExampleTable'):
            self._examples[row['ID']] = row
        self._candidates = {}
        for row in self._iter_table(CANDIDATES_TABLE):
            self._candidates[row['ID']] = row
        self._decisions = {}
        for row in self._iter_table(DECISIONS_TABLE):
            self._decisions.setdefault(row['Review_ID'], []).append(row)
        self._receipts = {}
        for row in self._iter_table(REVIEWS_TABLE):
            self._receipts[row['ID']] = self._load_receipt(row)

    def _iter_table(self, table_name):
        """表文件尚未写出时按空表处理。"""
        table = self.ds[table_name]
        path = pathlib.Path(table.url.resolve(table._parent.base))  # pylint: disable=W0212
        if not path.exists():
            return []
        return list(self.ds.iter_rows(table_name))

    # ------------------------------------------------------------------
    # 读取与导出
    # ------------------------------------------------------------------
    def _load_receipt(self, row):
        decisions = [
            ReviewDecision.from_row(d)
            for d in self._decisions.get(row['ID'], [])
        ]
        return ReviewReceipt(
            id=row['ID'],
            reviewer=row['Reviewer'],
            example_id=row['Example_ID'],
            position_count=int(row['Position_Count']),
            original_words=list(row['Original_Words'] or []),
            original_glosses=list(row['Original_Glosses'] or []),
            original_translation=row['Original_Translation'],
            decisions=decisions,
            created_on=row['Created_On'],
            status=row['Status'],
        )

    def example(self, example_id):
        return self._examples[example_id]

    def candidates_for(self, example_id):
        return [
            {'ID': row['ID'], 'Example_ID': row['Example_ID'],
             'Translated_Text': row['Translated_Text']}
            for row in self._candidates.values()
            if row['Example_ID'] == example_id
        ]

    def export_material(self, example_id=None):
        """导出审校材料：展示位置、原 gloss、正文与候选的实际 ID/文字。"""
        ids = [example_id] if example_id else sorted(self._examples)
        material = []
        for ex_id in ids:
            row = self._examples[ex_id]
            words = list(row['Analyzed_Word'] or [])
            glosses = list(row['Gloss'] or [])
            material.append({
                'Example_ID': ex_id,
                'Primary_Text': row['Primary_Text'],
                'Translated_Text': row['Translated_Text'],
                'positions': [
                    {'position': index, 'word': word,
                     'gloss': glosses[index] if index < len(glosses) else None}
                    for index, word in enumerate(words)
                ],
                'candidates': self.candidates_for(ex_id),
            })
        return material[0] if example_id else material

    def receipts(self, example_id=None):
        rows = [self._receipts[k] for k in sorted(self._receipts)]
        if example_id:
            rows = [r for r in rows if r.example_id == example_id]
        return rows

    def evidence(self, example_id):
        """当前仍被接受回执支撑的依据，按词位/译文归集。"""
        result = {}
        for receipt in self._receipts.values():
            if receipt.status != ACCEPTED:
                continue
            for decision in receipt.decisions:
                key = (decision.kind, decision.position)
                result.setdefault(key, []).append({
                    'review_id': receipt.id,
                    'reviewer': receipt.reviewer,
                    'value': decision.proposed_gloss if decision.kind == GLOSS_KIND
                    else (decision.selected_candidate_id or decision.proposed_text),
                    'candidate_id': decision.selected_candidate_id,
                })
        return result

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------
    def submit(self, receipt: ReviewReceipt) -> ReceiptResult:
        receipt.created_on = receipt.created_on or datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec='seconds')

        duplicate = self._find_equivalent(receipt)
        if duplicate is not None and duplicate.status == ACCEPTED:
            # 重投同一单不新增依据，直接幂等返回。
            return ReceiptResult(
                receipt_id=duplicate.id, accepted=True,
                evidence=self._evidence_for(duplicate))

        reasons = self._check_receipt(receipt)
        if reasons:
            receipt.status = REJECTED
            self._persist(receipt)
            return ReceiptResult(
                receipt_id=receipt.id, accepted=False,
                reasons=[r.as_dict() for r in reasons])

        self._apply(receipt)
        receipt.status = ACCEPTED
        self._persist(receipt)
        return ReceiptResult(
            receipt_id=receipt.id, accepted=True,
            evidence=self._evidence_for(receipt))

    def _find_equivalent(self, receipt):
        for other in self._receipts.values():
            if other.status in (ACCEPTED, PENDING) and other.signature() == receipt.signature():
                return other
        return None

    def _check_receipt(self, receipt):
        reasons = []
        example = self._examples.get(receipt.example_id)
        if example is None:
            reasons.append(ConflictReason(
                'position', f'unknown example {receipt.example_id}'))
            return reasons

        words = list(example['Analyzed_Word'] or [])
        glosses = list(example['Gloss'] or [])

        positions = [d.position for d in receipt.decisions if d.kind == GLOSS_KIND]
        if len(positions) != len(set(positions)):
            reasons.append(ConflictReason('position', 'duplicate positions in receipt'))
        for decision in receipt.decisions:
            if decision.kind == GLOSS_KIND and not (0 <= decision.position < len(words)):
                reasons.append(ConflictReason(
                    'position', f'position {decision.position} out of range',
                    position=decision.position))

        # 词序/词数变动：按位置对齐的格子全部失效。
        order_changed = len(words) != receipt.position_count
        if order_changed:
            for decision in receipt.decisions:
                if decision.kind == GLOSS_KIND:
                    reasons.append(ConflictReason(
                        'position',
                        f'word order/length changed: exported {receipt.position_count} '
                        f'slots, current {len(words)}',
                        position=decision.position,
                        expected=str(receipt.position_count), actual=str(len(words))))

        for decision in receipt.decisions:
            if decision.kind == GLOSS_KIND and not order_changed \
                    and 0 <= decision.position < len(words):
                if decision.position < len(receipt.original_words) and \
                        _norm(words[decision.position]) != \
                        _norm(receipt.original_words[decision.position]):
                    reasons.append(ConflictReason(
                        'position',
                        'word at this position changed since the sheet was sent',
                        position=decision.position,
                        expected=receipt.original_words[decision.position],
                        actual=words[decision.position]))
                current = glosses[decision.position] \
                    if decision.position < len(glosses) else None
                if _norm(current) != _norm(decision.original_value) and \
                        not self._explained_by_accepted(
                            receipt, decision, _norm(current)):
                    reasons.append(ConflictReason(
                        'position',
                        'gloss at this position changed since the sheet was sent',
                        position=decision.position,
                        expected=decision.original_value, actual=_norm(current)))

            if decision.kind == TRANSLATION_KIND:
                current_text = _norm(example['Translated_Text'])
                if current_text != _norm(receipt.original_translation) and \
                        not self._explained_by_accepted(
                            receipt, decision, current_text):
                    reasons.append(ConflictReason(
                        'translation',
                        'translation text changed since the sheet was sent',
                        expected=receipt.original_translation,
                        actual=example['Translated_Text']))

            if decision.selected_candidate_id:
                candidate = self._candidates.get(decision.selected_candidate_id)
                if candidate is None:
                    reasons.append(ConflictReason(
                        'candidate',
                        f'candidate {decision.selected_candidate_id} no longer exists',
                        candidate_id=decision.selected_candidate_id))
                else:
                    if candidate['Example_ID'] != receipt.example_id:
                        reasons.append(ConflictReason(
                            'candidate',
                            'candidate belongs to a different example',
                            candidate_id=candidate['ID']))
                    if _norm(candidate['Translated_Text']) != \
                            _norm(decision.proposed_text):
                        reasons.append(ConflictReason(
                            'candidate',
                            'candidate text was edited after the sheet was sent',
                            candidate_id=candidate['ID'],
                            expected=decision.proposed_text,
                            actual=candidate['Translated_Text']))

        reasons.extend(self._evidence_conflicts(receipt))
        return reasons

    def _proposed_value(self, decision):
        if decision.kind == GLOSS_KIND:
            return _norm(decision.proposed_gloss)
        if decision.selected_candidate_id:
            return _norm(self._candidates[decision.selected_candidate_id][
                'Translated_Text'])
        return _norm(decision.proposed_text)

    def _explained_by_accepted(self, receipt, decision, current_value):
        """同批已接受回执是否把该格改成了现值（可解释的改动不算过期）。"""
        if current_value == _norm(decision.original_value):
            return True
        for other in self._receipts.values():
            if other.status != ACCEPTED or other.example_id != receipt.example_id:
                continue
            for other_decision in other.decisions:
                if other_decision.kind == decision.kind and \
                        other_decision.position == decision.position and \
                        self._proposed_value(other_decision) == current_value:
                    return True
        return False

    def _evidence_conflicts(self, receipt):
        """与此前已接受回执之间的冲突（含同文不同候选 ID）。"""
        reasons = []
        for decision in receipt.decisions:
            for other in self._receipts.values():
                if other.status != ACCEPTED or other.example_id != receipt.example_id:
                    continue
                for other_decision in other.decisions:
                    reasons.extend(
                        self._decision_conflict(decision, other_decision, other.id))
        return reasons

    def _decision_conflict(self, decision, other, other_id):
        if decision.kind != other.kind or decision.position != other.position:
            return []

        if decision.kind == GLOSS_KIND:
            if _norm(decision.proposed_gloss) != _norm(other.proposed_gloss):
                return [ConflictReason(
                    'position',
                    f'conflicting accepted gloss from receipt {other_id}',
                    position=decision.position,
                    expected=other.proposed_gloss,
                    actual=decision.proposed_gloss)]
            return []

        # 译文：候选 ID 不同即冲突，即便拼出的正文字符串相同。
        if decision.selected_candidate_id and other.selected_candidate_id:
            if decision.selected_candidate_id != other.selected_candidate_id:
                same_text = _norm(decision.proposed_text) == _norm(other.proposed_text)
                return [ConflictReason(
                    'candidate',
                    ('distinct candidate IDs resolve to the same translation text'
                     if same_text else
                     f'conflicting candidate choice from receipt {other_id}'),
                    candidate_id=decision.selected_candidate_id,
                    expected=other.selected_candidate_id,
                    actual=decision.selected_candidate_id)]
            return []

        if _norm(decision.proposed_text) != _norm(other.proposed_text):
            return [ConflictReason(
                'translation',
                f'conflicting accepted translation from receipt {other_id}',
                expected=other.proposed_text, actual=decision.proposed_text)]
        return []

    def _apply(self, receipt):
        example = self._examples[receipt.example_id]
        for decision in receipt.decisions:
            if decision.kind == GLOSS_KIND:
                glosses = list(example['Gloss'] or [])
                while len(glosses) <= decision.position:
                    glosses.append('…')
                glosses[decision.position] = decision.proposed_gloss
                example['Gloss'] = glosses
            else:
                if decision.selected_candidate_id:
                    example['Translated_Text'] = \
                        self._candidates[decision.selected_candidate_id]['Translated_Text']
                else:
                    example['Translated_Text'] = decision.proposed_text
        self._write_examples()

    # ------------------------------------------------------------------
    # 撤回
    # ------------------------------------------------------------------
    def withdraw(self, receipt_id) -> ReceiptResult:
        receipt = self._receipts.get(receipt_id)
        if receipt is None:
            raise KeyError(receipt_id)
        if receipt.status == WITHDRAWN:
            return ReceiptResult(receipt_id=receipt_id, accepted=False, reasons=[
                ConflictReason('position', 'receipt already withdrawn').as_dict()])
        if receipt.status != ACCEPTED:
            # 被拒绝的回执从未生效，无可撤回的内容。
            return ReceiptResult(receipt_id=receipt_id, accepted=False, reasons=[
                ConflictReason(
                    'position',
                    f'only accepted receipts can be withdrawn (status={receipt.status})'
                ).as_dict()])

        example = self._examples[receipt.example_id]
        for decision in receipt.decisions:
            if self._still_supported(receipt_id, decision):
                continue
            if decision.kind == GLOSS_KIND:
                glosses = list(example['Gloss'] or [])
                if 0 <= decision.position < len(glosses):
                    glosses[decision.position] = \
                        self._baseline(receipt, decision) or '…'
                    example['Gloss'] = glosses
            else:
                example['Translated_Text'] = receipt.original_translation
        self._write_examples()

        receipt.status = WITHDRAWN
        self._persist(receipt)
        return ReceiptResult(receipt_id=receipt_id, accepted=True,
                             evidence=self._evidence_for(receipt))

    def _baseline(self, receipt, decision):
        """该词位在本轮审校开始前的原值（取最早触及它的回执快照）。"""
        candidates = [
            r for r in self._receipts.values()
            if r.example_id == receipt.example_id
            and any(d.kind == decision.kind and d.position == decision.position
                    for d in r.decisions)]
        if not candidates:
            return decision.original_value
        earliest = min(candidates, key=lambda r: r.created_on or '')
        for d in earliest.decisions:
            if d.kind == decision.kind and d.position == decision.position:
                return d.original_value
        return decision.original_value

    def _still_supported(self, withdrawn_id, decision):
        """其它仍接受的回执是否支撑同一结果。"""
        for other in self._receipts.values():
            if other.id == withdrawn_id or other.status != ACCEPTED:
                continue
            for other_decision in other.decisions:
                if other_decision.kind != decision.kind \
                        or other_decision.position != decision.position:
                    continue
                if decision.kind == GLOSS_KIND:
                    if _norm(other_decision.proposed_gloss) == \
                            _norm(decision.proposed_gloss):
                        return True
                elif decision.selected_candidate_id:
                    if other_decision.selected_candidate_id == \
                            decision.selected_candidate_id:
                        return True
                elif _norm(other_decision.proposed_text) == \
                        _norm(decision.proposed_text):
                    return True
        return False

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _evidence_for(self, receipt):
        evidence = self.evidence(receipt.example_id)
        out = []
        for decision in receipt.decisions:
            out.extend(evidence.get((decision.kind, decision.position), []))
        return out

    def _persist(self, receipt):
        self._receipts[receipt.id] = receipt
        review_rows = [
            {
                'ID': r.id, 'Reviewer': r.reviewer, 'Example_ID': r.example_id,
                'Status': r.status, 'Created_On': r.created_on,
                'Position_Count': r.position_count,
                'Original_Words': r.original_words,
                'Original_Glosses': r.original_glosses,
                'Original_Translation': r.original_translation,
            }
            for r in self._receipts.values()
        ]
        self.ds[REVIEWS_TABLE].write(review_rows)

        decision_rows = []
        for r in self._receipts.values():
            for decision in r.decisions:
                decision_rows.append(decision.as_row(r.id))
        self.ds[DECISIONS_TABLE].write(decision_rows)

    def _write_examples(self):
        self.ds['ExampleTable'].write(list(self._examples.values()))

    def write_candidates(self, candidates):
        rows = [
            {'ID': c['ID'], 'Example_ID': c['Example_ID'],
             'Translated_Text': c['Translated_Text']}
            for c in candidates
        ]
        self.ds[CANDIDATES_TABLE].write(rows)
        for row in self.ds.iter_rows(CANDIDATES_TABLE):
            self._candidates[row['ID']] = row
