import json
import re

from peewee import *
from peewee import ColumnBase
from peewee import EnclosedNodeList
from peewee import Entity
from peewee import Expression
from peewee import Node
from peewee import NodeList
from peewee import OP
from peewee import is_model
from peewee import merge_dict
from peewee import sqlite3
from playhouse import fts_parser as _fts_parser


FTS3_MATCHINFO = 'pcx'
FTS4_MATCHINFO = 'pcnalx'
FTS_VERSION = 4
FTS5_MIN_SQLITE_VERSION = (3, 9, 0)


class RowIDField(AutoField):
    auto_increment = True
    column_name = name = required_name = 'rowid'

    def bind(self, model, name, *args):
        if name != self.required_name:
            raise ValueError('%s must be named "%s".' %
                             (type(self), self.required_name))
        super(RowIDField, self).bind(model, name, *args)


class DocIDField(RowIDField):
    column_name = name = required_name = 'docid'


class AutoIncrementField(AutoField):
    def ddl(self, ctx):
        node_list = super(AutoIncrementField, self).ddl(ctx)
        return NodeList((node_list, SQL('AUTOINCREMENT')))


class TDecimalField(DecimalField):
    field_type = 'TEXT'
    def get_modifiers(self): pass

    def db_value(self, value):
        if value is not None:
            return str(super(TDecimalField, self).db_value(value))


class ISODateTimeField(DateTimeField):
    formats = [
        '%Y-%m-%dT%H:%M:%S.%f%z',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d',
    ]

    def db_value(self, value):
        if isinstance(value, str):
            # Normalize string input so the column stays uniformly ISO.
            value = self.adapt(value)
        if value and not isinstance(value, str):
            return value.isoformat()
        return value


class JSONPath(ColumnBase):
    def __init__(self, field, path=None):
        super(JSONPath, self).__init__()
        self._field = field
        self._path = path or ()

    def _converter(self, value):
        return self._field.python_value(value)

    @property
    def path(self):
        return Value('$%s' % ''.join(self._path))

    def __getitem__(self, idx):
        if isinstance(idx, int) or idx == '#':
            item = '[%s]' % idx
        else:
            item = '."%s"' % idx.replace('"', '""')
        return type(self)(self._field, self._path + (item,))

    def append(self, value, as_json=None):
        if as_json or isinstance(value, (list, dict)):
            value = fn.json(self._field._json_dumps(value))
        return fn.json_set(self._field, self['#'].path, value)

    def _json_operation(self, func, value, as_json=None):
        if as_json or isinstance(value, (list, dict)):
            value = fn.json(self._field._json_dumps(value))
        return func(self._field, self.path, value)

    def insert(self, value, as_json=None):
        return self._json_operation(fn.json_insert, value, as_json)

    def set(self, value, as_json=None):
        return self._json_operation(fn.json_set, value, as_json)

    def replace(self, value, as_json=None):
        return self._json_operation(fn.json_replace, value, as_json)

    def update(self, value):
        return self.set(fn.json_patch(self, self._field._json_dumps(value)))

    def remove(self):
        return fn.json_remove(self._field, self.path)

    def json_type(self):
        return fn.json_type(self._field, self.path)

    def length(self):
        return fn.json_array_length(self._field, self.path)

    def children(self):
        return fn.json_each(self._field, self.path)

    def tree(self):
        return fn.json_tree(self._field, self.path)

    def __sql__(self, ctx):
        return ctx.sql(fn.json_extract(self._field, self.path)
                       if self._path else self._field)

class JSONBPath(JSONPath):
    def append(self, value, as_json=None):
        if as_json or isinstance(value, (list, dict)):
            value = fn.jsonb(self._field._json_dumps(value))
        return fn.jsonb_set(self._field, self['#'].path, value)

    def _json_operation(self, func, value, as_json=None):
        if as_json or isinstance(value, (list, dict)):
            value = fn.jsonb(self._field._json_dumps(value))
        return func(self._field, self.path, value)

    def insert(self, value, as_json=None):
        return self._json_operation(fn.jsonb_insert, value, as_json)

    def set(self, value, as_json=None):
        return self._json_operation(fn.jsonb_set, value, as_json)

    def replace(self, value, as_json=None):
        return self._json_operation(fn.jsonb_replace, value, as_json)

    def update(self, value):
        return self.set(fn.jsonb_patch(self, self._field._json_dumps(value)))

    def remove(self):
        return fn.jsonb_remove(self._field, self.path)

    def __sql__(self, ctx):
        return ctx.sql(fn.jsonb_extract(self._field, self.path)
                       if self._path else self._field)


