from __future__ import absolute_import, division, print_function

import random
import time
import unittest

import imagehash
from imagehash.archive import HashArchive, Match

A = '0000000000000000'
B = 'ffffffffffffffff'
C = 'aaaaaaaaaaaaaaaa'
AP = '0000000000000001'  # A with the lowest bit flipped
BP = 'fffffffffffffffe'  # B with the lowest bit flipped
CP = 'aaaaaaaaaaaaaaab'  # C with the lowest bit flipped


def to_multihash(segments):
	return imagehash.ImageMultiHash([imagehash.hex_to_hash('%016x' % value) for value in segments])


def brute_force_winner(records, crop_segments, tolerance):
	"""
	Reference implementation: hands the valid multi-hashes in ingestion
	order to ImageMultiHash.best_match, exactly like the specification.
	``records`` is a list of (record_id, version, segments | None).
	"""
	valid = [(record_id, version, segments) for record_id, version, segments in records if segments is not None]
	if not valid:
		return None
	crop = to_multihash(crop_segments)
	best_index = None
	best_diff = None
	for index, (record_id, version, segments) in enumerate(valid):
		diff = crop.__sub__(to_multihash(segments), hamming_cutoff=tolerance)
		if best_diff is None or diff < best_diff:
			best_diff = diff
			best_index = index
	record_id, version, _ = valid[best_index]
	return Match(record_id, version)


def sheet_records(sheet):
	"""Effective records of a sheet in ingestion order, for the reference implementation."""
	records = []
	for record_id in sheet._archive._order:
		version, fingerprint = sheet._effective(record_id)
		segments = None if fingerprint is None else sorted(fingerprint)
		records.append((record_id, version, segments))
	return records


class TestScenario(unittest.TestCase):
	"""The exact walkthrough from the task description."""

	def setUp(self):
		self.archive = HashArchive([
			('R01', '%s,%s' % (A, BP)),
			('R02', '%s,%s,%s' % (AP, B, CP)),
			('R03', '%s,%s,%s' % (A, BP, CP)),
		])
		self.crop1 = '%s,%s,%s,%s' % (A, A, B, C)
		self.crop2 = '%s,%s,%s,%s' % (A, B, B, C)

	def test_walkthrough(self):
		archive = self.archive
		sheet1 = archive.open_sheet()

		# Before any publication: crop1 at tolerance 0 picks R01.
		self.assertEqual(sheet1.query(self.crop1, [0]), {0: Match('R01', 0)})

		v1 = archive.publish({'R01': '%s,%s,%s' % (A, BP, C)})
		v2 = archive.publish({'R01': B, 'R02': A})
		self.assertEqual((v1, v2), (1, 2))

		# The old sheet adopted nothing: both tolerances pick R02.
		result = sheet1.query(self.crop2, [0, 1])
		self.assertEqual(result[0].record_id, 'R02')
		self.assertEqual(result[1].record_id, 'R02')

		# A sheet opened after the second publication picks R01 then R03.
		sheet2 = archive.open_sheet()
		result = sheet2.query(self.crop2, [0, 1])
		self.assertEqual(result[0], Match('R01', v2))
		self.assertEqual(result[1], Match('R03', 0))

		# The old sheet adopts only R01's second publication.
		sheet1.adopt('R01', v2)
		result = sheet1.query(self.crop1, [0, 1])
		self.assertEqual(result[0].record_id, 'R03')
		self.assertEqual(result[1].record_id, 'R03')
		result = sheet1.query(self.crop2, [0, 1])
		self.assertEqual(result[0], Match('R01', v2))
		self.assertEqual(result[1].record_id, 'R02')

		# Switch R01 to the first publication: both tolerances pick R01 from v1.
		sheet1.adopt('R01', v1)
		result = sheet1.query(self.crop2, [0, 1])
		self.assertEqual(result[0], Match('R01', v1))
		self.assertEqual(result[1], Match('R01', v1))

		# Cancel the adoption: back to the open-time versions.
		sheet1.cancel('R01')
		result = sheet1.query(self.crop1, [0, 1])
		self.assertEqual(result[0].record_id, 'R01')
		self.assertEqual(result[1].record_id, 'R03')

		# Withdrawing everything: new sheets find nothing, old sheets are untouched.
		archive.publish({'R01': None, 'R02': None, 'R03': None})
		sheet3 = archive.open_sheet()
		result = sheet3.query(self.crop1, range(25))
		self.assertEqual(set(result), set(range(25)))
		self.assertTrue(all(match is None for match in result.values()))
		result = sheet1.query(self.crop1, [0, 1])
		self.assertEqual(result[0].record_id, 'R01')
		self.assertEqual(result[1].record_id, 'R03')
		result = sheet2.query(self.crop2, [0, 1])
		self.assertEqual(result[0], Match('R01', v2))
		self.assertEqual(result[1], Match('R03', 0))

	def test_withdrawal_adoption_removes_record(self):
		archive = self.archive
		sheet = archive.open_sheet()
		version = archive.publish({'R01': None, 'R03': None})
		sheet.adopt('R01', version)
		sheet.adopt('R03', version)
		result = sheet.query(self.crop1, [0, 1])
		self.assertEqual(result[0].record_id, 'R02')
		self.assertEqual(result[1].record_id, 'R02')
		# Withdrawing the last record leaves the sheet without valid records.
		sheet.adopt('R02', archive.publish({'R02': None}))
		result = sheet.query(self.crop1, [0, 1])
		self.assertEqual(result, {0: None, 1: None})


