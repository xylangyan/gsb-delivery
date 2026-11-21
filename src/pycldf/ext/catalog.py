"""
Trace historical catalog identities to augment old CLDF wordlists with standard codes.

Old wordlists often record codes of a standard catalog (e.g. ISO 639-3) that have
meanwhile been retired, split, renamed or re-assigned. This module models such a
catalog as

- versioned :class:`CatalogEntry` records, tying a stable *identity* to the code and
  name it carries during a validity interval ``[since, until)``, and
- :class:`Revision` relations (``split`` and ``rename``) between identities, taking
  effect at a given catalog version.

Resolution proceeds in two steps:

1. The catalog entry is identified that the recorded code denoted at the
   collection year of a language.
2. From this identity, evidenced inheritance relations are followed forward to the
   target catalog version. Region evidence may narrow down the successors of a
   split, but only as far as it matches; identities are never merged based on
   matching codes or names.

If a single final entry cannot be determined, all final candidates that are still
supported by evidence are listed.
"""
import dataclasses
import pathlib
import typing

from pycldf.dataset import Dataset

__all__ = [
    'CatalogEntry',
    'Revision',
    'TraceStep',
    'Catalog',
    'Resolution',
    'STATUS_RESOLVED',
    'STATUS_AMBIGUOUS',
    'STATUS_UNKNOWN_CODE',
    'STATUS_MISSING_CODE',
    'resolve',
    'augment_wordlist',
]

#: Unique final entry at the target version.
STATUS_RESOLVED = 'resolved'
#: Several final entries cannot be excluded.
STATUS_AMBIGUOUS = 'ambiguous'
#: The recorded code is not part of the catalog at the collection year.
STATUS_UNKNOWN_CODE = 'unknown-code'
#: No code was recorded for the language.
STATUS_MISSING_CODE = 'missing-code'


@dataclasses.dataclass(frozen=True)
class CatalogEntry:
    """
    A versioned catalog record: the code and name a stable identity carries \
    during the validity interval ``[since, until)``.
    """
    id: str
    identity: str
    code: str
    name: str
    since: typing.Any
    until: typing.Optional[typing.Any] = None
    regions: frozenset = dataclasses.field(default_factory=frozenset)

    def __post_init__(self):
        object.__setattr__(self, 'regions', frozenset(self.regions or ()))


@dataclasses.dataclass(frozen=True)
class Revision:
    """
    An evidenced inheritance relation between catalog identities, taking effect \
    at ``version``. ``split`` maps one identity to several successors, ``rename`` \
    preserves the identity (changed name and/or code only).
    """
    kind: str  # 'split' or 'rename'
    predecessor: str
    successors: tuple
    version: typing.Any

    def __post_init__(self):
        object.__setattr__(self, 'successors', tuple(self.successors))


@dataclasses.dataclass(frozen=True)
class TraceStep:
    """One inheritance relation traversed while tracing an identity."""
    kind: str
    predecessor: str
    successors: tuple
    version: typing.Any

    def __str__(self):
        return '{}:{}>{}@{}'.format(self.kind, self.predecessor, '/'.join(self.successors),
                                    self.version)


class Catalog:
    """
    A versioned standard catalog: entries, revisions and the means to trace \
    identities across versions.
    """

    def __init__(self,
                 entries: typing.Iterable[CatalogEntry],
                 revisions: typing.Iterable[Revision]):
        self.entries = list(entries)
        self.revisions = sorted(revisions, key=lambda rev: rev.version)

    def entry_for_code(self, code: str, version) -> typing.Optional[CatalogEntry]:
        """
        The catalog entry denoted by ``code`` at ``version``, or `None`.
        """
        matches = [e for e in self.entries
                   if e.code == code and e.since <= version and
                   (e.until is None or version < e.until)]
        if not matches:
            return None
        return max(matches, key=lambda e: e.since)

    def record_for(self, identity: str, version) -> typing.Optional[CatalogEntry]:
        """
        The record of ``identity`` valid at ``version``; falls back to the latest \
        earlier record if the identity has no record covering ``version``.
        """
        records = [e for e in self.entries if e.identity == identity]
        valid = [e for e in records
                 if e.since <= version and (e.until is None or version < e.until)]
        if valid:
            return max(valid, key=lambda e: e.since)
        earlier = [e for e in records if e.since <= version]
        return max(earlier, key=lambda e: e.since) if earlier else None

    def trace(self,
              identity: str,
              target,
              region: typing.Optional[str] = None,
              after=None) -> typing.Tuple[typing.List[TraceStep], typing.List[str]]:
        """
        Follow evidenced inheritance relations from ``identity`` up to catalog \
        version ``target``.

        :param region: Region evidence used to narrow down the successors of a \
        split. Only successors marked with the region are followed; if the \
        evidence matches no (or several) successors, all successors are followed.
        :param after: Only relations taking effect after this version (typically \
        the collection year) are traversed.
        :return: The traversed relations and the final candidate identities.
        """
        path, candidates, done = [], [], set()
        frontier = [identity]
        while frontier:
            current = frontier.pop()
            revisions = [rev for rev in self.revisions
                         if rev.predecessor == current and rev.version <= target
                         and (after is None or rev.version > after)
                         and id(rev) not in done]
            if not revisions:
                candidates.append(current)
                continue
            for rev in revisions:
                done.add(id(rev))
                if rev.kind == 'split':
                    chosen = list(rev.successors)
                    if region:
                        marked = [succ for succ in rev.successors
                                  if self.record_for(succ, rev.version)
                                  and region in self.record_for(succ, rev.version).regions]
                        if marked:
                            chosen = marked
                else:  # rename: identity is preserved.
                    chosen = list(rev.successors)
                path.append(TraceStep(rev.kind, rev.predecessor, tuple(chosen), rev.version))
                frontier.extend(chosen)
        return path, candidates