class JSONField(TextField):
    field_type = 'TEXT'
    Path = JSONPath

    def __init__(self, json_dumps=None, json_loads=None, **kwargs):
        self._json_dumps = json_dumps or json.dumps
        self._json_loads = json_loads or json.loads
        super(JSONField, self).__init__(**kwargs)

    def python_value(self, value):
        if value is not None:
            try:
                return self._json_loads(value)
            except (TypeError, ValueError):
                return value

    def db_value(self, value):
        if value is not None:
            if not isinstance(value, Node):
                value = fn.json(self._json_dumps(value))
            return value

    def _e(op):
        def inner(self, rhs):
            if isinstance(rhs, (list, dict)):
                rhs = AsIs(rhs, self.db_value)
            return Expression(self, op, rhs)
        return inner
    __eq__ = _e(OP.EQ)
    __ne__ = _e(OP.NE)
    __gt__ = _e(OP.GT)
    __ge__ = _e(OP.GTE)
    __lt__ = _e(OP.LT)
    __le__ = _e(OP.LTE)
    __hash__ = Field.__hash__

    def __getitem__(self, item):
        return self.Path(self)[item]

    def extract(self, *paths):
        paths = [Value(p, converter=False) for p in paths]
        return fn.json_extract(self, *paths)
    def extract_json(self, path):
        return Expression(self, '->', Value(path, converter=False))
    def extract_text(self, path):
        return Expression(self, '->>', Value(path, converter=False))

    def append(self, value, as_json=None):
        return self.Path(self).append(value, as_json)

    def insert(self, value, as_json=None):
        return self.Path(self).insert(value, as_json)

    def set(self, value, as_json=None):
        return self.Path(self).set(value, as_json)

    def replace(self, value, as_json=None):
        return self.Path(self).replace(value, as_json)

    def update(self, data):
        return self.Path(self).update(data)

    def remove(self, *paths):
        if not paths:
            return self.Path(self).remove()
        return fn.json_remove(self, *paths)

    def json_type(self):
        return fn.json_type(self)

    def length(self, path=None):
        args = (self, path) if path else (self,)
        return fn.json_array_length(*args)

    def children(self):
        """
        Schema of `json_each` and `json_tree`:

        key,
        value,
        type TEXT (object, array, string, etc),
        atom (value for primitive/scalar types, NULL for array and object)
        id INTEGER (unique identifier for element)
        parent INTEGER (unique identifier of parent element or NULL)
        fullkey TEXT (full path describing element)
        path TEXT (path to the container of the current element)
        json JSON hidden (1st input parameter to function)
        root TEXT hidden (2nd input parameter, path at which to start)
        """
        return fn.json_each(self)

    def tree(self):
        return fn.json_tree(self)


class JSONBField(JSONField):
    field_type = 'JSONB'
    Path = JSONBPath

    def db_value(self, value):
        if value is not None:
            if not isinstance(value, Node):
                value = fn.jsonb(self._json_dumps(value))
            return value

    def json(self):
        return fn.json(self)

    def extract(self, *paths):
        paths = [Value(p, converter=False) for p in paths]
        return fn.jsonb_extract(self, *paths)

    def remove(self, *paths):
        if not paths:
            return self.Path(self).remove()
        return fn.jsonb_remove(self, *paths)


class SearchField(Field):
    def __init__(self, unindexed=False, column_name=None, **k):
        if k:
            raise ValueError('SearchField does not accept these keyword '
                             'arguments: %s.' % sorted(k))
        super(SearchField, self).__init__(unindexed=unindexed,
                                          column_name=column_name, null=True)

    def match(self, term):
        return match(self, term)

    @property
    def fts_column_index(self):
        if not hasattr(self, '_fts_column_index'):
            search_fields = [f.name for f in self.model._meta.sorted_fields
                             if isinstance(f, SearchField)]
            self._fts_column_index = search_fields.index(self.name)
        return self._fts_column_index

    def highlight(self, left, right):
        column_idx = self.fts_column_index
        return fn.highlight(self.model._meta.entity, column_idx, left, right)

    def snippet(self, left, right, over_length='...', max_tokens=16):
        if not (0 < max_tokens < 65):
            raise ValueError('max_tokens must be between 1 and 64 (inclusive)')
        column_idx = self.fts_column_index
        return fn.snippet(self.model._meta.entity, column_idx, left, right,
                          over_length, max_tokens)