class TestAgainstBestMatch(unittest.TestCase):
	"""Randomized conformance against ImageMultiHash.best_match."""

	def test_random_archive(self):
		rng = random.Random(20261125)
		universe = [rng.getrandbits(64) for _ in range(24)]
		# Keep some values close to each other so low tolerances hit.
		universe += [value ^ 1 for value in universe[:8]]

		def random_segments():
			return [rng.choice(universe) for _ in range(rng.randint(1, 6))]

		def hash_string(segments):
			return ','.join('%016x' % value for value in segments)

		record_segments = {}
		records = []
		for index in range(60):
			segments = random_segments()
			record_id = 'R%03d' % index
			record_segments[record_id] = segments
			records.append((record_id, hash_string(segments)))
		archive = HashArchive(records)

		sheets = [archive.open_sheet()]
		publications = []
		for _ in range(6):
			changes = {}
			for record_id in rng.sample(list(record_segments), rng.randint(1, 10)):
				if rng.random() < 0.3:
					changes[record_id] = None
				else:
					changes[record_id] = hash_string(random_segments())
			publications.append(archive.publish(changes))
			sheets.append(archive.open_sheet())

		# Random per-record adoptions and cancellations on each sheet.
		for sheet in sheets:
			for record_id in rng.sample(list(record_segments), 20):
				candidates = [v for v, _ in archive._history[record_id] if v > 0]
				if not candidates:
					continue
				action = rng.random()
				if action < 0.7:
					sheet.adopt(record_id, rng.choice(candidates))
				elif action < 0.8:
					sheet.cancel(record_id)

		crops = []
		for _ in range(6):
			crops.append([rng.choice(universe) for _ in range(rng.randint(1, 32))])
		# A crop that cannot hit anything at low tolerances.
		crops.append([rng.getrandbits(64) for _ in range(4)])

		tolerances = list(range(25))
		for sheet in sheets:
			reference = sheet_records(sheet)
			for crop in crops:
				crop_string = ','.join('%016x' % value for value in crop)
				result = sheet.query(crop_string, tolerances)
				self.assertEqual(set(result), set(tolerances))
				for tolerance in tolerances:
					expected = brute_force_winner(reference, crop, tolerance)
					self.assertEqual(
						result[tolerance], expected,
						'tolerance %d, crop %s' % (tolerance, crop_string),
					)

	def test_crop_as_image_multi_hash(self):
		archive = HashArchive([
			('R01', '%s,%s' % (A, BP)),
			('R02', '%s,%s,%s' % (AP, B, CP)),
		])
		sheet = archive.open_sheet()
		crop = to_multihash([0, 0, int(B, 16), int(C, 16)])
		self.assertEqual(sheet.query(crop, [0]), {0: Match('R01', 0)})


