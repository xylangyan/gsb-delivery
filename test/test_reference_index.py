import unittest

from mistletoe.reference_index import (
    LineColumnEdit,
    Position,
    ReferenceIndex,
    TextEdit,
    line_column_to_offset,
    offset_to_line_column,
)


class TestPositions(unittest.TestCase):
    def test_line_column_round_trip(self):
        document = 'abc\ndef\n'
        self.assertEqual(line_column_to_offset(document, 1, 1), 0)
        self.assertEqual(line_column_to_offset(document, 2, 2), 5)
        self.assertEqual(offset_to_line_column(document, 5), Position(2, 2))
    def test_end_of_line_insert(self):
        document = 'abc\ndef\n'
        self.assertEqual(line_column_to_offset(document, 1, 4), 3)

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            line_column_to_offset('abc\n', 4, 1)
        with self.assertRaises(ValueError):
            line_column_to_offset('abc\n', 1, 9)


class TestReferenceIndex(unittest.TestCase):
    def test_links_images_and_labels(self):
        document = (
            '段落 [甲][总表] 和 ![图][样本] 与 [直链](https://example.test)\n'
            '\n'
            '[总表]: /master\n'
            '[样本]: /sample\n'
        )
        index = ReferenceIndex(document)
        references = index.index.references
        self.assertEqual([item.kind for item in references], ['link', 'image', 'link'])
        self.assertEqual(
            [item.label for item in references],
            ['总表', '样本', None],
        )
        self.assertEqual(
            [item.destination for item in references],
            ['/master', '/sample', 'https://example.test'],
        )
        self.assertEqual(references[1].reference_kind, 'full')
        self.assertEqual(references[2].reference_kind, 'inline')
        self.assertIsNone(references[2].definition)
        self.assertTrue(index.verify())

    def test_ranges_point_at_source(self):
        document = '前文 [甲][总表] 后文\n\n[总表]: /master\n'
        index = ReferenceIndex(document)
        reference = index.index.references[0]
        line = document.splitlines()[reference.line - 1]
        start = reference.column - 1
        end = reference.end_column - 1
        self.assertEqual(line[start:end], '[甲][总表]')
        definition = index.index.definition_for('总表')
        self.assertIsNotNone(definition)
        self.assertEqual(definition.range.start.line, 3)

    def test_effective_definition_first_wins(self):
        document = (
            '[甲][总表]\n'
            '\n'
            '[总表]: /first\n'
            '\n'
            '文字\n'
            '\n'
            '[总表]: /second\n'
        )
        index = ReferenceIndex(document)
        effective = index.index.effective_definitions['总表']
        self.assertEqual(effective.destination, '/first')
        self.assertTrue(effective.effective)
        shadowed = [item for item in index.index.definitions if not item.effective]
        self.assertEqual(len(shadowed), 1)
        self.assertEqual(shadowed[0].destination, '/second')
        self.assertEqual(shadowed[0].shadowed_by, effective.index)

    def test_normalized_labels(self):
        document = '段落 [甲][ 总表 ]\n\n[总表]: /master\n'
        index = ReferenceIndex(document)
        self.assertEqual(index.index.references[0].label, '总表')
        self.assertEqual(index.index.definition_for('总表').destination, '/master')

    def test_crlf_document(self):
        document = '段落 [甲][u]\r\n\r\n[u]: /url\r\n'
        index = ReferenceIndex(document)
        self.assertEqual(index.index.references[0].destination, '/url')
        self.assertTrue(index.verify())

    def test_empty_document(self):
        index = ReferenceIndex('')
        self.assertEqual(index.index.references, ())
        self.assertTrue(index.verify())


