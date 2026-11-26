# -*- coding: utf-8 -*-
"""Registration tables segmented with the exact isbnlib ``msk`` convention.

isbnlib's :func:`isbnlib._msk.msk` locates the registration group with a
sliding window over keys shaped ``978-99930`` and then matches the seven-digit
reading (the up to seven digits right after the group, right-padded with
zeros) against inclusive ``(lo, hi, publisher_length)`` rules.  Each side of
the reconciliation owns an independent table; correction work orders may
replace the rules of a whole registration group without touching anything
else.
"""

from .results import Segmented, UNSEGMENTED


class RegistryTable:
    """One side's replaceable registration table."""

    def __init__(self, name):
        self.name = name
        # group key ('978-99930') -> tuple of (lo, hi, publisher_length)
        self._ranges = {}

    def copy(self):
        clone = RegistryTable(self.name)
        clone._ranges = {key: tuple(rules) for key, rules in self._ranges.items()}
        return clone

    def has_group(self, reg_prefix):
        return reg_prefix in self._ranges

    def groups(self):
        return tuple(self._ranges)

    def set_group_rules(self, reg_prefix, rules):
        """Replace every rule of a registration group.

        ``rules`` is a sequence of ``(lo, hi, publisher_length)`` triples or
        of ``(reading, publisher_length)`` pairs, where a bare reading is a
        single seven-digit point rule.
        """
        normalized = []
        for rule in rules:
            if len(rule) == 2:
                reading, length = rule
                lo = hi = int(reading)
            elif len(rule) == 3:
                lo, hi, length = rule
                lo, hi = int(lo), int(hi)
            else:
                raise ValueError('malformed rule %r' % (rule,))
            if lo > hi or not 0 <= length <= 7:
                raise ValueError('malformed rule %r' % (rule,))
            normalized.append((lo, hi, int(length)))
        if not normalized:
            raise ValueError('refusing to install an empty rule set; '
                             'use drop_group() to withdraw a group')
        self._ranges[reg_prefix] = tuple(sorted(normalized))

    def set_reading_rule(self, reg_prefix, reading, publisher_length):
        """Install a single point rule, replacing the whole group's rules."""
        self.set_group_rules(reg_prefix, ((int(reading), int(reading),
                                           int(publisher_length)),))

    def drop_group(self, reg_prefix):
        del self._ranges[reg_prefix]

    def _locate_group(self, isbn13):
        """Mirror msk's sliding window; return (group_key, cur) or None."""
        cur = 3
        group = isbn13[0:3] + '-' + isbn13[cur:cur + 1]
        for _ in range(6):
            if group in self._ranges:
                return group, cur
            cur += 1
            group += isbn13[cur:cur + 1]
        return None

    def publisher_length_for(self, isbn13):
        """Publisher length under this table, or ``None`` when unsegmented."""
        located = self._locate_group(isbn13)
        if located is None:
            return None
        group_key, cur = located
        sevens = isbn13[cur + 1:cur + 8].ljust(7, '0')
        reading = int(sevens)
        for lo, hi, length in self._ranges[group_key]:
            if lo <= reading <= hi:
                return length
        return None

    def segment(self, isbn13):
        """Segment a canonical ISBN-13 or return the unsegmented sentinel."""
        located = self._locate_group(isbn13)
        if located is None:
            return UNSEGMENTED
        group_key, cur = located
        sevens = isbn13[cur + 1:cur + 8].ljust(7, '0')
        reading = int(sevens)
        publisher_length = None
        for lo, hi, length in self._ranges[group_key]:
            if lo <= reading <= hi:
                publisher_length = length
                break
        if publisher_length is None:
            return UNSEGMENTED
        group = group_key.split('-', 1)[1]
        publisher = isbn13[cur + 1:cur + 1 + publisher_length]
        return Segmented(group_key, group, publisher)