class VirtualTableSchemaManager(SchemaManager):
    def _create_virtual_table(self, safe=True, **options):
        options = self.model.clean_options(
            merge_dict(self.model._meta.options, options))

        # Structure:
        # CREATE VIRTUAL TABLE <model>
        # USING <extension_module>
        # ([prefix_arguments, ...] fields, ... [arguments, ...], [options...])
        ctx = self._create_context()
        ctx.literal('CREATE VIRTUAL TABLE ')
        if safe:
            ctx.literal('IF NOT EXISTS ')
        (ctx
         .sql(self.model)
         .literal(' USING '))

        ext_module = self.model._meta.extension_module
        if isinstance(ext_module, Node):
            return ctx.sql(ext_module)

        ctx.sql(SQL(ext_module)).literal(' ')
        arguments = []
        meta = self.model._meta

        if meta.prefix_arguments:
            arguments.extend([SQL(a) for a in meta.prefix_arguments])

        # Constraints, data-types, foreign and primary keys are all omitted.
        for field in meta.sorted_fields:
            if isinstance(field, (RowIDField)) or field._hidden:
                continue
            field_def = [Entity(field.column_name)]
            if field.unindexed:
                field_def.append(SQL('UNINDEXED'))
            arguments.append(NodeList(field_def))

        if meta.arguments:
            arguments.extend([SQL(a) for a in meta.arguments])

        if options:
            arguments.extend(self._create_table_option_sql(options))
        return ctx.sql(EnclosedNodeList(arguments))

    def _create_table(self, safe=True, **options):
        if issubclass(self.model, VirtualModel):
            return self._create_virtual_table(safe, **options)

        return super(VirtualTableSchemaManager, self)._create_table(
            safe, **options)


class VirtualModel(Model):
    class Meta:
        arguments = None
        extension_module = None
        prefix_arguments = None
        primary_key = False
        schema_manager_class = VirtualTableSchemaManager

    @classmethod
    def clean_options(cls, options):
        return options


class BaseFTSModel(VirtualModel):
    @classmethod
    def clean_options(cls, options):
        content = options.get('content')
        prefix = options.get('prefix')
        tokenize = options.get('tokenize')

        if isinstance(content, str) and content == '':
            # Special-case content-less full-text search tables.
            options['content'] = "''"
        elif isinstance(content, Field):
            # A field generates DDL fts5 rejects and fts4 silently truncates.
            raise ImproperlyConfigured('The content option must be a Model, '
                                       'a table-name string, or the empty '
                                       'string (contentless).')

        if prefix:
            if isinstance(prefix, (list, tuple)):
                prefix = ','.join([str(i) for i in prefix])
            elif isinstance(prefix, int):
                prefix = str(prefix)
            options['prefix'] = "'%s'" % prefix.strip("' ")

        if tokenize and cls._meta.extension_module.lower() == 'fts5':
            # Tokenizers need to be in quoted string for FTS5, but not for FTS3
            # or FTS4.
            options['tokenize'] = '"%s"' % tokenize

        return options


class FTSModel(BaseFTSModel):
    """
    VirtualModel class for creating tables that use the FTS4 search
    extension. To use FTS3, set ``Meta.extension_module = 'FTS3'``.
    """
    rowid = RowIDField()

    class Meta:
        extension_module = 'FTS%s' % FTS_VERSION

    @classmethod
    def _fts_cmd(cls, cmd):
        tbl = cls._meta.table_name
        res = cls._meta.database.execute_sql(
            "INSERT INTO %s(%s) VALUES('%s');" % (tbl, tbl, cmd))
        return res.fetchone()

    @classmethod
    def optimize(cls):
        return cls._fts_cmd('optimize')

    @classmethod
    def rebuild(cls):
        return cls._fts_cmd('rebuild')

    @classmethod
    def integrity_check(cls):
        return cls._fts_cmd('integrity-check')

    @classmethod
    def merge(cls, blocks=200, segments=8):
        return cls._fts_cmd('merge=%s,%s' % (blocks, segments))

    @classmethod
    def automerge(cls, state=True):
        return cls._fts_cmd('automerge=%s' % (state and '1' or '0'))

    @classmethod
    def match(cls, term):
        """
        Generate a `MATCH` expression appropriate for searching this table.
        """
        return match(cls._meta.entity, term)

    @classmethod
    def rank(cls, *weights):
        matchinfo = fn.matchinfo(cls._meta.entity, FTS3_MATCHINFO)
        return fn.fts_rank(matchinfo, *weights)

    @classmethod
    def bm25(cls, *weights):
        match_info = fn.matchinfo(cls._meta.entity, FTS4_MATCHINFO)
        return fn.fts_bm25(match_info, *weights)

    @classmethod
    def bm25f(cls, *weights):
        match_info = fn.matchinfo(cls._meta.entity, FTS4_MATCHINFO)
        return fn.fts_bm25f(match_info, *weights)

    @classmethod
    def lucene(cls, *weights):
        match_info = fn.matchinfo(cls._meta.entity, FTS4_MATCHINFO)
        return fn.fts_lucene(match_info, *weights)

    @classmethod
    def _search(cls, term, weights, with_score, score_alias, score_fn,
                explicit_ordering):
        if not weights:
            rank = score_fn()
        elif isinstance(weights, dict):
            weight_args = []
            for field in cls._meta.sorted_fields:
                # rowid is not a searchable column.
                if isinstance(field, RowIDField):
                    continue
                # Attempt to get the specified weight of the field by looking
                # it up using it's field instance followed by name.
                field_weight = weights.get(field, weights.get(field.name, 1.0))
                weight_args.append(field_weight)
            rank = score_fn(*weight_args)
        else:
            rank = score_fn(*weights)

        selection = ()
        order_by = rank
        if with_score:
            selection = (cls, rank.alias(score_alias))
        if with_score and not explicit_ordering:
            order_by = SQL(score_alias)

        return (cls
                .select(*selection)
                .where(cls.match(term))
                .order_by(order_by))

    @classmethod
    def search(cls, term, weights=None, with_score=False, score_alias='score',
               explicit_ordering=False):
        """Full-text search using selected `term`."""
        return cls._search(
            term,
            weights,
            with_score,
            score_alias,
            cls.rank,
            explicit_ordering)

    @classmethod
    def search_bm25(cls, term, weights=None, with_score=False,
                    score_alias='score', explicit_ordering=False):
        """Full-text search for selected `term` using BM25 algorithm."""
        return cls._search(
            term,
            weights,
            with_score,
            score_alias,
            cls.bm25,
            explicit_ordering)

    @classmethod
    def search_bm25f(cls, term, weights=None, with_score=False,
                     score_alias='score', explicit_ordering=False):
        """Full-text search for selected `term` using BM25F algorithm."""
        return cls._search(
            term,
            weights,
            with_score,
            score_alias,
            cls.bm25f,
            explicit_ordering)

    @classmethod
    def search_lucene(cls, term, weights=None, with_score=False,
                      score_alias='score', explicit_ordering=False):
        """Full-text search for selected `term` using Lucene algorithm."""
        return cls._search(
            term,
            weights,
            with_score,
            score_alias,
            cls.lucene,
            explicit_ordering)