class TestIncrementalScenario(unittest.TestCase):
    def _fenced_sample_document(self):
        return (
            '# 验货手册\n'
            '\n'
            '[证据 [样本]][总表]\n'
            '\n'
            '说明段落。\n'
            '\n'
            '```\n'
            '[样本]: /sample\n'
            '```\n'
            '\n'
            '文末。\n'
            '\n'
            '[总表]: /master\n'
        )

    def test_sample_definition_out_of_code_fence(self):
        index = ReferenceIndex(self._fenced_sample_document())
        before = index.index
        self.assertEqual(len(before.references), 1)
        only = before.references[0]
        self.assertEqual((only.label, only.destination), ('总表', '/master'))
        self.assertEqual(only.reference_kind, 'full')

        # Remove the opening and closing fence in a single save.
        index.apply_edits([
            LineColumnEdit(7, 1, 8, 1, ''),
            LineColumnEdit(9, 1, 10, 1, ''),
        ])
        after = index.index
        self.assertEqual(
            [(item.label, item.destination) for item in after.references],
            [('样本', '/sample'), ('总表', '/master')],
        )
        self.assertEqual(after.references[0].reference_kind, 'shortcut')
        self.assertTrue(index.verify())
        self.assertTrue(index.verify(0))

        changes = index.changes
        self.assertIsNotNone(changes)
        self.assertIn('样本', changes.resources_to_recheck)
        self.assertIn('总表', changes.resources_to_recheck)

    def test_sample_definition_back_into_code_fence(self):
        index = ReferenceIndex(self._fenced_sample_document())
        index.apply_edits([
            LineColumnEdit(7, 1, 8, 1, ''),
            LineColumnEdit(9, 1, 10, 1, ''),
        ])
        # Put the sample definition back under a fence.
        index.apply_edits([LineColumnEdit(7, 1, 7, 1, '```\n')])
        index.apply_edits([LineColumnEdit(9, 1, 9, 1, '```\n')])
        after = index.index
        self.assertEqual(
            [(item.label, item.destination) for item in after.references],
            [('总表', '/master')],
        )
        self.assertTrue(index.verify())
        self.assertTrue(index.verify(1))

    def test_definition_takeover_same_destination(self):
        document = (
            '段落 [证据][总表]\n'
            '\n'
            '[总表]: /same\n'
            '\n'
            '中间段落\n'
            '\n'
            '[总表]: /same\n'
        )
        index = ReferenceIndex(document)
        first = index.index.effective_definitions['总表']
        self.assertEqual(first.range.start.line, 3)

        index.apply_edits([LineColumnEdit(3, 1, 4, 1, '')])
        second = index.index.effective_definitions['总表']
        self.assertEqual(second.range.start.line, 6)
        self.assertNotEqual(first.identity, second.identity)
        changes = index.changes
        self.assertEqual(len(changes.changed_definitions), 1)
        change = changes.changed_definitions[0]
        self.assertEqual(change.label, '总表')
        self.assertFalse(change.destination_changed)
        self.assertTrue(change.effective_line_changed)
        self.assertIn('总表', changes.resources_to_recheck)
        self.assertTrue(index.verify())

    def test_removing_shadowed_definition_is_not_a_change(self):
        document = (
            '段落 [证据][总表]\n'
            '\n'
            '[总表]: /same\n'
            '\n'
            '中间段落\n'
            '\n'
            '[总表]: /same\n'
        )
        index = ReferenceIndex(document)
        index.apply_edits([LineColumnEdit(7, 1, 8, 1, '')])
        self.assertEqual(index.changes.changed_definitions, ())
        self.assertEqual(index.changes.resources_to_recheck, ())

    def test_line_shift_does_not_recheck_materials(self):
        document = (
            '段落 [甲][a]\n'
            '\n'
            '另一段 [乙][b]\n'
            '\n'
            '[a]: /a\n'
            '[b]: /b\n'
        )
        index = ReferenceIndex(document)
        index.apply_edits([LineColumnEdit(2, 1, 2, 1, '新增一行\n\n')])
        self.assertEqual(index.changes.changed_definitions, ())
        self.assertEqual(index.changes.resources_to_recheck, ())
        self.assertTrue(index.verify())

    def test_batched_edits_publish_one_version(self):
        document = (
            '段落 [甲][a]\n'
            '\n'
            '[a]: /a\n'
        )
        index = ReferenceIndex(document)
        index.apply_edits([
            (0, 0, '新增开头\n\n'),
            (len(document), len(document), '[b]: /b\n'),
        ])
        self.assertEqual(len(index.versions), 2)
        self.assertEqual(index.index.version, 1)
        self.assertIn('新增开头', index.text)
        self.assertIn('[b]: /b', index.text)
        self.assertTrue(index.verify())
        self.assertTrue(index.verify(0))

    def test_older_version_keeps_its_own_positions(self):
        document = '段落 [甲][a]\n\n[a]: /a\n'
        index = ReferenceIndex(document)
        index.apply_edits([(0, 0, '第一行\n\n第二行\n\n')])
        old = index.version(0)
        new = index.index
        self.assertEqual(old.references[0].range.start.line, 1)
        self.assertEqual(new.references[0].range.start.line, 5)
        self.assertEqual(old.text, document)

    def test_overlapping_edits_rejected_without_state_change(self):
        index = ReferenceIndex('abcdef\n')
        with self.assertRaises(ValueError):
            index.apply_edits([TextEdit(1, 4), TextEdit(3, 5)])
        self.assertEqual(len(index.versions), 1)
        self.assertEqual(index.text, 'abcdef\n')

    def test_pure_text_edits(self):
        index = ReferenceIndex('段落 [甲][a]\n\n[a]: /a\n')
        index.apply_edits([TextEdit(0, 0, '# 标题\n\n')])
        self.assertTrue(index.text.startswith('# 标题'))
        self.assertTrue(index.verify())