class TestApi(unittest.TestCase):
	def setUp(self):
		self.records = [
			('R01', '%s,%s' % (A, BP)),
			('R02', '%s,%s,%s' % (AP, B, CP)),
			('R03', '%s,%s,%s' % (A, BP, CP)),
		]

	def test_iterable_and_mapping_construction(self):
		from_mapping = HashArchive(dict(self.records))
		from_generator = HashArchive((pair for pair in self.records))
		sheet = from_generator.open_sheet()
		self.assertEqual(sheet.query('%s,%s' % (A, C), [0])[0].record_id, 'R01')
		self.assertEqual(len(from_mapping), 3)

	def test_single_tolerance_and_empty_tolerances(self):
		sheet = HashArchive(self.records).open_sheet()
		result = sheet.query('%s,%s' % (A, C), 0)
		self.assertEqual(result, {0: Match('R01', 0)})
		self.assertEqual(sheet.query('%s,%s' % (A, C), []), {})

	def test_invalid_input(self):
		sheet = HashArchive(self.records).open_sheet()
		with self.assertRaises(ValueError):
			sheet.query(A, [25])
		with self.assertRaises(ValueError):
			sheet.query(A, [-1])
		with self.assertRaises(ValueError):
			sheet.query('', [0])
		with self.assertRaises(ValueError):
			sheet.query(','.join([A] * 33), [0])
		with self.assertRaises(ValueError):
			HashArchive([('R01', A), ('R01', B)])
		with self.assertRaises(ValueError):
			HashArchive(self.records).publish({'R99': A})

	def test_adoption_validation(self):
		archive = HashArchive(self.records, review_window=1)
		sheet = archive.open_sheet()
		v1 = archive.publish({'R01': B})
		v2 = archive.publish({'R02': A})
		with self.assertRaises(KeyError):
			sheet.adopt('R99', v1)
		with self.assertRaises(ValueError):
			sheet.adopt('R02', v1)  # publication v1 did not touch R02
		sheet.adopt('R02', v2)  # the latest publication is still adoptable
		archive.publish({'R03': A})
		with self.assertRaises(ValueError):
			sheet.adopt('R01', v1)  # no longer within the review window
		sheet.cancel('R01')  # cancel without adoption is a no-op

	def test_empty_archive(self):
		sheet = HashArchive([]).open_sheet()
		self.assertEqual(sheet.query(A, [0, 1]), {0: None, 1: None})

	def test_no_segment_hits_picks_earliest_valid_record(self):
		sheet = HashArchive(self.records).open_sheet()
		far_crop = '5555555555555555,5555555555555556'
		self.assertEqual(sheet.query(far_crop, [0]), {0: Match('R01', 0)})


class TestPerformance(unittest.TestCase):
	"""Smoke test: large archive, many publications, heavy adoptions."""

	def test_large_archive(self):
		rng = random.Random(1109)
		record_count = 100000
		value_count = 2000
		universe = [rng.getrandbits(64) for _ in range(value_count)]
		# Common template segments covering most of the archive.
		templates = [universe[:4], universe[4:8]]

		records = []
		for index in range(record_count):
			if index % 10 < 6:
				segments = list(rng.choice(templates))
			else:
				segments = [rng.choice(universe) for _ in range(rng.randint(1, 8))]
			records.append(('R%06d' % index, ','.join('%016x' % value for value in segments)))

		start = time.monotonic()
		archive = HashArchive(records)
		load_seconds = time.monotonic() - start

		# Thousands of publications, each touching a small batch of records.
		publications = []
		start = time.monotonic()
		for batch in range(2000):
			changes = {}
			for index in range(batch * 25, batch * 25 + 25):
				changes['R%06d' % index] = '%016x' % rng.choice(universe)
			publications.append(archive.publish(changes))
		publish_seconds = time.monotonic() - start

		sheet = archive.open_sheet()
		# Adopt corrections for half of the records, spread over all publications.
		start = time.monotonic()
		for batch, version in enumerate(publications):
			for index in range(batch * 25, batch * 25 + 25, 2):
				sheet.adopt('R%06d' % index, version)
		adopt_seconds = time.monotonic() - start

		crop = [rng.choice(universe) for _ in range(32)]
		crop_string = ','.join('%016x' % value for value in crop)
		tolerances = list(range(25))
		start = time.monotonic()
		result = sheet.query(crop_string, tolerances)
		query_seconds = time.monotonic() - start

		self.assertEqual(set(result), set(tolerances))

		# Independent reference: score every valid record directly.
		import numpy
		values = sorted(set(universe))
		value_index = {value: index for index, value in enumerate(values)}
		distances = numpy.bitwise_count(
			numpy.array(values, dtype=numpy.uint64)[:, None] ^ numpy.array(crop, dtype=numpy.uint64)[None, :]
		).astype(numpy.int64)
		tolerance_array = numpy.array(tolerances, dtype=numpy.int64)
		best = {tolerance: None for tolerance in tolerances}
		for position, record_id in enumerate(archive._order):
			version, fingerprint = sheet._effective(record_id)
			if fingerprint is None:
				continue
			mins = numpy.sort(distances[[value_index[value] for value in fingerprint]].min(axis=0))
			prefix = numpy.concatenate(([0], numpy.cumsum(mins)))
			matches = numpy.searchsorted(mins, tolerance_array, side='right')
			sums = prefix[matches]
			for column, tolerance in enumerate(tolerances):
				candidate = (int(matches[column]), -int(sums[column]), -position)
				if best[tolerance] is None or candidate > best[tolerance][0]:
					best[tolerance] = (candidate, Match(record_id, version))
		for tolerance in tolerances:
			self.assertEqual(result[tolerance], best[tolerance][1])

		# Generous bounds to catch accidental per-record work.
		self.assertLess(load_seconds, 30)
		self.assertLess(publish_seconds, 30)
		self.assertLess(adopt_seconds, 30)
		self.assertLess(query_seconds, 30)


if __name__ == '__main__':
	unittest.main()