@dataclasses.dataclass
class Resolution:
    """The resolution result for one language row of a wordlist."""
    language_id: str
    status: str
    collection_entry: typing.Optional[str]
    path: typing.List[TraceStep]
    candidates: typing.List[CatalogEntry]

    @property
    def code(self) -> typing.Optional[str]:
        """The standard code, if a unique final entry was determined."""
        if self.status == STATUS_RESOLVED and self.candidates:
            return self.candidates[0].code
        return None

    @property
    def trace(self) -> str:
        """The traversed relations, formatted for tabular output."""
        return '; '.join(str(step) for step in self.path)

    @property
    def candidate_list(self) -> str:
        """The final candidates (``identity:code``), formatted for tabular output."""
        return ';'.join('{}:{}'.format(c.identity, c.code or '') for c in self.candidates)


def resolve(catalog: Catalog,
            rows: typing.Iterable[dict],
            target,
            get_code: typing.Callable[[dict], typing.Optional[str]],
            get_year: typing.Callable[[dict], typing.Any],
            get_region: typing.Optional[typing.Callable[[dict], typing.Optional[str]]] = None,
            get_id: typing.Callable[[dict], str] = lambda row: row['id']) \
        -> typing.Dict[str, Resolution]:
    """
    Resolve the standard codes of language ``rows`` against catalog version ``target``.

    :return: A mapping from original language ID to :class:`Resolution`. The result \
    does not depend on the order of ``rows``.
    """
    results = {}
    for row in rows:
        lid = get_id(row)
        code = get_code(row)
        if not code:
            results[lid] = Resolution(lid, STATUS_MISSING_CODE, None, [], [])
            continue
        year = get_year(row)
        entry = catalog.entry_for_code(code, year)
        if entry is None:
            results[lid] = Resolution(lid, STATUS_UNKNOWN_CODE, None, [], [])
            continue
        region = get_region(row) if get_region else None
        path, identities = catalog.trace(entry.identity, target,
                                         region=region or None, after=year)
        candidates = []
        for identity in sorted(set(identities)):
            record = catalog.record_for(identity, target)
            if record is not None:
                candidates.append(record)
        status = STATUS_RESOLVED if len(candidates) == 1 else STATUS_AMBIGUOUS
        results[lid] = Resolution(lid, status, entry.identity, path, candidates)
    return results


def augment_wordlist(dataset,
                     outdir,
                     catalog: Catalog,
                     target,
                     *,
                     code_column: str,
                     year_column: str,
                     region_column: typing.Optional[str] = None,
                     mdname: typing.Optional[str] = None) -> typing.Dict[str, Resolution]:
    """
    Copy a CLDF wordlist to ``outdir``, adding standard catalog codes to the \
    LanguageTable.

    The original data - in particular the FormTable, foreign keys and the recorded \
    original codes - is left untouched; results are written to the separate columns \
    ``Collection_Entry``, ``Trace``, ``Standard_Code``, ``Standard_Candidates`` and \
    ``Resolution``.

    :param dataset: A :class:`~pycldf.dataset.Dataset` or the path of its metadata.
    :param target: The catalog version to resolve against.
    :return: A mapping from original language ID to :class:`Resolution`.
    """
    if not isinstance(dataset, Dataset):
        dataset = Dataset.from_metadata(dataset)
    md = dataset.copy(pathlib.Path(outdir), mdname=mdname)
    out = Dataset.from_metadata(md)

    table = out['LanguageTable']
    id_column = table.tableSchema.primaryKey
    id_column = id_column[0] if isinstance(id_column, list) else id_column
    rows = list(table.iterdicts())

    def to_version(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    results = resolve(
        catalog, rows, target,
        get_id=lambda row: row[id_column],
        get_code=lambda row: row.get(code_column),
        get_year=lambda row: to_version(row.get(year_column)),
        get_region=(lambda row: row.get(region_column)) if region_column else None)

    out.add_columns(
        'LanguageTable',
        {'name': 'Collection_Entry',
         'dc:description': 'Catalog entry the recorded code denoted at collection time.'},
        {'name': 'Trace',
         'dc:description': 'Inheritance relations traversed towards the target version.'},
        {'name': 'Standard_Code',
         'dc:description': 'Standard code at the target catalog version, if unique.'},
        {'name': 'Standard_Candidates',
         'dc:description': 'Final candidates (identity:code) that cannot be excluded.'},
        {'name': 'Resolution',
         'dc:description': 'Resolution status: resolved, ambiguous, unknown-code or '
                           'missing-code.'})
    for row in rows:
        res = results[row[id_column]]
        row['Collection_Entry'] = res.collection_entry or ''
        row['Trace'] = res.trace
        row['Standard_Code'] = res.code or ''
        row['Standard_Candidates'] = res.candidate_list
        row['Resolution'] = res.status
    out.write(LanguageTable=rows)
    return results
