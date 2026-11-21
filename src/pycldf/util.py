"""
The mixed bag of utility functions and classes of the pycldf package ...
"""
import re
import shutil
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
    'pkg_path', 'DictTuple', 'qname2url', 'iter_uritemplates',
    'iter_effective_uritemplates', 'rewrite_uritemplate',
    'MD_SUFFIX', 'GitRepository']

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


def iter_effective_uritemplates(
        table: Table) -> Generator[tuple[Any, Any, str, URITemplate], None, None]:
    """
    Generator of the URITemplates actually governing `table`, following CSVW inheritance.

    CSVW lets `aboutUrl` and `valueUrl` be inherited from the enclosing `TableGroup` (or any
    other object along the parent chain). For the table, its schema and each column this
    generator resolves that chain and yields four-tuples

    ``(governed_object, owner, prop, template)``

    where ``governed_object`` is the table, schema or column for which the template is effective,
    ``owner`` is the object physically carrying the template (possibly an ancestor such as the
    `TableGroup`), ``prop`` is ``'aboutUrl'`` or ``'valueUrl'`` and ``template`` is the effective
    template.

    The hierarchy is traversed explicitly (column -> schema -> table -> table's ancestors) rather
    than relying solely on each object's own `_parent` link, because columns added dynamically
    (via :meth:`Dataset.add_columns`) only get their `_parent` wired when metadata is reloaded.
    The explicit resolution matches what CSVW resolves after such a reload.

    Unlike :func:`iter_uritemplates`, this also surfaces templates inherited from the
    `TableGroup`, which is required to keep inherited templates in sync when columns are renamed.
    """
    props = ['aboutUrl', 'valueUrl']
    schema = table.tableSchema

    ancestors = []
    node = getattr(table, '_parent', None)
    while node is not None:
        ancestors.append(node)
        node = getattr(node, '_parent', None)

    targets = [table, schema] + list(schema.columns)
    for obj in targets:
        if obj is table:
            chain = [table] + ancestors
        elif obj is schema:
            chain = [schema, table] + ancestors
        else:
            chain = [obj, schema, table] + ancestors
        for prop in props:
            owner, tmpl = None, None
            for candidate in chain:
                value = getattr(candidate, prop, None)
                if value is not None:
                    owner, tmpl = candidate, value
                    break
            if tmpl:
                yield obj, owner, prop, tmpl


def rewrite_uritemplate(
        tmpl: URITemplate, renames: dict[str, str]) -> Optional[URITemplate]:
    """
    Rename template variables of `tmpl` according to `renames`, matching whole names only.

    `renames` maps old variable names to new names. All mappings are applied simultaneously, so
    swapping two names (``{'slug': 'slug_long', 'slug_long': 'slug'}``) is handled correctly -
    each variable is looked up in the mapping once. Variable names are matched at name
    boundaries, so renaming ``slug`` does not touch the distinct variable ``slug_long``.
    Expansion operators (``#``, ``/``, ``;`` ...), composite variable lists, explode (``*``) and
    prefix (``:3``) modifiers are preserved, e.g. ``{slug:3}`` becomes ``{new:3}`` rather than
    having its modifier detached.

    :return: A new :class:`csvw.metadata.URITemplate` with the variables renamed, or `None` if
        none of the variables in `tmpl` are keys of `renames`.
    """
    if not set(tmpl.variable_names) & renames.keys():
        return None

    def rewrite_expression(match: 're.Match[str]') -> str:
        expression = match.groups()[0]
        operator = ''
        if expression[:1] in '+#./;?&,!@|=':
            operator, expression = expression[0], expression[1:]

        parts = []
        for var in expression.split(','):
            suffix = ''
            core = var
            if core.endswith('*'):
                suffix = '*'
                core = core[:-1]
            if ':' in core:
                name, prefix = core.split(':', 1)
                suffix = ':' + prefix + suffix
            else:
                name = core
            parts.append(renames.get(name, name) + suffix)
        return '{' + operator + ','.join(parts) + '}'

    return URITemplate(re.sub(r'{([^}]+)}', rewrite_expression, str(tmpl)))


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


def copy_dataset(ds: 'Dataset', dest: PathType, mdname: str = None) -> pathlib.Path:
    """
    Copy metadata, data and sources to files in `dest`.
    """
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