class TestIncrementalReuse(unittest.TestCase):
    @staticmethod
    def _document(paragraphs=20):
        parts = ['# 手册\n\n']
        for number in range(paragraphs):
            parts.append(
                '完全相同的段落 [甲{}][u{}]。\n\n'.format(number, number)
            )
        parts.append('```\n' * 1)
        parts.append('很长的代码栏\n' * 40)
        parts.append('```\n\n')
        for number in range(paragraphs):
            parts.append('[u{}]: /url/{}\n'.format(number, number))
        return ''.join(parts)

    def test_unrelated_blocks_are_reused(self):
        index = ReferenceIndex(self._document())
        total = index.index.stats.total_blocks
        index.apply_edits([(0, 0, '新增一行\n\n')])
        stats = index.index.stats
        self.assertEqual(stats.total_blocks, total + 1)
        self.assertLessEqual(stats.inline_parsed_blocks, 1)
        self.assertGreaterEqual(stats.reused_blocks, total)
        self.assertTrue(index.verify())

    def test_changed_block_only_reparsed(self):
        document = self._document()
        index = ReferenceIndex(document)
        target = next(
            number for number, line in enumerate(index.text.splitlines(), start=1)
            if line.startswith('完全相同的段落 [甲7]')
        )
        index.apply_edits([LineColumnEdit(
            target, 1, target, len(document.splitlines()[target - 1]) + 1,
            '完全相同的段落 [甲7][u7]，已改。\n',
        )])
        stats = index.index.stats
        self.assertEqual(stats.inline_parsed_blocks, 1)
        self.assertEqual(stats.reused_blocks, stats.total_blocks - 1)
        self.assertTrue(index.verify())

    def test_label_definition_change_invalidates_dependent_block(self):
        document = '段落 [甲][u5]\n\n[u5]: /old\n'
        index = ReferenceIndex(document)
        index.apply_edits([LineColumnEdit(3, 1, 3, 1, '[u5]: /new\n')])
        self.assertEqual(index.index.references[0].destination, '/new')
        self.assertTrue(index.verify())

    def test_code_fence_content_never_yields_references(self):
        document = (
            '段落 [甲][a]\n'
            '\n'
            '```\n'
            '[b]: /not-a-definition\n'
            '[x][a]\n'
            '```\n'
            '\n'
            '[a]: /a\n'
        )
        index = ReferenceIndex(document)
        self.assertEqual([item.label for item in index.index.references], ['a'])
        self.assertEqual([item.label for item in index.index.definitions], ['a'])

    def test_identical_paragraphs_reuse_all_but_changed_block(self):
        body = '完全相同的段落 [甲][u]。\n\n'
        document = '# 手册\n\n' + body * 12 + '[u]: /u\n'
        index = ReferenceIndex(document)
        starts = []
        offset = 0
        for number, line in enumerate(document.splitlines(keepends=True), start=1):
            if line.startswith('完全相同的段落') and number > 5:
                starts.append(offset)
            offset += len(line)
        target = starts[0]
        original = '完全相同的段落 [甲][u]。\n'
        replacement = '完全相同的段落 [甲][u]，这一行改了。\n'
        index.apply_edits([TextEdit(target, target + len(original), replacement)])
        stats = index.index.stats
        self.assertEqual(stats.inline_parsed_blocks, 1)
        self.assertEqual(stats.reused_blocks, stats.total_blocks - 1)
        self.assertTrue(index.verify())


