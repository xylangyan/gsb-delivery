# -*- coding: utf-8 -*-
"""Per-side reconciliation results (the cells of the account grid)."""


class _Singleton:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return self.label

    def __reduce__(self):
        return (self.__class__, ())


class Absent(_Singleton):
    """No surviving supporting bundle on this side."""

    label = '缺席'


class Evidenced(_Singleton):
    """At least one supporting bundle, but the registry table cannot segment."""

    label = '有凭据但未分段'


ABSENT = Absent()
UNSEGMENTED = Evidenced()


class Segmented:
    """A full registration result.

    ``reg_prefix`` is the complete registration prefix (e.g. ``978-99930``),
    ``group`` the group number (``99930``) and ``publisher`` the publisher
    segment.  An empty publisher segment (publisher length 0) is still a
    regular segmentation result, distinct from :class:`Evidenced`.
    """

    __slots__ = ('reg_prefix', 'group', 'publisher')

    def __init__(self, reg_prefix, group, publisher):
        self.reg_prefix = reg_prefix
        self.group = group
        self.publisher = publisher

    def __eq__(self, other):
        return (isinstance(other, Segmented)
                and self.reg_prefix == other.reg_prefix
                and self.group == other.group
                and self.publisher == other.publisher)

    def __hash__(self):
        return hash((self.reg_prefix, self.group, self.publisher))

    def __repr__(self):
        publisher = self.publisher if self.publisher else '∅'
        return '分段(%s/%s/%s)' % (self.reg_prefix, self.group, publisher)