_alphabet = 'abcdefghijklmnopqrstuvwxyz'
_alphanum = (set('\t ,"(){}*:_+0123456789') |
             set(_alphabet) |
             set(_alphabet.upper()) |
             set((chr(26),)))
_invalid_ascii = set(chr(p) for p in range(128) if chr(p) not in _alphanum)
del _alphabet
del _alphanum
_quote_re = re.compile(r'[^\s"]+|"[^"\\]*(?:\\.[^"\\]*)*"')


class FTS5Model(BaseFTSModel):
    """
    Requires SQLite >= 3.9.0.

    Table options:

    content: table name of external content, or empty string for "contentless"
    content_rowid: column name of external content primary key
    prefix: integer(s). Ex: '2' or '2 3 4'
    tokenize: porter, unicode61, ascii. Ex: 'porter unicode61'

    The unicode tokenizer supports the following parameters:

    * remove_diacritics (1 or 0, default is 1)
    * tokenchars (string of characters, e.g. '-_'
    * separators (string of characters)

    Parameters are passed as alternating parameter name and value, so:

    {'tokenize': "unicode61 remove_diacritics 0 tokenchars '-_'"}

    Content-less tables:

    If you don't need the full-text content in its original form, you can
    specify a content-less table. Searches and auxiliary functions will work
    as usual, but the only values returned when SELECT-ing can be rowid. Also
    content-less tables do not support UPDATE or DELETE, unless created with
    the contentless_delete=1 option (sqlite >= 3.43).

    External content tables:

    You can set up triggers to sync these, e.g.

    -- Create a table. And an external content fts5 table to index it.
    CREATE TABLE tbl(a INTEGER PRIMARY KEY, b);
    CREATE VIRTUAL TABLE ft USING fts5(b, content='tbl', content_rowid='a');

    -- Triggers to keep the FTS index up to date.
    CREATE TRIGGER tbl_ai AFTER INSERT ON tbl BEGIN
      INSERT INTO ft(rowid, b) VALUES (new.a, new.b);
    END;
    CREATE TRIGGER tbl_ad AFTER DELETE ON tbl BEGIN
      INSERT INTO ft(ft, rowid, b) VALUES('delete', old.a, old.b);
    END;
    CREATE TRIGGER tbl_au AFTER UPDATE ON tbl BEGIN
      INSERT INTO ft(ft, rowid, b) VALUES('delete', old.a, old.b);
      INSERT INTO ft(rowid, b) VALUES (new.a, new.b);
    END;

    Note: with sqlite's default recursive_triggers=off, INSERT OR REPLACE on
    the content table skips the delete trigger and corrupts the index. Enable
    PRAGMA recursive_triggers if REPLACE may be used, or call
    ``FTS5Model.install_maintenance()``: it installs BEFORE/AFTER triggers
    plus a staging table that stay correct with recursive_triggers off for
    plain writes and INSERT OR REPLACE / INSERT OR IGNORE alike, and backfills
    an index left with stale or missing terms in a single transaction.

    Built-in auxiliary functions:

    * bm25(tbl[, weight_0, ... weight_n])
    * highlight(tbl, col_idx, prefix, suffix)
    * snippet(tbl, col_idx, prefix, suffix, ?, max_tokens)
    """
    # FTS5 does not support declared primary keys, but we can use the
    # implicit rowid.
    rowid = RowIDField()

    class Meta:
        extension_module = 'fts5'

    _error_messages = {
        'field_type': ('Besides the implicit `rowid` column, all columns must '
                       'be instances of SearchField'),
        'index': 'Secondary indexes are not supported for FTS5 models',
        'pk': 'FTS5 models must use the default `rowid` primary key',
    }

    @classmethod
    def validate_model(cls):
        # Perform FTS5-specific validation and options post-processing.
        if cls._meta.primary_key.name != 'rowid':
            raise ImproperlyConfigured(cls._error_messages['pk'])
        for field in cls._meta.fields.values():
            if not isinstance(field, (SearchField, RowIDField)):
                raise ImproperlyConfigured(cls._error_messages['field_type'])
        if cls._meta.indexes:
            raise ImproperlyConfigured(cls._error_messages['index'])

    @classmethod
    def fts5_installed(cls):
        if sqlite3.sqlite_version_info[:3] < FTS5_MIN_SQLITE_VERSION:
            return False

        # Test in-memory DB to determine if the FTS5 extension is installed.
        tmp_db = sqlite3.connect(':memory:')
        try:
            tmp_db.execute('CREATE VIRTUAL TABLE fts5test USING fts5 (data);')
        except:
            try:
                tmp_db.enable_load_extension(True)
                tmp_db.load_extension('fts5')
            except:
                return False
            else:
                cls._meta.database.load_extension('fts5')
        finally:
            tmp_db.close()

        return True

    @staticmethod
    def validate_query(query):
        """
        Simple helper function to indicate whether a search query is a
        valid FTS5 query. Note: this simply looks at the characters being
        used, and is not guaranteed to catch all problematic queries.
        """
        tokens = _quote_re.findall(query)
        for token in tokens:
            if token.startswith('"') and token.endswith('"'):
                continue
            if set(token) & _invalid_ascii:
                return False
        return True

    @staticmethod
    def clean_query(query, replace=chr(26)):
        """
        Clean a query of invalid tokens.
        """
        accum = []
        any_invalid = False
        tokens = _quote_re.findall(query)
        for token in tokens:
            if token.startswith('"') and token.endswith('"'):
                accum.append(token)
                continue
            token_set = set(token)
            invalid_for_token = token_set & _invalid_ascii
            if invalid_for_token:
                any_invalid = True
                for c in invalid_for_token:
                    token = token.replace(c, replace)
            accum.append(token)

        if any_invalid:
            return ' '.join(accum)
        return query

    @classmethod
    def web_query(cls, query):
        """
        Translate a search-box query (quoted phrases, AND/OR/NOT, -exclusion,
        column:filters, parentheses) into an FTS5 query.
        """
        columns = [f.column_name for f in cls._meta.sorted_fields
                   if isinstance(f, SearchField) and not f.unindexed]
        return _fts_parser.parse(query, columns)

    @classmethod
    def match(cls, term):
        """
        Generate a `MATCH` expression appropriate for searching this table.
        """
        return match(cls._meta.entity, term)

    @classmethod
    def rank(cls, *args):
        return cls.bm25(*args) if args else SQL('rank')

    @classmethod
    def bm25(cls, *weights):
        return fn.bm25(cls._meta.entity, *weights)

    @classmethod
    def search(cls, term, weights=None, with_score=False, score_alias='score',
               explicit_ordering=False):
        """Full-text search using selected `term`."""
        return cls.search_bm25(
            FTS5Model.clean_query(term),
            weights,
            with_score,
            score_alias,
            explicit_ordering)

    @classmethod
    def search_bm25(cls, term, weights=None, with_score=False,
                    score_alias='score', explicit_ordering=False):
        """Full-text search using selected `term`."""
        if not weights:
            rank = SQL('rank')
        elif isinstance(weights, dict):
            weight_args = []
            for field in cls._meta.sorted_fields:
                # bm25() assigns weights positionally across all columns,
                # including UNINDEXED columns, so they must not be skipped.
                if isinstance(field, SearchField):
                    weight_args.append(
                        weights.get(field, weights.get(field.name, 1.0)))
            rank = fn.bm25(cls._meta.entity, *weight_args)
        else:
            rank = fn.bm25(cls._meta.entity, *weights)

        selection = ()
        order_by = rank
        if with_score:
            selection = (cls, rank.alias(score_alias))
        if with_score and not explicit_ordering:
            order_by = SQL(score_alias)

        return (cls
                .select(*selection)
                .where(cls.match(FTS5Model.clean_query(term)))
                .order_by(order_by))

    @classmethod
    def _fts_cmd_sql(cls, cmd, **extra_params):
        tbl = cls._meta.entity
        columns = [tbl]
        values = [cmd]
        for key, value in extra_params.items():
            columns.append(Entity(key))
            values.append(value)

        return NodeList((
            SQL('INSERT INTO'),
            cls._meta.entity,
            EnclosedNodeList(columns),
            SQL('VALUES'),
            EnclosedNodeList(values)))

    @classmethod
    def _fts_cmd(cls, cmd, **extra_params):
        query = cls._fts_cmd_sql(cmd, **extra_params)
        return cls._meta.database.execute(query)

    @classmethod
    def automerge(cls, level):
        if not (0 <= level <= 64):
            raise ValueError('level must be between 0 and 64')
        return cls._fts_cmd('automerge', rank=level)

    @classmethod
    def merge(cls, npages):
        return cls._fts_cmd('merge', rank=npages)

    @classmethod
    def optimize(cls):
        return cls._fts_cmd('optimize')

    @classmethod
    def rebuild(cls):
        return cls._fts_cmd('rebuild')

    @classmethod
    def set_pgsz(cls, pgsz):
        return cls._fts_cmd('pgsz', rank=pgsz)

    @classmethod
    def set_rank(cls, rank_expression):
        return cls._fts_cmd('rank', rank=rank_expression)

    @classmethod
    def delete_all(cls):
        return cls._fts_cmd('delete-all')

    @classmethod
    def delete_command(cls, rowid, **values):
        """
        Remove a row using the fts5 'delete' command, passing the values
        exactly as they were indexed. This is how rows are removed from
        external-content and contentless tables, which cannot look up the
        old values themselves.
        """
        accum = {}
        for field in cls._meta.sorted_fields:
            if not isinstance(field, SearchField):
                continue
            if field.name in values:
                accum[field.column_name] = values.pop(field.name)
            elif field.column_name in values:
                accum[field.column_name] = values.pop(field.column_name)
            elif not field.unindexed:
                # Deleting against a missing value silently corrupts the
                # index, so all indexed columns are required.
                raise ValueError('delete_command() requires the value of "%s" '
                                 'as it was originally indexed.' % field.name)
        if values:
            raise ValueError('delete_command() received unrecognized column(s): '
                             '%s' % ', '.join(sorted(values)))
        return cls._fts_cmd('delete', rowid=rowid, **accum)

    @classmethod
    def integrity_check(cls, rank=0):
        return cls._fts_cmd('integrity-check', rank=rank)

    @classmethod
    def _external_content_meta(cls, content_model=None):
        # Resolve the external-content table, its integer rowid column and the
        # indexed columns from the model's FTS5 options.
        options = cls._meta.options or {}
        if content_model is None:
            content_model = options.get('content')
        rowid_field = options.get('content_rowid')
        if not is_model(content_model) or not isinstance(rowid_field, Field):
            raise ImproperlyConfigured(
                'install_maintenance() requires an external-content FTS5 '
                'table declared with the "content" (a Model) and '
                '"content_rowid" (an integer Field) options.')
        columns = [f.column_name for f in cls._meta.sorted_fields
                   if isinstance(f, SearchField)]
        return (content_model,
                content_model._meta.table_name,
                rowid_field.column_name,
                columns)

    @staticmethod
    def _quote(name):
        return '"%s"' % name.replace('"', '""')

    @classmethod
    def _sync_object_names(cls, content_table):
        return {
            'stage': '%s_fts5_stage' % content_table,
            'triggers': {
                'bi': '%s_fts5_bi' % content_table,
                'ai': '%s_fts5_ai' % content_table,
                'au': '%s_fts5_au' % content_table,
                'ad': '%s_fts5_ad' % content_table,
            },
        }

    @classmethod
    def _drop_legacy_sync_triggers(cls, db, content_table, fts_table, names):
        our_names = set(names['triggers'].values())
        detected = set()
        for catalog in ('sqlite_master', 'sqlite_temp_master'):
            rows = db.execute_sql(
                'SELECT name, sql FROM %s WHERE type = ? AND tbl_name = ?'
                % catalog, ('trigger', content_table)).fetchall()
            for name, sql in rows:
                if name in our_names:
                    continue
                # Only replace triggers that maintain this FTS index.
                if sql and fts_table in sql:
                    detected.add(name)
        for name in our_names | detected:
            db.execute_sql('DROP TRIGGER IF EXISTS %s' % cls._quote(name))

    @classmethod
    def install_sync_triggers(cls, content_model=None):
        """
        Create (or replace) the triggers that keep an external-content FTS5
        index in sync, together with a staging table they use to remember
        the indexed values.

        The triggers are correct with ``PRAGMA recursive_triggers`` kept off:
        ``INSERT OR REPLACE`` skips the content table's DELETE triggers in that
        configuration, so the old values are captured in a BEFORE INSERT
        trigger instead. ``INSERT OR IGNORE`` conflicts only fire BEFORE
        INSERT, which means nothing is ever applied to the FTS index for a row
        that was not actually written.
        """
        content_model, content_table, rowid_col, columns = (
            cls._external_content_meta(content_model))
        db = cls._meta.database
        fts_table = cls._meta.table_name
        names = cls._sync_object_names(content_table)
        stage_table = names['stage']
        triggers = names['triggers']

        q = cls._quote
        fts = q(fts_table)
        doc = q(content_table)
        stage = q(stage_table)
        rid = q(rowid_col)
        cols = [q(column) for column in columns]
        col_list = ', '.join(cols)

        cls._drop_legacy_sync_triggers(
            db, content_table, fts_table, names)

        stage_ddl = ('CREATE TABLE IF NOT EXISTS %s ("rid" INTEGER '
                     'PRIMARY KEY%s)')
        extra = ''.join(', %s' % column for column in cols)
        db.execute_sql(stage_ddl % (stage, extra))

        delete_cmd = "'delete'"
        old_vals = ', '.join('old.%s' % column for column in cols)
        new_vals = ', '.join('new.%s' % column for column in cols)

        # BEFORE INSERT: remember whatever is currently indexed under the
        # incoming rowid. On an INSERT OR REPLACE conflict this is the only
        # trigger that can still see the doomed row; on an INSERT OR IGNORE
        # conflict it is the only trigger that fires at all.
        same_content = ' AND '.join(
            '"d".%s IS %s.%s' % (column, stage, column)
            for column in cols)
        bi_sql = (
            'CREATE TRIGGER %s BEFORE INSERT ON %s BEGIN\n'
            '  DELETE FROM %s WHERE EXISTS(\n'
            '    SELECT 1 FROM %s AS "d"\n'
            '     WHERE "d".%s IS %s."rid" AND (%s));\n'
            '  DELETE FROM %s WHERE %s."rid" IS new.%s;\n'
            '  INSERT INTO %s ("rid", %s)\n'
            '  SELECT %s, %s FROM %s WHERE %s.%s IS new.%s;\n'
            'END') % (
            q(triggers['bi']), doc, stage, doc,
            rid, stage, same_content,
            stage, stage, rid,
            stage, col_list,
            rid, col_list, doc, doc, rid, rid)

        # AFTER INSERT: remove the previously indexed values (REPLACE only),
        # discard the staged row (IGNORE conflicts leave it here harmlessly),
        # then index the values that were actually written.
        ai_sql = (
            'CREATE TRIGGER %s AFTER INSERT ON %s BEGIN\n'
            '  INSERT INTO %s (%s, "rowid", %s)\n'
            '  SELECT %s, "rid", %s FROM %s WHERE "rid" IS new.%s;\n'
            '  DELETE FROM %s WHERE "rid" IS new.%s;\n'
            '  INSERT INTO %s ("rowid", %s) VALUES(new.%s, %s);\n'
            'END;') % (
            q(triggers['ai']), doc,
            fts, fts, col_list,
            delete_cmd, col_list, stage, rid,
            stage, rid,
            fts, col_list, rid, new_vals)

        au_sql = (
            'CREATE TRIGGER %s AFTER UPDATE ON %s BEGIN\n'
            '  INSERT INTO %s (%s, "rowid", %s) VALUES(%s, old.%s, %s);\n'
            '  INSERT INTO %s ("rowid", %s) VALUES(new.%s, %s);\n'
            '  DELETE FROM %s WHERE "rid" IN (old.%s, new.%s);\n'
            'END;') % (
            q(triggers['au']), doc,
            fts, fts, col_list, delete_cmd, rid, old_vals,
            fts, col_list, rid, new_vals,
            stage, rid, rid)

        ad_sql = (
            'CREATE TRIGGER %s AFTER DELETE ON %s BEGIN\n'
            '  INSERT INTO %s (%s, "rowid", %s) VALUES(%s, old.%s, %s);\n'
            '  DELETE FROM %s WHERE "rid" IS old.%s;\n'
            'END;') % (
            q(triggers['ad']), doc,
            fts, fts, col_list, delete_cmd, rid, old_vals,
            stage, rid)

        for statement in (bi_sql, ai_sql, au_sql, ad_sql):
            db.execute_sql(statement)
        return names

    @classmethod
    def reconcile_external_index(cls, content_model=None):
        """
        Bring an external-content FTS5 index back in line with the content
        table row by row.

        A healthy index only receives the content rows that are missing from
        it; the index is not rebuilt. A drifted index (stale terms or orphan
        rows left by the old insert-only triggers) is rebuilt once inside the
        operation. Integrity is always re-verified afterwards.
        """
        content_model, content_table, rowid_col, columns = (
            cls._external_content_meta(content_model))
        db = cls._meta.database
        fts_table = cls._meta.table_name
        names = cls._sync_object_names(content_table)

        q = cls._quote
        fts = q(fts_table)
        doc = q(content_table)
        rid = q(rowid_col)
        cols = [q(column) for column in columns]
        col_list = ', '.join(cols)

        # rank 1 verifies the index against the content table. A failure
        # poisons the current statement, so probe inside a savepoint.
        drifted = False
        try:
            with db.savepoint():
                cls.integrity_check(rank=1)
        except DatabaseError:
            drifted = True

        if drifted:
            cls.delete_all()
            db.execute_sql(
                'INSERT INTO %s ("rowid", %s) SELECT %s, %s FROM %s' % (
                    fts, col_list, rid, col_list, doc))
        else:
            # Only the "row"/"col" fts5vocab tables report external-content
            # rowids unreliably; the instance table reports the real rowids.
            vocab_table = '%s_fts5_reconcile_vocab' % fts_table
            db.execute_sql(
                'CREATE VIRTUAL TABLE %s USING fts5vocab(%s, %s)' % (
                    q(vocab_table), fts, "'instance'"))
            try:
                db.execute_sql(
                    'INSERT INTO %s ("rowid", %s)\n'
                    'SELECT %s, %s FROM %s WHERE %s NOT IN (\n'
                    '  SELECT DISTINCT "doc" FROM %s)' % (
                        fts, col_list, rid, col_list, doc, rid,
                        q(vocab_table)))
            finally:
                db.execute_sql('DROP TABLE %s' % q(vocab_table))

        cls.integrity_check(rank=1)
        # INSERT OR IGNORE conflicts that ran before the install can leave
        # staging rows behind; nothing should remain once the index is aligned.
        stage_exists = db.execute_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (names['stage'],)).fetchone()
        if stage_exists:
            db.execute_sql('DELETE FROM %s' % q(names['stage']))
        return {'rebuilt': drifted}

    @classmethod
    def install_maintenance(cls, content_model=None):
        """
        Re-runnable, single-transaction maintenance install for an
        external-content FTS5 table.

        Replaces any triggers that previously maintained this index (including
        insert-only legacy triggers), installs triggers that correctly handle
        plain inserts, updates, deletes and ``INSERT OR REPLACE`` /
        ``INSERT OR IGNORE`` against the integer content rowid, then backfills
        the historical content. Every step is transactional DDL, so a failure
        rolls the whole install back and leaves the old behavior untouched.
        """
        db = cls._meta.database
        with db.atomic():
            names = cls.install_sync_triggers(content_model)
            result = cls.reconcile_external_index(content_model)
            result['names'] = names
            return result

    @classmethod
    def VocabModel(cls, table_type='row', table=None):
        if table_type not in ('row', 'col', 'instance'):
            raise ValueError('table_type must be either "row", "col" or '
                             '"instance".')

        # Each table-type needs a distinct default name, or the second type
        # silently binds to the first's table via IF NOT EXISTS.
        suffix = '_v' if table_type == 'row' else '_v_%s' % table_type

        class Meta:
            database = cls._meta.database
            table_name = table or cls._meta.table_name + suffix
            extension_module = fn.fts5vocab(
                cls._meta.entity,
                SQL(table_type))

        if table_type == 'row':
            columns = ('term', 'doc', 'cnt')
        elif table_type == 'col':
            columns = ('term', 'col', 'doc', 'cnt')
        else:
            columns = ('term', 'doc', 'col', 'offset')

        attrs = {c: TextField() if c in ('term', 'col') else IntegerField()
                 for c in columns}
        attrs['Meta'] = Meta

        # Built fresh per-call: caching would pin the database bound at the
        # time of the first call and ignore the table param thereafter.
        return type('%sVocab' % cls.__name__, (VirtualModel,), attrs)


OP.MATCH = 'MATCH'

def match(lhs, rhs):
    return Expression(lhs, OP.MATCH, rhs)