class TestNormalizedDuplicateDefinitions(unittest.TestCase):
    def test_same_destination_but_new_effective_line(self):
        document = (
            '段落 [证据][总表]\n'
            '\n'
            '[总表]: /same\n'
            '\n'
            '中间段落\n'
            '\n'
            '[  总表  ]: /same\n'
        )
        index = ReferenceIndex(document)
        first = index.version(0)
        self.assertEqual(len(first.definitions), 2)
        self.assertEqual(first.definition_for('总表').range.start.line, 3)
        self.assertEqual(first.references[0].destination, '/same')

        # An edit elsewhere makes the trailing, differently spelled
        # definition the first (and therefore effective) one.
        index.apply_edits([LineColumnEdit(3, 1, 4, 1, '')])
        latest = index.index
        self.assertEqual(len(latest.definitions), 1)
        definition = latest.definition_for('总表')
        self.assertEqual(definition.destination, '/same')
        self.assertEqual(definition.range.start.line, 6)
        change = index.changes.changed_definitions[0]
        self.assertTrue(change.effective_line_changed)
        self.assertFalse(change.destination_changed)
        self.assertIn('总表', index.changes.resources_to_recheck)
        self.assertTrue(index.verify())
        self.assertTrue(index.verify(0))
        # The older version still reports its own source line.
        self.assertEqual(index.version(0).definitions[0].range.start.line, 3)


class TestPromptScenario(unittest.TestCase):
    document = (
        '# 验货手册\n'
        '\n'
        '[证据 [样本]][总表]\n'
        '\n'
        '## 甲\n'
        '\n'
        '[总表]: /master\n'
        '\n'
        '## 乙\n'
        '\n'
        '```\n'
        '[样本]: /sample\n'
        '```\n'
        '\n'
        '## 丙\n'
        '\n'
        '[  总表  ]: /master\n'
    )

    def test_three_saves(self):
        index = ReferenceIndex(self.document)

        # Initially the sample definition is inside a code fence, so the body
        # line is one outer link to 总表.
        first = index.index
        self.assertEqual(
            [(item.label, item.reference_kind) for item in first.references],
            [('总表', 'full')],
        )
        effective = first.effective_definitions
        self.assertEqual(sorted(effective), ['总表'])
        self.assertEqual(effective['总表'].range.start.line, 7)

        # Save 1: move the sample definition out of the code fence.  The same
        # body line now resolves to 样本, and 总表 becomes its own link.
        index.apply_edits([
            LineColumnEdit(11, 1, 12, 1, ''),
            LineColumnEdit(13, 1, 14, 1, ''),
        ])
        second = index.index
        self.assertEqual(
            [(item.label, item.reference_kind) for item in second.references],
            [('样本', 'shortcut'), ('总表', 'shortcut')],
        )
        self.assertEqual(
            {label: item.range.start.line
             for label, item in second.effective_definitions.items()},
            {'总表': 7, '样本': 11},
        )
        self.assertTrue(index.verify())

        # Save 2: remove the earlier 总表 definition.  The trailing definition
        # has the same destination, so only the effective line changes.
        index.apply_edits([LineColumnEdit(7, 1, 8, 1, '')])
        third = index.index
        effective = third.effective_definitions
        self.assertEqual(effective['总表'].destination, '/master')
        self.assertEqual(effective['总表'].range.start.line, 14)
        self.assertEqual(
            index.changes.changed_definitions[0].label, '总表')
        self.assertTrue(
            index.changes.changed_definitions[0].effective_line_changed)
        self.assertFalse(
            index.changes.changed_definitions[0].destination_changed)
        self.assertIn('总表', index.changes.resources_to_recheck)
        self.assertTrue(index.verify())

        # Save 3: move the sample definition back into a code fence.
        index.apply_edits([
            LineColumnEdit(9, 1, 9, 1, '```\n'),
            LineColumnEdit(11, 1, 11, 1, '```\n'),
        ])
        fourth = index.index
        self.assertEqual(
            [(item.label, item.reference_kind) for item in fourth.references],
            [('总表', 'full')],
        )
        self.assertEqual(sorted(fourth.effective_definitions), ['总表'])
        self.assertTrue(index.verify())

        # Every confirmed version still reads back its own positions.
        self.assertEqual(
            index.version(0).effective_definitions['总表'].range.start.line, 7)
        self.assertEqual(
            index.version(1).effective_definitions['总表'].range.start.line, 7)
        self.assertEqual(
            index.version(2).effective_definitions['总表'].range.start.line, 14)
        self.assertEqual(len(index.versions), 4)
        self.assertTrue(all(index.verify_all()))


if __name__ == '__main__':
    unittest.main()
