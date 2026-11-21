"""
The mixed bag of utility functions and classes of the pycldf package ...
"""
import os
import shutil
import tempfile
import zipfile
from typing import Optional, TYPE_CHECKING, Any
import pathlib
import collections
import urllib.parse
import urllib.request
from collections.abc import Generator

from csvw.metadata import is_url, Link, Column, Table, Schema, URITemplate
from clldutils.path import git_describe

from pycldf.fileutil import PathType
from pycldf.urlutil import sanitize_url

if TYPE_CHECKING:
    from pycldf import Dataset  # pragma: no cover

__all__ = [
    'pkg_path', 'DictTuple', 'qname2url', 'iter_uritemplates', 'MD_SUFFIX', 'GitRepository',
    'MediaFileMissingError', 'MissingMediaError', 'copy_dataset']

MD_SUFFIX = '-metadata.json'


class GitRepository:  # pylint: disable=too-few-public-methods
    """
    CLDF datasets are often created from data curated in git repositories. If this is the case, we
    exploit this to provide better provenance information in the dataset's metadata.
    """
    def __init__(
            self, url: str, clone: Optional[PathType] = None, version: Optional[str] = None, **dc):
        # We remove credentials from the URL immediately to make sure this isn't leaked into
        # CLDF metadata. Such credentials might be present in URLs read via gitpython from
        # remotes.
        self.url = sanitize_url(url)
        self.clone = clone
        self.version = version
        self.dc = dc

    def json_ld(self) -> collections.OrderedDict[str, Any]:
        """The repository described in JSON-LD."""
        res = collections.OrderedDict([
            ('rdf:about', self.url),
            ('rdf:type', 'prov:Entity'),
        ])
        if self.version:
            res['dc:created'] = self.version
        elif self.clone:
            res['dc:created'] = git_describe(self.clone)
        res.update({f'dc:{k}': self.dc[k] for k in sorted(self.dc)})
        return res


def iter_uritemplates(table: Table) -> Generator[tuple[Table | Schema | Column, str, URITemplate]]:
    """
    Generator of URITemplates specified in a table.

    Since URITemplates use column names as template variables, it is important to keep these in
    sync with the table schema, e.g. in case of renaming columns.
    """
    props = ['aboutUrl', 'valueUrl']
    for obj in [table, table.tableSchema] + table.tableSchema.columns:
        for prop in props:
            tmpl = getattr(obj, prop)
            if tmpl:
                yield obj, prop, tmpl


def pkg_path(*comps: str) -> pathlib.Path:
    """Returns a path within the pycldf package."""
    return pathlib.Path(__file__).resolve().parent.joinpath(*comps)


class DictTuple(tuple):
    """
    A `tuple` that acts like a `dict` when a `str` is passed to `__getitem__`.

    Since CLDF requires a unique `id` for each row in a component, and recommends identifier of
    type `str`, this class can be used to provide convenient access to items in an ordered
    collection of such objects.
    """
    def __new__(cls, items, **kw):
        return super(DictTuple, cls).__new__(cls, tuple(items))

    def __init__(self, _, key=lambda i: i.id, multi=False):
        """
        If `key` does not return unique values for all items, you may pass `multi=True` to
        retrieve `list`s of matching items for `l[key]`.
        """
        self._d = collections.defaultdict(list)
        for i, o in enumerate(self):
            self._d[key(o)].append(i)
        self._multi = multi

    def __getitem__(self, item):
        if not isinstance(item, (int, slice)):
            if self._multi:
                return [self[i] for i in self._d[item]]
            return self[self._d[item][0]]
        return super().__getitem__(item)


def qname2url(qname: str) -> Optional[str]:
    """
    Turns a qname of the form <prefix>:<localname> into a full HTTP URL if the prefix is known.
    """
    for prefix, uri in {
        'csvw': 'http://www.w3.org/ns/csvw#',
        'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
        'rdfs': 'http://www.w3.org/2000/01/rdf-schema#',
        'xsd': 'http://www.w3.org/2001/XMLSchema#',
        'dc': 'http://purl.org/dc/terms/',
        'dcat': 'http://www.w3.org/ns/dcat#',
        'prov': 'http://www.w3.org/ns/prov#',
    }.items():
        if qname.startswith(prefix + ':'):
            return qname.replace(prefix + ':', uri)
    return None


