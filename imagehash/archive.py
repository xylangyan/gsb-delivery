"""
Fast archive lookup for crop-resistant multi-segment hashes.

A quality-control sheet (:class:`QCSheet`) is opened on a :class:`HashArchive`
and can then answer, for any number of crops and any set of integer
tolerances in ``0..24``, which archive record is the best match for the
crop.  The winner is exactly the record that
``imagehash.ImageMultiHash.best_match`` would return when handed the
sheet's valid multi-hashes in their original ingestion order (including
the case where no segment hits at all), but it is found without scoring
records one by one:

* records are grouped by their set of distinct segment values, so
  records sharing a fingerprint are scored once, not per record;
* all distinct segment values (a small universe during the review
  period) are hashed against the crop with vectorized numpy popcounts;
* every requested tolerance is answered from the same per-fingerprint
  sorted min-distance vector, so one pass covers a whole set of
  tolerances.

Publications (corrections and withdrawals of individual records) and
per-record adoptions on a sheet are incremental: they only touch the
records they are given and never rescan the rest of the archive.
"""

from __future__ import absolute_import, division, print_function

from bisect import bisect_left, insort
from collections import OrderedDict, namedtuple

import numpy

__all__ = ['HashArchive', 'QCSheet', 'Match']

SEGMENT_BITS = 64
MAX_CROP_SEGMENTS = 32
MIN_TOLERANCE = 0
MAX_TOLERANCE = 24

# A winning record is returned together with the archive publication
# version whose data the sheet actually used for it.
Match = namedtuple('Match', ['record_id', 'version'])


def _parse_segment(segment):
	# type: (object) -> int
	"""
	Parse one 64 bit segment hash, given either as an int or as a hex
	string (as produced by ``str(ImageHash)``).
	"""
	if isinstance(segment, bool):
		raise ValueError('Segment hashes must be 64 bit values, not booleans.')
	if isinstance(segment, int):
		value = segment
	else:
		text = str(segment).strip()
		if text.lower().startswith('0x'):
			text = text[2:]
		value = int(text, 16)
	if not 0 <= value < 1 << SEGMENT_BITS:
		raise ValueError('Segment hash {!r} does not fit into {} bits.'.format(segment, SEGMENT_BITS))
	return value


def _parse_hash_string(hash_string):
	# type: (object) -> list[int]
	"""
	Parse a comma separated string of 64 bit segment hashes (or any
	iterable of segments) into a list of ints.
	"""
	if isinstance(hash_string, str):
		parts = hash_string.split(',')
	else:
		parts = list(hash_string)
	values = [_parse_segment(part) for part in parts if str(part).strip() != '']
	if not values:
		raise ValueError('A record hash must contain at least one segment.')
	return values


def _crop_segments(crop):
	# type: (object) -> list[int]
	"""
	Extract the ordered list of 64 bit segment values of a crop.  Accepts
	an ``imagehash.ImageMultiHash`` (as returned by
	``imagehash.crop_resistant_hash``), a comma separated string, or an
	iterable of segments.  Duplicate segments are kept: they are counted
	individually by the matching logic.
	"""
	if hasattr(crop, 'segment_hashes'):
		parts = [str(segment) for segment in crop.segment_hashes]
	elif isinstance(crop, str):
		parts = crop.split(',')
	else:
		parts = list(crop)
	values = [_parse_segment(part) for part in parts]
	if not 1 <= len(values) <= MAX_CROP_SEGMENTS:
		raise ValueError('A crop must have between 1 and {} segments.'.format(MAX_CROP_SEGMENTS))
	return values


def _tolerances(tolerances):
	# type: (object) -> list[int]
	if isinstance(tolerances, bool):
		raise ValueError('Tolerances must be integers in {}..{}.'.format(MIN_TOLERANCE, MAX_TOLERANCE))
	if isinstance(tolerances, int):
		tolerances = [tolerances]
	result = []
	for tolerance in tolerances:
		if isinstance(tolerance, bool) or not isinstance(tolerance, int):
			raise ValueError('Tolerance {!r} is not an integer.'.format(tolerance))
		if not MIN_TOLERANCE <= tolerance <= MAX_TOLERANCE:
			raise ValueError('Tolerance {!r} is outside {}..{}.'.format(tolerance, MIN_TOLERANCE, MAX_TOLERANCE))
		result.append(tolerance)
	return result


