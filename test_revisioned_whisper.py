import os
import tempfile
import unittest

import whisper


class RevisionedWhisperTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, 'metric.wsp')
        self.now = 1_700_000_000
        whisper.create(
            self.path,
            [(1, 128), (4, 128), (8, 128)],
            xFilesFactor=1.0,
        )
        self.points = {
            40: 1.0, 41: 2.0, 42: 3.0, 43: 100.0,
            44: 4.0, 45: 4.0, 46: 4.0, 47: 4.0,
        }
        for timestamp, value in self.points.items():
            whisper.update(self.path, value, self.now + timestamp, now=self.now + 120)

    def tearDown(self):
        self.tempdir.cleanup()

    def value_at(self, step, offset):
        result = whisper.fetch(
            self.path,
            self.now + offset - step,
            self.now + offset + step,
            now=self.now + 120,
            archiveToSelect=step,
        )
        start, end, archive_step = result[0]
        index = (self.now + offset - (self.now % step)) - start
        return result[1][index // step]

    def test_bad_reading_disappears_and_cascades(self):
        self.assertAlmostEqual(self.value_at(4, 40), 26.5)
        self.assertAlmostEqual(self.value_at(8, 40), 15.25)

        result = whisper.apply_correction(
            self.path,
            timestamp=self.now + 43,
            revision=7,
            correction_id='correction-1',
            now=self.now + 120,
        )

        self.assertEqual(result.unresolved_levels, [])
        self.assertIsNone(self.value_at(4, 40))
        self.assertIsNone(self.value_at(8, 40))

        provenance = whisper.provenance(self.path)
        self.assertEqual(provenance.cells['4:{}'.format(self.now + 40)]['status'],
                         'insufficient_coverage')

    def test_replay_is_idempotent_and_preserves_new_seconds(self):
        kwargs = dict(
            timestamp=self.now + 43,
            revision=7,
            correction_id='correction-1',
            now=self.now + 120,
        )
        first = whisper.apply_correction(self.path, **kwargs)
        second = whisper.apply_correction(self.path, **kwargs)

        self.assertFalse(first.get('already_applied'))
        self.assertTrue(second.get('already_applied'))
        self.assertFalse(second.get('retracted_again'))

        whisper.update(self.path, 8.0, self.now + 60, now=self.now + 120)
        self.assertAlmostEqual(self.value_at(1, 60), 8.0)

        ledger = whisper._load_lineage(self.path)
        self.assertEqual(
            len([e for e in ledger['retractions'] if e['correction_id'] == 'correction-1']),
            1,
        )

    def test_manual_hourly_source_survives_derived_invalidation(self):
        source = {
            'observations': [{
                'timestamp': self.now + 40,
                'value': 42.0,
                'kind': 'manual_hourly',
                'archive': 2,
                'voucher_id': 'FIELD-1',
            }],
        }
        whisper.apply_correction(
            self.path,
            timestamp=self.now + 43,
            revision=7,
            correction_id='correction-manual',
            source_record=source,
            now=self.now + 120,
        )

        self.assertIsNone(self.value_at(4, 40))
        self.assertAlmostEqual(self.value_at(8, 40), 42.0)

        whisper.apply_correction(
            self.path,
            source_id='FIELD-1',
            correction_id='revoke-field',
            now=self.now + 120,
        )
        self.assertIsNone(self.value_at(8, 40))

    def test_versioned_result_and_provenance_match(self):
        result = whisper.apply_correction(
            self.path,
            timestamp=self.now + 43,
            revision=7,
            correction_id='versioned',
            now=self.now + 120,
        )
        looked_up = whisper.get_result(self.path, correction_id='versioned')
        self.assertEqual(looked_up['result_id'], result['result_id'])
        self.assertEqual(looked_up['version'], result['version'])
        self.assertIn('provenance', looked_up)

    def test_ring_overwrite_reports_unresolved_level(self):
        path = os.path.join(self.tempdir.name, 'ring.wsp')
        base = 1_700_000_100
        whisper.create(path, [(1, 4), (4, 16)], xFilesFactor=0.75)
        for offset in range(8, 12):
            whisper.update(path, float(offset), base + offset, now=base + 12)
        for offset in range(12, 16):
            whisper.update(path, float(offset), base + offset, now=base + 16)

        result = whisper.apply_correction(
            path,
            timestamp=base + 10,
            revision=1,
            correction_id='ring',
            now=base + 16,
        )

        self.assertIn(1, result.unresolved_levels)
        self.assertIn(4, result.unresolved_archives)
        self.assertEqual(result.unresolved_details[0]['reason'], 'insufficient_evidence')


if __name__ == '__main__':
    unittest.main()