CLDF_TERMS = 'http://cldf.clld.org/v1.0/terms.rdf#'
CLDF_ID = CLDF_TERMS + 'id'
CLDF_LANGUAGE = CLDF_TERMS + 'languageReference'
CLDF_SOURCE = CLDF_TERMS + 'source'


class MediaFileMissingError(FileNotFoundError, ValueError):
    """
    Raised if a media item referenced by the selection cannot be read.

    :ivar media_id: ID of the affected media row.
    :ivar references: Source rows referencing the media item, each with `fname`, `lineno` and \
      `label` (the row's CLDF ID).
    :ivar path: Path which could not be read (if any).
    """

    def __init__(self, media_id: str, references: list, path: Optional[pathlib.Path] = None,
                 reason: Optional[str] = None):
        self.media_id = media_id
        self.references = references or []
        self.path = pathlib.Path(path) if path else None
        self.reason = reason or 'media file cannot be read'
        self.linenos = [r.lineno for r in self.references]
        self.source_rows = list(self.references)
        refs = ', '.join(
            f'{pathlib.PurePosixPath(str(r.fname)).name}:{r.lineno} ({r.label})'
            for r in self.references) or 'unknown source row'
        msg = f'{self.reason}: media {media_id}, referenced from {refs}'
        if self.path:
            msg += f' [{self.path}]'
        super().__init__(msg)


MissingMediaError = MediaFileMissingError


class _Row:
    """A table row with the physical line number needed for precise error reports."""

    def __init__(self, table, data: dict, lineno: int, fname):
        self.table = table
        self.data = data
        self.lineno = lineno
        self.fname = fname

    @property
    def label(self) -> str:
        col = _column(self.table, CLDF_ID)
        if col and self.data.get(col.header) is not None:
            return str(self.data[col.header])
        if self.table.tableSchema.primaryKey:
            col = self.table.get_column(self.table.tableSchema.primaryKey[0])
            if col and self.data.get(col.header) is not None:
                return str(self.data[col.header])
        return 'row'


def _column(table, property_url):
    for col in table.tableSchema.columns:
        if col.propertyUrl and col.propertyUrl.uri == property_url:
            return col
    return None