class HashArchive:
	"""
	Archive of multi-segment record hashes.

	``records`` is an iterable of ``(record_id, hash_string)`` pairs (or a
	mapping) in the original ingestion order.  ``hash_string`` is the
	comma separated 64 bit segment hashes of the record.  The initial
	load is publication version 0; every :meth:`publish` call creates the
	next version.

	``review_window`` optionally limits how many publications back a
	sheet may still adopt: a publication ``v`` is adoptable while
	``archive.version - v <= review_window``.  ``None`` (the default)
	keeps every publication adoptable.
	"""

	def __init__(self, records, review_window=None):
		# type: (object, int | None) -> None
		if hasattr(records, 'items'):
			records = records.items()
		self._order = []  # type: list
		self._position = {}  # type: dict
		self._history = {}  # type: dict
		self._current = {}  # type: dict
		self._version = 0
		self.review_window = review_window
		for record_id, hash_string in records:
			if record_id in self._position:
				raise ValueError('Duplicate record id {!r}.'.format(record_id))
			fingerprint = frozenset(_parse_hash_string(hash_string))
			self._position[record_id] = len(self._order)
			self._order.append(record_id)
			self._history[record_id] = [(0, fingerprint)]
			self._current[record_id] = (0, fingerprint)

	def __len__(self):
		return len(self._order)

	@property
	def version(self):
		# type: () -> int
		"""The latest publication version (0 for the initial load)."""
		return self._version

	def publish(self, changes):
		# type: (object) -> int
		"""
		Publish a batch of corrections and/or withdrawals.

		``changes`` maps record ids to their new comma separated segment
		hashes, or to ``None`` to withdraw the record.  Only the given
		records are touched.  Returns the new publication version.
		"""
		if hasattr(changes, 'items'):
			changes = changes.items()
		self._version += 1
		version = self._version
		for record_id, hash_string in changes:
			if record_id not in self._current:
				raise ValueError('Unknown record id {!r}.'.format(record_id))
			fingerprint = None if hash_string is None else frozenset(_parse_hash_string(hash_string))
			self._history[record_id].append((version, fingerprint))
			self._current[record_id] = (version, fingerprint)
		return version

	def open_sheet(self):
		# type: () -> QCSheet
		"""Open a quality-control sheet on the currently published state."""
		return QCSheet(self)