def _values(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _read_rows(table):
    return [_Row(table, row, lineno, fname)
            for fname, lineno, row in table.iterdicts(with_metadata=True)]


def _fk_target(ds, table, fk):
    target = ds.get(fk.reference.resource)
    if target is None or not fk.reference.columnReference or not fk.columnReference:
        return None, None, None
    target_col = target.get_column(fk.reference.columnReference[0])
    source_col = table.get_column(fk.columnReference[0])
    if not target_col or not source_col:
        return None, None, None
    return target, target_col.header, source_col.header


def _referenced(ds, entry):
    for fk in entry.table.tableSchema.foreignKeys:
        target, target_col, source_col = _fk_target(ds, entry.table, fk)
        if target:
            for value in _values(entry.data.get(source_col)):
                if value is not None:
                    yield target, target_col, str(value)


def _source_id(ref: str) -> str:
    sid = ref.strip()
    if '[' in sid:
        sid = sid.split('[', 1)[0].strip()
    return sid


def _select_rows(ds, language_ids):
    """Return retained row objects per table for a language selection."""
    language_table = ds.get('LanguageTable')
    form_table = ds.get('FormTable')
    media_table = ds.get('MediaTable')
    if language_table is None:
        raise ValueError('A language-filtered copy requires a LanguageTable')
    if form_table is None:
        raise ValueError('A language-filtered copy requires a FormTable')

    tables = list(ds.tables)
    rows = {id(t): _read_rows(t) for t in tables}
    by_value = collections.defaultdict(list)
    for table in tables:
        for entry in rows[id(table)]:
            for col in table.tableSchema.columns:
                for value in _values(entry.data.get(col.header)):
                    if value is not None:
                        by_value[(id(table), col.header, str(value))].append(entry)
    selected = set(language_ids)
    lang_id_col = _column(language_table, CLDF_ID)
    lang_pk_col = language_table.get_column(language_table.tableSchema.primaryKey[0]) \
        if language_table.tableSchema.primaryKey else None
    matched, language_values = set(), set()
    retained = {id(t): set() for t in tables}

    for entry in rows[id(language_table)]:
        values = {
            str(v) for v in (
                entry.data.get(lang_id_col.header) if lang_id_col else None,
                entry.data.get(lang_pk_col.header) if lang_pk_col else None,
            ) if v is not None}
        hit = values.intersection(selected)
        if hit:
            retained[id(language_table)].add(entry)
            matched.update(hit)
            language_values.update(values)
    if selected - matched:
        raise ValueError(f'unknown language ID(s): {sorted(selected - matched)}')

    form_lang_col = _column(form_table, CLDF_LANGUAGE)
    form_id_col = _column(form_table, CLDF_ID)
    form_pk_col = form_table.get_column(form_table.tableSchema.primaryKey[0]) \
        if form_table.tableSchema.primaryKey else None
    form_ids = set()
    if form_lang_col:
        for entry in rows[id(form_table)]:
            refs = {str(v) for v in _values(entry.data.get(form_lang_col.header))}
            if refs and refs.issubset(language_values):
                retained[id(form_table)].add(entry)
                for col in (form_id_col, form_pk_col):
                    if col and entry.data.get(col.header) is not None:
                        form_ids.add(str(entry.data[col.header]))

    # Rows in dependent components are selected if they link to a selected form or language.
    for table in tables:
        if table in (language_table, form_table, media_table):
            continue
        for entry in rows[id(table)]:
            linked_form, linked_language = False, False
            for fk in table.tableSchema.foreignKeys:
                target, _, source_col = _fk_target(ds, table, fk)
                values = {str(v) for v in _values(entry.data.get(source_col))} \
                    if source_col else set()
                if target is form_table and values.intersection(form_ids):
                    linked_form = True
                elif target is language_table and values and values.issubset(language_values):
                    linked_language = True
            if linked_form or linked_language:
                retained[id(table)].add(entry)

    # Close over referenced rows from retained rows. Language/form rows are never added by
    # closure, to avoid leaking data for unselected languages. Media rows are only added if
    # directly referenced by retained data.
    changed = True
    while changed:
        changed = False
        for table in tables:
            for entry in list(retained[id(table)]):
                for target, target_col, value in _referenced(ds, entry):
                    if target in (language_table, form_table):
                        continue
                    for candidate in by_value[(id(target), target_col, value)]:
                        if candidate not in retained[id(target)]:
                            retained[id(target)].add(candidate)
                            changed = True

    # Remove rows with unresolvable required foreign keys. Optional language/form references
    # pointing at excluded languages are cleared rather than failing the whole delivery.
    removed = True
    while removed:
        removed = False
        for table in tables:
            for entry in list(retained[id(table)]):
                for fk in table.tableSchema.foreignKeys:
                    target, target_col, source_col = _fk_target(ds, table, fk)
                    if not target or not source_col:
                        continue
                    values = [v for v in _values(entry.data.get(source_col)) if v is not None]
                    if not values:
                        continue
                    candidates = []
                    for value in values:
                        candidates.extend(by_value[(id(target), target_col, str(value))])
                    if target in (language_table, form_table):
                        if not retained[id(target)].intersection(candidates):
                            entry.data[source_col] = None
                    elif target is media_table:
                        if not candidates:
                            raise MediaFileMissingError(
                                str(values[0]), [entry],
                                reason='media reference points to a missing media row')
                    elif not candidates:
                        retained[id(table)].discard(entry)
                        removed = True
                        break
                if entry not in retained[id(table)]:
                    break
    return retained, rows


def _relpath(target: pathlib.Path, start: pathlib.Path) -> str:
    return pathlib.PurePosixPath(os.path.relpath(str(target), str(start))).as_posix()


def _filtered_copy(ds, dest, mdname, language_ids):
    from pycldf import Dataset  # pylint: disable=import-outside-toplevel
    from pycldf.media import MediaTable  # pylint: disable=import-outside-toplevel
    from pycldf.sources import Sources  # pylint: disable=import-outside-toplevel

    if is_url(ds.tablegroup.base):
        raise ValueError('Language-filtered copies require a local dataset')
    source_md = pathlib.Path(ds.tablegroup._fname)  # pylint: disable=W0212

    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    clone = Dataset.from_metadata(source_md)
    retained, all_rows = _select_rows(clone, language_ids)
    entries = {
        id(t): [e for e in all_rows[id(t)] if e in retained[id(t)]] for t in clone.tables}

    media_table = clone.get('MediaTable')
    media = MediaTable(clone) if media_table else None
    media_by_id = {str(f.id): f for f in media} if media else {}

    # Map media IDs to the retained rows referencing them, for precise error reports.
    media_refs = collections.defaultdict(list)
    for table in clone.tables:
        for entry in entries[id(table)]:
            for fk in table.tableSchema.foreignKeys:
                target, _, source_col = _fk_target(clone, table, fk)
                if target is media_table and source_col:
                    for value in _values(entry.data.get(source_col)):
                        if value is not None:
                            media_refs[str(value)].append(entry)

    staging = pathlib.Path(tempfile.mkdtemp(prefix='.pycldf-copy-', dir=str(dest.parent)))
    try:
        out_md = staging / (mdname or clone.filename)
        out_md.parent.mkdir(parents=True, exist_ok=True)

        # Validate and copy selected media before any tables are written. This guarantees
        # that a missing selected file cannot yield a plausible-looking partial delivery.
        copied_paths = set()
        for media_id, file_obj in media_by_id.items():
            if media_id not in media_refs:
                continue
            if file_obj.scheme != 'file':
                continue
            source = file_obj.local_path()
            if not source or not source.is_file():
                raise MediaFileMissingError(media_id, media_refs[media_id], path=source)
            if file_obj.path_in_zip:
                try:
                    with zipfile.ZipFile(str(source)) as zf:
                        zf.getinfo(file_obj.path_in_zip)
                except (KeyError, OSError, zipfile.BadZipFile) as exc:
                    raise MediaFileMissingError(
                        media_id, media_refs[media_id], path=source,
                        reason=f'archive member cannot be read ({exc})')
            else:
                try:
                    with source.open('rb') as fp:
                        fp.read(1)
                except OSError as exc:
                    raise MediaFileMissingError(
                        media_id, media_refs[media_id], path=source,
                        reason=f'media file cannot be read ({exc})')

            rel = pathlib.PurePosixPath(urllib.parse.unquote(file_obj.relpath))
            if rel.is_absolute() or '..' in rel.parts:
                raise MediaFileMissingError(
                    media_id, media_refs[media_id], path=source,
                    reason='media URL escapes the dataset directory')
            target = staging.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target not in copied_paths:
                shutil.copyfile(source, target)
                copied_paths.add(target)

            # Rewrite the URL relative to the output metadata file, preserving archive member.
            url_col = media.url_col
            if url_col:
                id_header = media.filename_col.header
                new_url = urllib.parse.quote(_relpath(target, out_md.parent), safe='/')
                for entry in entries[id(media_table)]:
                    if str(entry.data.get(id_header)) == media_id:
                        entry.data[url_col.header] = new_url

        # Write tables at their original relative paths, remembering the paths for FK updates.
        table_paths = {}
        media_url_col = media.url_col if media else None
        media_url_datatype = media_url_col.datatype if media_url_col else None
        try:
            if media_url_col and mdname and pathlib.PurePosixPath(mdname).parent.parts:
                # anyURI serialization strips leading ".." segments, which would break media
                # paths relative to a nested metadata file. Write those values verbatim.
                from csvw.metadata import Datatype  # pylint: disable=import-outside-toplevel
                media_url_col.datatype = Datatype.fromvalue('string')
            for table in clone.tables:
                rel = pathlib.PurePosixPath(
                    urllib.parse.unquote(urllib.parse.urlparse(table.url.string).path))
                target = staging.joinpath(*rel.parts)
                table_paths[id(table)] = target
                target.parent.mkdir(parents=True, exist_ok=True)
                table.common_props['dc:extent'] = table.write(
                    [entry.data for entry in entries[id(table)]], fname=target)
        finally:
            if media_url_col and media_url_datatype is not None:
                media_url_col.datatype = media_url_datatype

        for table in clone.tables:
            for fk in table.tableSchema.foreignKeys:
                target = clone.get(fk.reference.resource)
                if target:
                    fk.reference.resource = Link(
                        _relpath(table_paths[id(target)], out_md.parent))
            table.url = Link(_relpath(table_paths[id(table)], out_md.parent))

        # Restrict the bibliography to sources cited by retained rows.
        source_ids = set()
        for table in clone.tables:
            col = _column(table, CLDF_SOURCE)
            if not col:
                continue
            for entry in entries[id(table)]:
                for ref in _values(entry.data.get(col.header)):
                    if ref:
                        source_ids.add(_source_id(str(ref)))
        if source_ids and clone.sources:
            bib = Sources()
            for sid in sorted(source_ids):
                if sid in clone.sources:
                    bib.add(clone.sources[sid])
            if bib:
                # Keep the original relative bibliography location where possible.
                source_ref = clone.properties.get('dc:source') or 'sources.bib'
                parsed = urllib.parse.urlparse(source_ref)
                source_path = pathlib.PurePosixPath(urllib.parse.unquote(parsed.path))
                if parsed.scheme or pathlib.PurePosixPath(parsed.path).is_absolute():
                    source_path = pathlib.PurePosixPath(clone.bibname)
                bibpath = staging.joinpath(*source_path.parts)
                bibpath.parent.mkdir(parents=True, exist_ok=True)
                bib.write(bibpath)
                clone.properties['dc:source'] = _relpath(bibpath, out_md.parent)
            else:
                clone.properties.pop('dc:source', None)
        else:
            clone.properties.pop('dc:source', None)

        clone.write_metadata(fname=out_md)
        Dataset.from_metadata(out_md)

        if dest.exists():
            shutil.copytree(str(staging), str(dest), dirs_exist_ok=True)
            shutil.rmtree(str(staging), ignore_errors=True)
        else:
            staging.rename(dest)
        return dest / (mdname or clone.filename)
    finally:
        if staging.exists():
            shutil.rmtree(str(staging), ignore_errors=True)


def copy_dataset(
        ds: 'Dataset',
        dest: PathType,
        mdname: str = None,
        languages: Optional[list[str] | set[str] | str] = None,
        language_ids: Optional[list[str] | set[str] | str] = None,
) -> pathlib.Path:
    """
    Copy metadata, data and sources to files in `dest`.

    If `languages` (alias `language_ids`) is given, only data for the selected languages is
    copied, together with referenced languages, media and sources. If a selected media item
    cannot be read, `MediaFileMissingError` is raised and no deliverable is written.
    """
    selection = languages if languages is not None else language_ids
    if selection:
        if isinstance(selection, str):
            selection = [selection]
        return _filtered_copy(ds, dest, mdname, [str(i) for i in selection])

    from pycldf.media import MediaTable  # pylint: disable=import-outside-toplevel

    dest = pathlib.Path(dest)
    if not dest.exists():
        dest.mkdir(parents=True)

    from_url = is_url(ds.tablegroup.base)
    ds = ds.__class__.from_metadata(
        ds.tablegroup.base if from_url else ds.tablegroup._fname)  # pylint: disable=W0212

    _getter = urllib.request.urlretrieve if from_url else shutil.copy

    def copy_file(source, target):
        if not from_url and not source.exists():
            _getter(source.with_name(source.name + '.zip'), target.with_name(target.name + '.zip'))
            if target.exists():
                target.unlink()  # The plain file would take precedence over the archive.
        else:
            _getter(source, target)

    try:
        copy_file(ds.bibpath, dest / ds.bibname)
        ds.properties['dc:source'] = ds.bibname
    except:  # pragma: no cover # noqa  pylint: disable=W0702
        # Sources are optional
        pass

    # Read media while table URLs still refer to their original locations.
    if 'MediaTable' in ds:
        for f in MediaTable(ds):
            if f.scheme == 'file':
                if f.local_path().exists():
                    target = dest / urllib.parse.unquote(f.relpath)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(f.local_path(), target)

    for table in ds.tables:
        fname = table.url.resolve(table.base)
        name = pathlib.Path(urllib.parse.urlparse(fname).path).name if from_url else fname.name
        copy_file(fname, dest / name)
        table.url = Link(name)

        for fk in table.tableSchema.foreignKeys:
            fk.reference.resource = Link(pathlib.Path(fk.reference.resource.string).name)
    mdpath = dest.joinpath(
        mdname or  # noqa: W504
        (ds.tablegroup.base.split('/')[-1] if from_url
         else ds.tablegroup._fname.name))  # pylint: disable=W0212
    if from_url:
        del ds.tablegroup.at_props['base']  # pragma: no cover
    ds.write_metadata(fname=mdpath)
    return mdpath