class QCSheet:
	"""
	A quality-control sheet.

	Opened on the archive state published at open time; later
	publications never change it implicitly.  Individual corrections or
	withdrawals that are still within the review period can be adopted
	per record id with :meth:`adopt`, switched to another publication, or
	reverted to the open-time version with :meth:`cancel`.
	"""

	_CACHE_SIZE = 8

	def __init__(self, archive):
		# type: (HashArchive) -> None
		self._archive = archive
		self._base_version = archive.version
		# Record state as published when the sheet was opened: id -> (version, fingerprint | None)
		self._base = dict(archive._current)
		# Adopted publications: id -> (version, fingerprint | None)
		self._adopted = {}  # type: dict
		# Valid records grouped by fingerprint: fingerprint -> [(position, record_id, version), ...]
		# Each group is kept sorted by ingestion position, so the first
		# entry is the group's tie-breaking representative.
		self._groups = {}  # type: dict
		for position, record_id in enumerate(archive._order):
			version, fingerprint = self._base[record_id]
			if fingerprint is None:
				continue
			self._groups.setdefault(fingerprint, []).append((position, record_id, version))
		# Per-crop LRU cache: crop key -> (first entries, orders, prefixes, positions)
		self._cache = OrderedDict()  # type: OrderedDict

	@property
	def base_version(self):
		# type: () -> int
		"""The archive publication version this sheet was opened on."""
		return self._base_version

	def _effective(self, record_id):
		# type: (object) -> tuple
		return self._adopted.get(record_id, self._base[record_id])

	def adopt(self, record_id, version):
		# type: (object, int) -> None
		"""
		Adopt, for this record id only, the correction or withdrawal that
		publication ``version`` made to it.  The publication must still be
		within the review period.  Only this record is affected, even if
		the publication changed other records as well.
		"""
		archive = self._archive
		if record_id not in archive._position:
			raise KeyError('Unknown record id {!r}.'.format(record_id))
		state = None
		for published_version, fingerprint in archive._history[record_id]:
			if published_version == version:
				state = (published_version, fingerprint)
				break
		if state is None:
			raise ValueError('Publication {} did not change record {!r}.'.format(version, record_id))
		if archive.review_window is not None and archive.version - version > archive.review_window:
			raise ValueError('Publication {} is no longer within the review period.'.format(version))
		self._replace_state(record_id, state)
		self._adopted[record_id] = state

	def cancel(self, record_id):
		# type: (object) -> None
		"""Cancel the adoption for a record id, restoring the open-time version."""
		if record_id not in self._adopted:
			return
		self._replace_state(record_id, self._base[record_id])
		del self._adopted[record_id]

	def _replace_state(self, record_id, state):
		# type: (object, tuple) -> None
		old_version, old_fingerprint = self._effective(record_id)
		new_version, new_fingerprint = state
		if old_version == new_version and old_fingerprint == new_fingerprint:
			return
		position = self._archive._position[record_id]
		if old_fingerprint is not None:
			group = self._groups[old_fingerprint]
			index = bisect_left(group, (position, record_id, -1))
			del group[index]
			if not group:
				del self._groups[old_fingerprint]
		if new_fingerprint is not None:
			insort(self._groups.setdefault(new_fingerprint, []), (position, record_id, new_version))
		self._cache.clear()

	def _score_table(self, segments):
		# type: (list[int]) -> tuple
		"""
		Build (and cache) the per-fingerprint score tables for a crop:
		for every fingerprint group, the sorted vector of per-crop-segment
		minimum hamming distances and its prefix sums.  All tolerances are
		answered from these.
		"""
		key = tuple(sorted(segments))
		cached = self._cache.get(key)
		if cached is not None:
			self._cache.move_to_end(key)
			return cached
		# Index the distinct segment values used by this sheet's records.
		values = {}
		for fingerprint in self._groups:
			for value in fingerprint:
				if value not in values:
					values[value] = len(values)
		value_array = numpy.array(list(values), dtype=numpy.uint64)
		crop_array = numpy.array(segments, dtype=numpy.uint64)
		# Hamming distances between every distinct value and every crop segment.
		distances = numpy.bitwise_count(value_array[:, None] ^ crop_array[None, :])
		first_entries = []
		orders = []
		prefixes = []
		for fingerprint, entries in self._groups.items():
			rows = distances[[values[value] for value in fingerprint]]
			order = numpy.sort(rows.min(axis=0).astype(numpy.int64))
			first_entries.append(entries[0])
			orders.append(order)
			prefixes.append(numpy.concatenate(([0], numpy.cumsum(order))))
		positions = numpy.array([entry[0] for entry in first_entries], dtype=numpy.int64)
		cached = (first_entries, orders, prefixes, positions)
		self._cache[key] = cached
		if len(self._cache) > self._CACHE_SIZE:
			self._cache.popitem(last=False)
		return cached

	def query(self, crop, tolerances):
		# type: (object, object) -> dict
		"""
		Find the winning record for every given tolerance.

		``crop`` is an ``ImageMultiHash`` (e.g. from
		``imagehash.crop_resistant_hash``), a comma separated string, or
		an iterable of segments with 1 to 32 values.  ``tolerances`` is
		any iterable of integers in 0..24 (a single int is accepted too).

		Returns a dict mapping each tolerance to a ``Match`` namedtuple
		``(record_id, version)`` with the winning record id and the
		archive publication version this sheet actually uses for it, or
		``None`` for every tolerance if the sheet has no valid records.
		The winner is the same record that
		``ImageMultiHash.best_match`` would pick from the sheet's valid
		hashes in original ingestion order.
		"""
		segments = _crop_segments(crop)
		tolerance_list = sorted(set(_tolerances(tolerances)))
		result = {tolerance: None for tolerance in tolerance_list}
		if not tolerance_list or not self._groups:
			return result
		first_entries, orders, prefixes, positions = self._score_table(segments)
		tolerance_array = numpy.array(tolerance_list, dtype=numpy.int64)
		# matches[f, t] = number of crop segments hitting fingerprint f at tolerance t
		matches = numpy.stack([numpy.searchsorted(order, tolerance_array, side='right') for order in orders])
		sums = numpy.stack([prefix[match] for prefix, match in zip(prefixes, matches)])
		# ImageMultiHash.__sub__ returns N - matches + sum / (matches * 64)
		# with sum <= tolerance * matches <= 24 * matches, so a higher
		# match count always wins, and equal counts are decided by the
		# smaller sum; remaining ties go to the earliest ingestion
		# position.  This composite score encodes exactly that order.
		sum_bound = MAX_TOLERANCE * MAX_CROP_SEGMENTS + 1
		position_bound = len(self._archive._order) + 1
		scores = matches * (sum_bound * position_bound) - sums * position_bound - positions[:, None]
		winners = scores.argmax(axis=0)
		for column, tolerance in enumerate(tolerance_list):
			position, record_id, version = first_entries[winners[column]]
			result[tolerance] = Match(record_id, version)
		return result
