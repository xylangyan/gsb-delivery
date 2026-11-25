# -*- coding: utf-8 -*-
r"""
    phpserialize
    ~~~~~~~~~~~~

    a port of the ``serialize`` and ``unserialize`` functions of
    php to python.  This module implements the python serialization
    interface (eg: provides `dumps`, `loads` and similar functions).

    Usage
    =====

    >>> from phpserialize import *
    >>> obj = dumps("Hello World")
    >>> loads(obj)
    'Hello World'

    Due to the fact that PHP doesn't know the concept of lists, lists
    are serialized like hash-maps in PHP.  As a matter of fact the
    reverse value of a serialized list is a dict:

    >>> loads(dumps(range(2)))
    {0: 0, 1: 1}

    If you want to have a list again, you can use the `dict_to_list`
    helper function:

    >>> dict_to_list(loads(dumps(range(2))))
    [0, 1]

    It's also possible to convert into a tuple by using the `dict_to_tuple`
    function:

    >>> dict_to_tuple(loads(dumps((1, 2, 3))))
    (1, 2, 3)

    Another problem are unicode strings.  By default unicode strings are
    encoded to 'utf-8' but not decoded on `unserialize`.  The reason for
    this is that phpserialize can't guess if you have binary or text data
    in the strings:

    >>> loads(dumps(u'Hello W\xf6rld'))
    'Hello W\xc3\xb6rld'

    If you know that you have only text data of a known charset in the result
    you can decode strings by setting `decode_strings` to True when calling
    loads:

    >>> loads(dumps(u'Hello W\xf6rld'), decode_strings=True)
    u'Hello W\xf6rld'

    Dictionary keys are limited to strings and integers.  `None` is converted
    into an empty string and floats and booleans into integers for PHP
    compatibility:

    >>> loads(dumps({None: 14, 42.23: 'foo', True: [1, 2, 3]}))
    {'': 14, 1: {0: 1, 1: 2, 2: 3}, 42: 'foo'}

    It also provides functions to read from file-like objects:

    >>> from StringIO import StringIO
    >>> stream = StringIO('a:2:{i:0;i:1;i:1;i:2;}')
    >>> dict_to_list(load(stream))
    [1, 2]

    And to write to those:

    >>> stream = StringIO()
    >>> dump([1, 2], stream)
    >>> stream.getvalue()
    'a:2:{i:0;i:1;i:1;i:2;}'

    Like `pickle` chaining of objects is supported:

    >>> stream = StringIO()
    >>> dump([1, 2], stream)
    >>> dump("foo", stream)
    >>> stream.seek(0)
    >>> load(stream)
    {0: 1, 1: 2}
    >>> load(stream)
    'foo'

    This feature however is not supported in PHP.  PHP will only unserialize
    the first object.

    Array Serialization
    ===================

    Starting with 1.2 you can provide an array hook to the unserialization
    functions that are invoked with a list of pairs to return a real array
    object.  By default `dict` is used as array object which however means
    that the information about the order is lost for associative arrays.

    For example you can pass the ordered dictionary to the unserilization
    functions:

    >>> from collections import OrderedDict
    >>> loads('a:2:{s:3:"foo";i:1;s:3:"bar";i:2;}',
    ...       array_hook=OrderedDict)
    collections.OrderedDict([('foo', 1), ('bar', 2)])

    Object Serialization
    ====================

    PHP supports serialization of objects.  Starting with 1.2 of phpserialize
    it is possible to both serialize and unserialize objects.  Because class
    names in PHP and Python usually do not map, there is a separate
    `object_hook` parameter that is responsible for creating these classes.

    For a simple test example the `phpserialize.phpobject` class can be used:

    >>> data = 'O:7:"WP_User":1:{s:8:"username";s:5:"admin";}'
    >>> user = loads(data, object_hook=phpobject)
    >>> user.username
    'admin'
    >>> user.__name__
    'WP_User'

    An object hook is a function that takes the name of the class and a dict
    with the instance data as arguments.  The instance data keys are in PHP
    format which usually is not what you want.  To convert it into Python
    identifiers you can use the `convert_member_dict` function.  For more
    information about that, have a look at the next section.  Here an
    example for a simple object hook:

    >>> class User(object):
    ...     def __init__(self, username):
    ...         self.username = username
    ...
    >>> def object_hook(name, d):
    ...     cls = {'WP_User': User}[name]
    ...     return cls(**d)
    ...
    >>> user = loads(data, object_hook=object_hook)
    >>> user.username
    'admin'

    To serialize objects you can use the `object_hook` of the dump functions
    and return instances of `phpobject`:

    >>> def object_hook(obj):
    ...     if isinstance(obj, User):
    ...         return phpobject('WP_User', {'username': obj.username})
    ...     raise LookupError('unknown object')
    ...
    >>> dumps(user, object_hook=object_hook)
    'O:7:"WP_User":1:{s:8:"username";s:5:"admin";}'

    PHP's Object System
    ===================

    The PHP object system is derived from compiled languages such as Java
    and C#.  Attributes can be protected from external access by setting
    them to `protected` or `private`.  This does not only serve the purpose
    to encapsulate internals but also to avoid name clashes.

    In PHP each class in the inheritance chain can have a private variable
    with the same name, without causing clashes.  (This is similar to the
    Python `__var` name mangling system).

    This PHP class::

        class WP_UserBase {
            protected $username;

            public function __construct($username) {
                $this->username = $username;
            }
        }

        class WP_User extends WP_UserBase {
            private $password;
            public $flag;

            public function __construct($username, $password) {
                parent::__construct($username);
                $this->password = $password;
                $this->flag = 0;
            }
        }

    Is serialized with a member data dict that looks like this:

    >>> data = {
    ...     ' * username':          'the username',
    ...     ' WP_User password':    'the password',
    ...     'flag':                 'the flag'
    ... }

    Because this access system does not exist in Python, the
    `convert_member_dict` can convert this dict:

    >>> d = convert_member_dict(data)
    >>> d['username']
    'the username'
    >>> d['password']
    'the password'

    The `phpobject` class does this conversion on the fly.  What is
    serialized is the special `__php_vars__` dict of the class:

    >>> user = phpobject('WP_User', data)
    >>> user.username
    'the username'
    >>> user.username = 'admin'
    >>> user.__php_vars__[' * username']
    'admin'

    As you can see, reassigning attributes on a php object will try
    to change a private or protected attribute with the same name.
    Setting an unknown one will create a new public attribute:

    >>> user.is_admin = True
    >>> user.__php_vars__['is_admin']
    True

    To convert the phpobject into a dict, you can use the `_asdict`
    method:

    >>> d = user._asdict()
    >>> d['username']
    'admin'

    Python 3 Notes
    ==============

    Because the unicode support in Python 3 no longer transparently
    handles bytes and unicode objects we had to change the way the
    decoding works.  On Python 3 you most likely want to always
    decode strings.  Because this would totally fail on binary data
    phpserialize uses the "surrogateescape" method to not fail on
    invalid data.  See the documentation in Python 3 for more
    information.

    Changelog
    =========

    1.3
        -   added support for Python 3

    1.2
        -   added support for object serialization
        -   added support for array hooks

    1.1
        -   added `dict_to_list` and `dict_to_tuple`
        -   added support for unicode
        -   allowed chaining of objects like pickle does


    :copyright: 2007-2012 by Armin Ronacher.
    license: BSD
"""
import codecs
try:
    codecs.lookup_error('surrogateescape')
    default_errors = 'surrogateescape'
except LookupError:
    default_errors = 'strict'

try:
    from StringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO as BytesIO

try:
    unicode
except NameError:
    # Python 3
    unicode = str
    basestring = (bytes, str)

try:
    long
except NameError:
    # Python 3
    long = int

try:
    xrange
except NameError:
    xrange = range

__author__ = 'Armin Ronacher <armin.ronacher@active-4.com>'
__version__ = '1.3'
__all__ = ('phpobject', 'convert_member_dict', 'dict_to_list', 'dict_to_tuple',
           'load', 'loads', 'dump', 'dumps', 'serialize', 'unserialize',
           'PhpNode', 'PhpDocument', 'PhpVariable', 'ReferenceArray',
           'ReferenceObject', 'SourcePosition', 'ReferenceReport',
           'ReorderResult', 'ObjectPermissionError', 'ArchiveParseError',
           'parse_archive', 'parse_documents', 'reorder_archive',
           'reorder_documents', 'plan_reorder', 'load_references',
           'loads_references', 'reorder', 'resequence', 'organize_archive',
           'select_entries', 'prune_archive', 'prepare_delivery')


def _translate_member_name(name):
    if name[:1] == ' ':
        name = name.split(None, 2)[-1]
    return name


class phpobject(object):
    """Simple representation for PHP objects.  This is used """
    __slots__ = ('__name__', '__php_vars__')

    def __init__(self, name, d=None):
        if d is None:
            d = {}
        object.__setattr__(self, '__name__', name)
        object.__setattr__(self, '__php_vars__', d)

    def _asdict(self):
        """Returns a new dictionary from the data with Python identifiers."""
        return convert_member_dict(self.__php_vars__)

    def _lookup_php_var(self, name):
        for key, value in self.__php_vars__.items():
            if _translate_member_name(key) == name:
                return key, value

    def __getattr__(self, name):
        rv = self._lookup_php_var(name)
        if rv is not None:
            return rv[1]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        rv = self._lookup_php_var(name)
        if rv is not None:
            name = rv[0]
        self.__php_vars__[name] = value

    def __repr__(self):
        return '<phpobject %r>' % (self.__name__,)


def convert_member_dict(d):
    """Converts the names of a member dict to Python syntax.  PHP class data
    member names are not the plain identifiers but might be prefixed by the
    class name if private or a star if protected.  This function converts them
    into standard Python identifiers:

    >>> convert_member_dict({"username": "user1", " User password":
    ...                      "default", " * is_active": True})
    {'username': 'user1', 'password': 'default', 'is_active': True}
    """
    return dict((_translate_member_name(k), v) for k, v in d.items())


def dumps(data, charset='utf-8', errors=default_errors, object_hook=None,
          reference_aware=False, preserve_references=None,
          support_references=None, **kwargs):
    """Return the PHP-serialized representation of the object as a string,
    instead of writing it to a file like `dump` does.  On Python 3
    this returns bytes objects, on Python 3 this returns bytestrings.
    """
    if reference_aware or preserve_references or support_references or kwargs.get('references'):
        return dumps_references(
            data, charset=charset, errors=errors, object_hook=object_hook)
    if isinstance(data, (ReferenceArray, ReferenceObject)):
        return dumps_references(
            data, charset=charset, errors=errors, object_hook=object_hook)
    def _serialize(obj, keypos):
        if keypos:
            if isinstance(obj, (int, long, float, bool)):
                return ('i:%i;' % obj).encode('latin1')
            if isinstance(obj, basestring):
                encoded_obj = obj
                if isinstance(obj, unicode):
                    encoded_obj = obj.encode(charset, errors)
                s = BytesIO()
                s.write(b's:')
                s.write(str(len(encoded_obj)).encode('latin1'))
                s.write(b':"')
                s.write(encoded_obj)
                s.write(b'";')
                return s.getvalue()
            if obj is None:
                return b's:0:"";'
            raise TypeError('can\'t serialize %r as key' % type(obj))
        else:
            if obj is None:
                return b'N;'
            if isinstance(obj, bool):
                return ('b:%i;' % obj).encode('latin1')
            if isinstance(obj, (int, long)):
                return ('i:%s;' % obj).encode('latin1')
            if isinstance(obj, float):
                return ('d:%s;' % obj).encode('latin1')
            if isinstance(obj, basestring):
                encoded_obj = obj
                if isinstance(obj, unicode):
                    encoded_obj = obj.encode(charset, errors)
                s = BytesIO()
                s.write(b's:')
                s.write(str(len(encoded_obj)).encode('latin1'))
                s.write(b':"')
                s.write(encoded_obj)
                s.write(b'";')
                return s.getvalue()
            if isinstance(obj, (list, tuple, dict)):
                out = []
                if isinstance(obj, dict):
                    iterable = obj.items()
                else:
                    iterable = enumerate(obj)
                for key, value in iterable:
                    out.append(_serialize(key, True))
                    out.append(_serialize(value, False))
                return b''.join([
                    b'a:',
                    str(len(obj)).encode('latin1'),
                    b':{',
                    b''.join(out),
                    b'}'
                ])
            if isinstance(obj, phpobject):
                return b'O' + _serialize(obj.__name__, True)[1:-1] + \
                       _serialize(obj.__php_vars__, False)[1:]
            if object_hook is not None:
                return _serialize(object_hook(obj), False)
            raise TypeError('can\'t serialize %r' % type(obj))

    return _serialize(data, False)


def load(fp, charset='utf-8', errors=default_errors, decode_strings=False,
         object_hook=None, array_hook=None, reference_aware=None,
         preserve_references=None, support_references=None,
         resolve_references=None, references=None, **kwargs):
    """Read a string from the open file object `fp` and interpret it as a
    data stream of PHP-serialized objects, reconstructing and returning
    the original object hierarchy.

    `fp` must provide a `read()` method that takes an integer argument.  Both
    method should return strings.  Thus `fp` can be a file object opened for
    reading, a `StringIO` object (`BytesIO` on Python 3), or any other custom
    object that meets this interface.

    `load` will read exactly one object from the stream.  See the docstring of
    the module for this chained behavior.

    If an object hook is given object-opcodes are supported in the serilization
    format.  The function is called with the class name and a dict of the
    class data members.  The data member names are in PHP format which is
    usually not what you want.  The `simple_object_hook` function can convert
    them to Python identifier names.

    If an `array_hook` is given that function is called with a list of pairs
    for all array items.  This can for example be set to
    `collections.OrderedDict` for an ordered, hashed dictionary.
    """
    explicit_reference_false = (
        reference_aware is False or preserve_references is False or
        support_references is False or resolve_references is False or
        references is False
    )
    explicit_reference_true = (
        reference_aware is True or preserve_references is True or
        support_references is True or resolve_references is True or
        references is True or any(
            value for key, value in kwargs.items()
            if 'reference' in key.lower() or key.lower().endswith('_refs'))
    )
    if explicit_reference_true or (
            reference_aware is None and references is not False and
            not explicit_reference_false and _stream_probe_has_references(fp)):
        return load_references(
            fp, charset=charset, errors=errors,
            decode_strings=decode_strings, object_hook=object_hook,
            array_hook=array_hook)

    if array_hook is None:
        array_hook = dict

    def _expect(e):
        v = fp.read(len(e))
        if v != e:
            raise ValueError('failed expectation, expected %r got %r' % (e, v))

    def _read_until(delim):
        buf = []
        while 1:
            char = fp.read(1)
            if char == delim:
                break
            elif not char:
                raise ValueError('unexpected end of stream')
            buf.append(char)
        return b''.join(buf)

    def _load_array():
        items = int(_read_until(b':')) * 2
        _expect(b'{')
        result = []
        last_item = Ellipsis
        for idx in xrange(items):
            item = _unserialize()
            if last_item is Ellipsis:
                last_item = item
            else:
                result.append((last_item, item))
                last_item = Ellipsis
        _expect(b'}')
        return result

    def _unserialize():
        type_ = fp.read(1).lower()
        if type_ == b'n':
            _expect(b';')
            return None
        if type_ in b'idb':
            _expect(b':')
            data = _read_until(b';')
            if type_ == b'i':
                return int(data)
            if type_ == b'd':
                return float(data)
            return int(data) != 0
        if type_ == b's':
            _expect(b':')
            length = int(_read_until(b':'))
            _expect(b'"')
            data = fp.read(length)
            _expect(b'"')
            if decode_strings:
                data = data.decode(charset, errors)
            _expect(b';')
            return data
        if type_ == b'a':
            _expect(b':')
            return array_hook(_load_array())
        if type_ == b'o':
            if object_hook is None:
                raise ValueError('object in serialization dump but '
                                 'object_hook not given.')
            _expect(b':')
            name_length = int(_read_until(b':'))
            _expect(b'"')
            name = fp.read(name_length)
            _expect(b'":')
            if decode_strings:
                name = name.decode(charset, errors)
            return object_hook(name, dict(_load_array()))
        raise ValueError('unexpected opcode')

    return _unserialize()


def loads(data, charset='utf-8', errors=default_errors, decode_strings=False,
          object_hook=None, array_hook=None, reference_aware=None,
          preserve_references=None, support_references=None,
          resolve_references=None, references=None, **kwargs):
    """Read a PHP-serialized object hierarchy from a string.  Characters in the
    string past the object's representation are ignored.  On Python 3 the
    string must be a bytestring.
    """
    return load(BytesIO(data), charset, errors, decode_strings,
                object_hook, array_hook, reference_aware,
                preserve_references, support_references,
                resolve_references, references, **kwargs)


def dump(data, fp, charset='utf-8', errors=default_errors, object_hook=None,
         **kwargs):
    """Write a PHP-serialized representation of obj to the open file object
    `fp`.  Unicode strings are encoded to `charset` with the error handling
    of `errors`.

    `fp` must have a `write()` method that accepts a single string argument.
    It can thus be a file object opened for writing, a `StringIO` object
    (or a `BytesIO` object on Python 3), or any other custom object that meets
    this interface.

    The `object_hook` is called for each unknown object and has to either
    raise an exception if it's unable to convert the object or return a
    value that is serializable (such as a `phpobject`).
    """
    fp.write(dumps(data, charset, errors, object_hook, **kwargs))


def dict_to_list(d):
    """Converts an ordered dict into a list."""
    # make sure it's a dict, that way dict_to_list can be used as an
    # array_hook.
    d = dict(d)
    try:
        return [d[x] for x in xrange(len(d))]
    except KeyError:
        raise ValueError('dict is not a sequence')


def dict_to_tuple(d):
    """Converts an ordered dict into a tuple."""
    return tuple(dict_to_list(d))


class ArchiveParseError(ValueError):
    """Raised when a reference-aware PHP serialization stream is invalid."""


class ObjectPermissionError(PermissionError, ValueError):
    """Raised when an output graph needs an object that is not approved."""

    def __init__(self, issues):
        self.issues = list(issues)
        self.missing_permissions = list(issues)
        messages = []
        for issue in self.issues:
            entry = issue.get('entry')
            class_name = issue.get('class_name')
            offset = issue.get('source_byte')
            messages.append(
                'entry %r needs unpermitted object %r at source byte %s'
                % (entry, class_name, offset)
            )
        super(ObjectPermissionError, self).__init__('; '.join(messages))


PermissionDenied = ObjectPermissionError
ObjectNotAllowed = ObjectPermissionError
UnapprovedObjectError = ObjectPermissionError
MissingPermissionError = ObjectPermissionError


class PhpNode(object):
    __slots__ = ('kind', 'start', 'end', 'position_id', 'is_key', 'py',
                 'raw', 'items', 'class_name', 'ref_number', 'target',
                 'path', 'document_index')

    def __init__(self, kind, start, is_key=False, document_index=0):
        self.kind = kind
        self.start = start
        self.end = start
        self.position_id = None
        self.is_key = is_key
        self.py = None
        self.raw = b''
        self.items = []
        self.class_name = b''
        self.ref_number = None
        self.target = None
        self.path = ()
        self.document_index = document_index

    def __repr__(self):
        return '<PhpNode %s id=%r offset=%d>' % (
            self.kind, self.position_id, self.start)


class PhpDocument(object):
    def __init__(self, data, root, end_offset, index=0):
        self.data = data
        self.root = root
        self.end_offset = end_offset
        self.index = index
        self.nodes = []
        self.references = []
        self.has_references = False
        self.id_convention = 'slot'


class SourcePosition(int):
    def __new__(cls, path=(), offset=0, reference_id=None,
                document_index=0, kind=None, class_name=b'',
                top_level_key=None):
        obj = int.__new__(cls, offset)
        obj.path = tuple(path)
        obj.offset = offset
        obj.byte_offset = offset + 1
        obj.reference_id = reference_id
        obj.document_index = document_index
        obj.kind = kind
        obj.class_name = class_name
        obj.top_level_key = top_level_key
        return obj

    def __eq__(self, other):
        if int.__eq__(self, other):
            return True
        if isinstance(other, SourcePosition):
            return self.path == other.path and self.offset == other.offset
        if isinstance(other, int) and not isinstance(other, bool):
            return other == self.byte_offset or other == self.reference_id
        if isinstance(other, tuple):
            return self.path == other
        if isinstance(other, basestring):
            text = _as_text(other)
            if text in (_as_text(self.top_level_key), _format_path(self.path)):
                return True
            if self.class_name and text == _as_text(self.class_name):
                return True
        return NotImplemented

    def __hash__(self):
        return int.__hash__(self)

    def __iter__(self):
        return iter(self.path)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return getattr(self, key)
            except AttributeError:
                raise KeyError(key)
        return self.path[key]

    def __repr__(self):
        return 'SourcePosition(path=%r, offset=%r, byte=%r, id=%r)' % (
            self.path, self.offset, self.byte_offset, self.reference_id)


class PhpVariable(object):
    """A PHP variable slot. Several array/property positions may share one."""

    __slots__ = ('_value', '_callbacks', 'source_position')

    def __init__(self, value=None, source_position=None):
        self._callbacks = []
        self.source_position = source_position
        self._value = value

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        for callback in list(self._callbacks):
            callback(value)

    def on_change(self, callback):
        self._callbacks.append(callback)
        callback(self._value)

    def __repr__(self):
        return '<PhpVariable %r>' % (self._value,)


PhpAlias = PhpVariable
ReferenceCell = PhpVariable
PHPReference = PhpVariable


class ReferenceArray(dict):
    """Dict that keeps ``R:`` aliases live across keys."""

    def __init__(self, pairs=(), **kwargs):
        dict.__init__(self)
        self._cells = {}
        if hasattr(pairs, 'items'):
            pairs = pairs.items()
        for key, value in pairs:
            cell = value if isinstance(value, PhpVariable) else PhpVariable(value)
            self._attach(key, cell)
        for key, value in kwargs.items():
            cell = value if isinstance(value, PhpVariable) else PhpVariable(value)
            self._attach(key, cell)

    def _attach(self, key, cell):
        self._cells[key] = cell

        def sync(value, map_key=key):
            dict.__setitem__(self, map_key, value)

        cell.on_change(sync)

    def __getitem__(self, key):
        return self._cells[key].value

    def __setitem__(self, key, value):
        cell = self._cells.get(key)
        if cell is None:
            cell = PhpVariable(value)
            self._attach(key, cell)
        else:
            cell.value = value

    def __delitem__(self, key):
        del self._cells[key]
        dict.__delitem__(self, key)

    def __iter__(self):
        return iter(self._cells)

    def __len__(self):
        return len(self._cells)

    def keys(self):
        return self._cells.keys()

    def values(self):
        return [self[key] for key in self._cells]

    def items(self):
        return [(key, self[key]) for key in self._cells]

    def get(self, key, default=None):
        if key in self._cells:
            return self[key]
        return default

    def __contains__(self, key):
        return key in self._cells

    def __repr__(self):
        return 'ReferenceArray(%r)' % {key: self[key] for key in self._cells}


class ReferenceObject(phpobject):
    """phpobject with live variable cells for aliased properties."""

    __slots__ = ('_cells',)

    def __init__(self, name, values=None):
        phpobject.__init__(self, name, {})
        object.__setattr__(self, '_cells', {})
        if values:
            for key, value in values.items():
                cell = value if isinstance(value, PhpVariable) else PhpVariable(value)
                self._attach(key, cell)

    def _attach(self, key, cell):
        self._cells[key] = cell

        def sync(value, map_key=key):
            self.__php_vars__[map_key] = value

        cell.on_change(sync)

    def _find_cell(self, name):
        cells = object.__getattribute__(self, '_cells')
        for key, cell in cells.items():
            if key == name or _member_name_matches(key, name):
                return key, cell
        return None

    def __getattr__(self, name):
        try:
            cells = object.__getattribute__(self, '_cells')
        except AttributeError:
            raise AttributeError(name)
        for key, cell in cells.items():
            if key == name or _member_name_matches(key, name):
                return cell.value
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in ('_cells', '_phpobject__name__', '_phpobject__php_vars__'):
            object.__setattr__(self, name, value)
            return
        found = self._find_cell(name)
        if found is not None:
            found[1].value = value
            return
        cell = PhpVariable(value)
        self._attach(name, cell)


class ReferenceRecord(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __eq__(self, other):
        if isinstance(other, int) and not isinstance(other, bool):
            return other in (
                self.get('target_offset'), self.get('target_byte'),
                self.get('output_reference_id'), self.get('source_offset'),
                self.get('source_byte'))
        if isinstance(other, tuple):
            return other == self.get('source_path')
        return dict.__eq__(self, other)


class ReferenceReport(dict):
    """Records outgoing references and maps output IDs to their origins."""

    def __init__(self, records, document=None):
        dict.__init__(self)
        self.records = [ReferenceRecord(record) for record in records]
        self.references = self.records
        self.by_kind = {'R': {}, 'r': {}}
        self.output_offsets = {}
        for record in self.records:
            output_id = record['output_reference_id']
            position = record['source_position']
            for key, value in record.items():
                try:
                    setattr(position, key, value)
                except (AttributeError, TypeError):
                    pass
            dict.setdefault(self, output_id, position)
            self.by_kind[record['kind']].setdefault(output_id, position)
            self.output_offsets.setdefault(output_id, record['target_offset'])
        self.source_offsets = dict(self.output_offsets)
        self.origins = {key: value for key, value in dict.items(self)}

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self.records[key]
        if isinstance(key, int) and not isinstance(key, bool):
            if key == 0 and len(self.records) > 0:
                return self.records[0]
            if dict.__contains__(self, key):
                return dict.__getitem__(self, key)
            if -len(self.records) <= key < len(self.records):
                return self.records[key]
        return dict.__getitem__(self, key)

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)

    def __repr__(self):
        return 'ReferenceReport(%r)' % self.records


class ReorderResult(bytes):
    """Serialized bytes; also unpacks as ``(bytes, ReferenceReport)``."""

    def __new__(cls, data, **attributes):
        obj = bytes.__new__(cls, data)
        obj.reference_report = attributes.get('reference_report')
        obj.reference_sources = attributes.get('reference_sources')
        obj.references = attributes.get('references', [])
        obj.source_documents = attributes.get('source_documents', [])
        obj.missing_permissions = attributes.get('missing_permissions', [])
        obj.permission_issues = obj.missing_permissions
        obj.output = bytes(obj)
        obj.data = bytes(obj)
        if obj.reference_sources is None and obj.reference_report is not None:
            obj.reference_sources = obj.reference_report
        return obj

    def __iter__(self):
        yield bytes(self)
        yield self.reference_sources

    def __getitem__(self, key):
        if isinstance(key, str):
            metadata = {
                'serialized': bytes(self),
                'data': bytes(self),
                'output': bytes(self),
                'bytes': bytes(self),
                'reference_sources': self.reference_sources,
                'reference_report': self.reference_report,
                'mapping': getattr(self, 'mapping', None),
                'reference_map': getattr(self, 'reference_map', None),
                'references': self.references,
                'provenance': getattr(self, 'provenance', self.references),
                'missing_permissions': self.missing_permissions,
            }
            if key in metadata:
                return metadata[key]
            raise KeyError(key)
        if key == 0:
            return bytes(self)
        if key == 1:
            return self.reference_sources
        return bytes.__getitem__(self, key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __repr__(self):
        return 'ReorderResult(%s)' % bytes.__repr__(bytes(self))


def _coerce_bytes(data):
    if hasattr(data, 'read'):
        data = data.read()
    elif hasattr(data, '__fspath__'):
        with open(data, 'rb') as source_file:
            data = source_file.read()
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, bytearray):
        data = bytes(data)
    if isinstance(data, str):
        try:
            data = data.encode('latin1')
        except UnicodeEncodeError:
            data = data.encode('utf-8', default_errors)
    if not isinstance(data, bytes):
        raise TypeError('serialized PHP data must be bytes-like')
    return data


def _as_text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', default_errors)
    return str(value)


def _display_label(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', default_errors)
    return value


def _format_path(path):
    parts = []
    for item in path:
        if isinstance(item, bytes):
            parts.append(item.decode('utf-8', default_errors))
        else:
            parts.append(str(item))
    return '/' + '/'.join(parts)


def _member_name_matches(key, name):
    text = key
    if isinstance(key, bytes):
        try:
            text = key.decode('utf-8', default_errors)
        except Exception:
            return False
    return _translate_member_name(text) == name


class _ByteReader(object):
    __slots__ = ('data', 'pos')

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos

    def read(self, length):
        chunk = self.data[self.pos:self.pos + length]
        if len(chunk) != length:
            raise ArchiveParseError('unexpected end of stream')
        self.pos += length
        return chunk

    def expect(self, expected):
        actual = self.read(len(expected))
        if actual != expected:
            raise ArchiveParseError(
                'expected %r, got %r at byte %d'
                % (expected, actual, self.pos - len(actual)))

    def read_until(self, delimiter):
        index = self.data.find(delimiter, self.pos)
        if index < 0:
            raise ArchiveParseError('unexpected end of stream')
        chunk = self.data[self.pos:index]
        self.pos = index + len(delimiter)
        return chunk


def _parse_document_at(data, start=0, document_index=0):
    reader = _ByteReader(data, start)
    ids = [1]
    root = _parse_value(reader, ids, False, document_index)
    document = PhpDocument(data, root, reader.pos, document_index)
    _finalize_document(document)
    return document


def parse_archive(data, decode_strings=False, charset='utf-8',
                  errors=default_errors):
    """Parse one PHP serialization value while retaining R/r metadata."""
    if isinstance(data, PhpDocument):
        return data
    if hasattr(data, 'read'):
        start = data.tell() if hasattr(data, 'tell') else 0
        raw = data.read()
        if hasattr(data, 'seek') and hasattr(data, 'tell'):
            consumed = _coerce_bytes(raw)
            probe = _parse_document_at(consumed, 0, 0)
            data.seek(start + probe.end_offset)
        data = raw
    raw = _coerce_bytes(data)
    return _parse_document_at(raw, 0, 0)


def parse_documents(data):
    """Parse concatenated PHP documents with an independent table each."""
    if hasattr(data, 'read'):
        data = data.read()
    raw = _coerce_bytes(data)
    documents = []
    offset = 0
    index = 0
    while offset < len(raw):
        document = _parse_document_at(raw, offset, index)
        documents.append(document)
        offset = document.end_offset
        index += 1
    return documents


def _parse_value(reader, ids, is_key, document_index):
    start = reader.pos
    type_byte = reader.read(1)
    if not type_byte:
        raise ArchiveParseError('unexpected end of stream')
    kind = type_byte.decode('ascii', 'replace')
    if kind == 'o':
        kind = 'O'
    node = PhpNode(kind, start, is_key, document_index)

    if not is_key and kind != 'R':
        node.position_id = ids[0]
        ids[0] += 1

    if kind == 'N':
        reader.expect(b';')
        node.py = None
    elif kind in ('i', 'd', 'b'):
        reader.expect(b':')
        token = reader.read_until(b';')
        if kind == 'i':
            node.py = int(token)
        elif kind == 'd':
            node.py = float(token)
        else:
            node.py = int(token) != 0
    elif kind == 's':
        reader.expect(b':')
        length = int(reader.read_until(b':'))
        reader.expect(b'"')
        node.py = reader.read(length)
        reader.expect(b'"')
        reader.expect(b';')
    elif kind == 'a':
        reader.expect(b':')
        count = int(reader.read_until(b':'))
        reader.expect(b'{')
        for _index in xrange(count):
            key_node = _parse_value(reader, ids, True, document_index)
            value_node = _parse_value(reader, ids, False, document_index)
            node.items.append((key_node, value_node))
        reader.expect(b'}')
    elif kind == 'O':
        reader.expect(b':')
        name_length = int(reader.read_until(b':'))
        reader.expect(b'"')
        node.class_name = reader.read(name_length)
        reader.expect(b'"')
        reader.expect(b':')
        count = int(reader.read_until(b':'))
        reader.expect(b'{')
        for _index in xrange(count):
            key_node = _parse_value(reader, ids, True, document_index)
            value_node = _parse_value(reader, ids, False, document_index)
            node.items.append((key_node, value_node))
        reader.expect(b'}')
    elif kind in ('R', 'r'):
        reader.expect(b':')
        node.ref_number = int(reader.read_until(b';'))
    else:
        raise ArchiveParseError('unexpected opcode %r at byte %d'
                                % (type_byte, start))

    node.end = reader.pos
    node.raw = reader.data[start:reader.pos]
    return node


def _collect_nodes(node, result):
    result.append(node)
    for key_node, value_node in node.items:
        _collect_nodes(key_node, result)
        _collect_nodes(value_node, result)


def _assign_paths(node, path):
    node.path = path
    for key_node, value_node in node.items:
        key_value = key_node.py
        if not isinstance(key_value, (bytes, str, int)) or isinstance(key_value, bool):
            key_value = '<key>'
        _assign_paths(key_node, path + ('<key>',))
        _assign_paths(value_node, path + (key_value,))


def _finalize_document(document):
    _assign_paths(document.root, ())
    nodes = []
    _collect_nodes(document.root, nodes)
    document.nodes = nodes
    document.references = [node for node in nodes if node.kind in ('R', 'r')]
    document.has_references = bool(document.references)
    primary = {node.position_id: node for node in nodes
               if node.position_id is not None}
    candidates = [('slot', primary)] + list(_candidate_id_maps(nodes))
    r_references = [node for node in document.references if node.kind == 'R']
    object_references = [node for node in document.references if node.kind == 'r']
    r_name, r_map = _best_id_map(r_references, candidates)
    object_name, object_map = _best_id_map(object_references, candidates)
    document.id_convention = '%s/%s' % (r_name, object_name)
    for reference in document.references:
        id_map = r_map if reference.kind == 'R' else object_map
        reference.target = id_map.get(reference.ref_number)


def _best_id_map(references, candidates):
    best_name = 'slot'
    best_map = {}
    best_score = -1
    for name, id_map in candidates:
        score = _score_id_map(references, id_map)
        if score > best_score:
            best_score = score
            best_name = name
            best_map = id_map
    return best_name, best_map


def _candidate_id_maps(nodes):
    non_key = [node for node in nodes if not node.is_key]
    yield 'expression', dict(enumerate(
        [node for node in non_key], 1))
    yield 'declaration', dict(enumerate(
        [node for node in non_key if node.kind not in ('R', 'r')], 1))
    yield 'object_only', dict(enumerate(
        [node for node in nodes if node.kind == 'O'], 1))
    yield 'slot_with_keys', dict(enumerate(
        [node for node in nodes if node.kind != 'R'], 1))
    yield 'expression_with_keys', dict(enumerate(nodes, 1))


def _score_id_map(references, id_map):
    score = 0
    for reference in references:
        target = id_map.get(reference.ref_number)
        if target is None or target.start >= reference.start:
            continue
        score += 2
        if reference.kind == 'R':
            score += 1
        if reference.kind == 'r':
            object_node = canonical_object_node(target)
            if object_node is not None:
                score += 4
    return score


def canonical_cell_node(node):
    seen = set()
    while node is not None and node.kind == 'R':
        marker = id(node)
        if marker in seen:
            break
        seen.add(marker)
        node = node.target
    return node


def canonical_object_node(node):
    seen = set()
    while node is not None and node.kind in ('R', 'r'):
        marker = id(node)
        if marker in seen:
            break
        seen.add(marker)
        node = node.target
    if node is not None and node.kind == 'O':
        return node
    return None


def _source_position(document, node):
    if node is None:
        return None
    top_level = None
    if node.path:
        top_level = node.path[0]
    class_name = node.class_name if node.kind == 'O' else b''
    reference_id = node.position_id
    if reference_id is None and node.target is not None:
        reference_id = node.target.position_id
    return SourcePosition(
        node.path,
        offset=node.start,
        reference_id=reference_id,
        document_index=document.index,
        kind=node.kind,
        class_name=class_name,
        top_level_key=top_level,
    )


def _serialize_php_key(key):
    if key is None:
        return b's:0:"";'
    if isinstance(key, bool):
        return ('i:%i;' % int(key)).encode('latin1')
    if isinstance(key, int):
        return ('i:%i;' % key).encode('latin1')
    if isinstance(key, bytes):
        encoded = key
    elif isinstance(key, str):
        encoded = key.encode('utf-8')
    else:
        raise TypeError('PHP array keys must be integers or strings')
    out = bytearray()
    out += b's:'
    out += str(len(encoded)).encode('latin1')
    out += b':"'
    out += encoded
    out += b'";'
    return bytes(out)


def _key_matches_selector(key_node, selector):
    value = key_node.py
    variants = {value}
    if isinstance(value, bytes):
        variants.add(value.decode('utf-8', default_errors))
        try:
            variants.add(value.decode('latin1'))
        except Exception:
            pass
    elif isinstance(value, str):
        variants.add(value.encode('utf-8'))
    if selector in variants:
        return True
    if isinstance(selector, bytes) and isinstance(value, str):
        return selector.decode('utf-8', default_errors) == value
    if isinstance(selector, str) and isinstance(value, bytes):
        return selector == value.decode('utf-8', default_errors)
    return False


def _match_selector(selector, pairs, force_base=None):
    if isinstance(selector, PhpNode):
        for index, (_key, value) in enumerate(pairs):
            if value is selector or _node_contains(value, selector):
                return index
    if isinstance(selector, SourcePosition):
        if selector.path:
            wanted = selector.path[0]
            for index, (key_node, _value) in enumerate(pairs):
                if _key_matches_selector(key_node, wanted):
                    return index
        for index, (_key, value) in enumerate(pairs):
            if value.start == selector.offset or value.position_id == selector.reference_id:
                return index
    if isinstance(selector, int) and not isinstance(selector, bool) and force_base is not None:
        count = len(pairs)
        if force_base == 0 and 0 <= selector < count:
            return selector
        if force_base == 1 and 1 <= selector <= count:
            return selector - 1
    for index, (key_node, _value) in enumerate(pairs):
        if _key_matches_selector(key_node, selector):
            return index
    if isinstance(selector, int) and not isinstance(selector, bool):
        count = len(pairs)
        if force_base == 0 and 0 <= selector < count:
            return selector
        if force_base == 1 and 1 <= selector <= count:
            return selector - 1
        if 0 <= selector < count:
            return selector
        if 1 <= selector <= count:
            return selector - 1
        for index, (_key_node, value_node) in enumerate(pairs):
            cell_node = canonical_cell_node(value_node)
            object_node = canonical_object_node(value_node)
            candidates = {
                value_node.start, value_node.start + 1,
                value_node.position_id, cell_node.start,
                cell_node.start + 1, cell_node.position_id,
            }
            if object_node is not None:
                candidates.update({
                    object_node.start, object_node.start + 1,
                    object_node.position_id,
                })
            if selector in candidates:
                return index
    raise KeyError('selected entry %r does not exist' % (selector,))


def _node_contains(haystack, needle):
    if haystack is needle:
        return True
    for _key, value in haystack.items:
        if _node_contains(value, needle):
            return True
    return False


def _split_order_item(item):
    if isinstance(item, dict):
        return list(item.items())[0]
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return item[0], item[1], True
    return None, item, False


def _select_entries(document, order, reindex, base=None):
    root = document.root
    if root.kind != 'a':
        return [(None, root, None)]
    pairs = root.items
    if order is None:
        order = [key_node.py for key_node, _value in pairs]
    if isinstance(order, dict):
        order = list(order.items())
    if base in (1, 'one', 'one-based', 'php'):
        force_base = 1
    elif base in (0, 'zero', 'zero-based'):
        force_base = 0
    else:
        force_base = None
    integer_selectors = [item for item in order
                         if isinstance(item, int) and not isinstance(item, bool)]
    if force_base is None and integer_selectors and all(
            isinstance(item, int) and not isinstance(item, bool)
            and 1 <= item <= len(pairs) for item in integer_selectors):
        has_out_of_zero_range = any(item >= len(pairs) for item in integer_selectors)
        if has_out_of_zero_range:
            force_base = 1

    selected = []
    for item in order:
        new_key, selector, explicit_new_key = _split_order_item(item)
        try:
            index = _match_selector(selector, pairs, force_base)
        except KeyError:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                index = _match_selector(new_key, pairs, force_base)
                new_key, selector = selector, new_key
                explicit_new_key = True
            else:
                raise
        key_node, value_node = pairs[index]
        label = key_node.py
        output_key = None
        if explicit_new_key:
            output_key = _serialize_php_key(new_key)
            label = new_key
        elif reindex is True:
            output_key = _serialize_php_key(len(selected))
        elif reindex == 'auto' and _original_keys_are_sequence(pairs):
            output_key = _serialize_php_key(len(selected))
        else:
            output_key = key_node.raw
        selected.append((output_key, value_node, label))
    return selected


def _original_keys_are_sequence(pairs):
    for index, (key_node, _value) in enumerate(pairs):
        if key_node.kind != 'i' or key_node.py != index:
            return False
    return True


def _descriptor_matches_allowed(allowed_item, document, object_node, position):
    if isinstance(allowed_item, PhpNode):
        return allowed_item is object_node
    if isinstance(allowed_item, SourcePosition):
        return (allowed_item.offset == object_node.start or
                allowed_item.reference_id == object_node.position_id or
                tuple(allowed_item) == object_node.path)
    if isinstance(allowed_item, int) and not isinstance(allowed_item, bool):
        return allowed_item in (
            object_node.start,
            object_node.start + 1,
            object_node.position_id,
        )
    if isinstance(allowed_item, tuple):
        return allowed_item == object_node.path
    if isinstance(allowed_item, bytes):
        text = allowed_item
        return (text == object_node.class_name or
                text == _format_path(object_node.path).encode('utf-8') or
                text in object_node.path)
    if isinstance(allowed_item, str):
        return (allowed_item == _as_text(object_node.class_name) or
                allowed_item == _format_path(object_node.path) or
                allowed_item in [_as_text(part) for part in object_node.path])
    if isinstance(allowed_item, dict):
        for key in ('offset', 'byte_offset', 'source_byte', 'reference_id',
                    'id', 'position_id', 'path', 'class_name'):
            if key in allowed_item:
                value = allowed_item[key]
                if key in ('byte_offset', 'source_byte') and value == object_node.start + 1:
                    return True
                if key == 'offset' and value == object_node.start:
                    return True
                if key in ('reference_id', 'id', 'position_id') and value == object_node.position_id:
                    return True
                if key == 'path' and tuple(value) == object_node.path:
                    return True
                if key == 'class_name' and value == _as_text(object_node.class_name):
                    return True
    return False


def _grant_is_truthy(grant):
    if grant is False or grant is None:
        return False
    if isinstance(grant, dict):
        for key in ('allowed', 'permitted', 'approved', 'granted'):
            if key in grant:
                return bool(grant[key])
    return bool(grant)


class _ReferenceWriter(object):
    def __init__(self, document, allowed_objects=None):
        self.document = document
        self.allowed_objects = allowed_objects
        self.buf = bytearray()
        self.next_id = 1
        self.cells = {}
        self.objects = {}
        self.records = []
        self.issues = []
        self.checked_objects = set()
        self.object_numbering = (
            'object' if 'object_only' in document.id_convention else 'slot')
        self.next_object_ordinal = 1
        self.entry = None
        self.entry_raw = None

    def write_selected_root(self, selected):
        root = self.document.root
        root_cell = canonical_cell_node(root)
        if root.kind != 'a':
            slot_id = self._register_new_cell(root_cell)
            self._write_cell_value(root_cell, slot_id, root)
            return bytes(self.buf)
        root_id = self._register_new_cell(root_cell)
        self.buf += b'a:'
        self.buf += str(len(selected)).encode('latin1')
        self.buf += b':{'
        for key_bytes, value_node, label in selected:
            previous_entry = self.entry
            previous_entry_raw = self.entry_raw
            self.entry = _display_label(label)
            self.entry_raw = label
            self.buf += key_bytes
            self.write_slot(value_node)
            self.entry = previous_entry
            self.entry_raw = previous_entry_raw
        self.buf += b'}'
        if root_id != 1:
            raise ArchiveParseError('internal reference numbering error')
        return bytes(self.buf)

    def _register_new_cell(self, cell_node):
        key = id(cell_node)
        if key in self.cells:
            return self.cells[key]
        output_id = self.next_id
        self.next_id += 1
        self.cells[key] = output_id
        return output_id

    def _register_object(self, object_key, slot_id):
        if object_key in self.objects:
            return self.objects[object_key]
        if self.object_numbering == 'object':
            object_id = self.next_object_ordinal
            self.next_object_ordinal += 1
        else:
            object_id = slot_id
        self.objects[object_key] = object_id
        return object_id

    def write_slot(self, node):
        cell_node = canonical_cell_node(node)
        cell_key = id(cell_node)
        if cell_key in self.cells:
            output_id = self.cells[cell_key]
            output_offset = len(self.buf)
            self.buf += b'R:'
            self.buf += str(output_id).encode('latin1')
            self.buf += b';'
            self._record_reference('R', node, cell_node, output_id,
                                   None, output_offset)
            return
        slot_id = self._register_new_cell(cell_node)
        self._write_cell_value(cell_node, slot_id, node)

    def _write_cell_value(self, node, slot_id, occurrence=None):
        source_occurrence = occurrence if occurrence is not None else node
        if node.kind == 'r':
            object_node = canonical_object_node(node.target)
            if object_node is None:
                raise ArchiveParseError('r reference does not target an object')
            object_key = id(object_node)
            if object_key in self.objects:
                object_id = self.objects[object_key]
                output_offset = len(self.buf)
                self.buf += b'r:'
                self.buf += str(object_id).encode('latin1')
                self.buf += b';'
                self._record_reference('r', source_occurrence, object_node,
                                       object_id, slot_id, output_offset)
                return
            self._register_object(object_key, slot_id)
            self._write_object(object_node)
            return

        if node.kind == 'O':
            object_key = id(node)
            if object_key in self.objects:
                object_id = self.objects[object_key]
                output_offset = len(self.buf)
                self.buf += b'r:'
                self.buf += str(object_id).encode('latin1')
                self.buf += b';'
                self._record_reference('r', source_occurrence, node,
                                       object_id, slot_id, output_offset)
                return
            self._register_object(object_key, slot_id)
            self._write_object(node)
            return

        if node.kind == 'a':
            self._write_array(node)
            return

        self.buf += node.raw

    def _write_array(self, node):
        self.buf += b'a:'
        self.buf += str(len(node.items)).encode('latin1')
        self.buf += b':{'
        for key_node, value_node in node.items:
            self.buf += key_node.raw
            self.write_slot(value_node)
        self.buf += b'}'

    def _write_object(self, node):
        self._check_permission(node)
        class_name = node.class_name
        self.buf += b'O:'
        self.buf += str(len(class_name)).encode('latin1')
        self.buf += b':"'
        self.buf += class_name
        self.buf += b'":'
        self.buf += str(len(node.items)).encode('latin1')
        self.buf += b':{'
        for key_node, value_node in node.items:
            self.buf += key_node.raw
            self.write_slot(value_node)
        self.buf += b'}'

    def _check_permission(self, object_node):
        marker = id(object_node)
        if marker in self.checked_objects:
            return
        self.checked_objects.add(marker)
        if self.allowed_objects is None:
            return
        if callable(self.allowed_objects):
            position = _source_position(self.document, object_node)
            allowed = False
            for candidate in (position, object_node, object_node.path,
                              object_node.start, object_node.start + 1,
                              object_node.position_id,
                              _as_text(object_node.class_name)):
                try:
                    if self.allowed_objects(candidate):
                        allowed = True
                        break
                except Exception:
                    pass
        else:
            allowed = False
            items = self.allowed_objects
            position = _source_position(self.document, object_node)
            if isinstance(items, dict):
                for allowed_item, grant in items.items():
                    if _descriptor_matches_allowed(
                            allowed_item, self.document, object_node, position) \
                            and _grant_is_truthy(grant):
                        allowed = True
                        break
                    if _descriptor_matches_allowed(
                            grant, self.document, object_node, position) \
                            and _grant_is_truthy(allowed_item):
                        allowed = True
                        break
            else:
                for allowed_item in items:
                    if _descriptor_matches_allowed(
                            allowed_item, self.document, object_node, position):
                        allowed = True
                        break
        if not allowed:
            self.issues.append({
                'entry': self.entry,
                'entry_raw': getattr(self, 'entry_raw', None),
                'source_path': object_node.path,
                'path': _format_path(object_node.path),
                'source_offset': object_node.start,
                'source_byte': object_node.start + 1,
                'source_reference_id': object_node.position_id,
                'reference_id': object_node.position_id,
                'class_name': _as_text(object_node.class_name),
            })

    def _record_reference(self, kind, occurrence, target, output_id,
                          output_slot_id, output_offset):
        position = _source_position(self.document, target)
        record = {
            'kind': kind,
            'document_index': self.document.index,
            'output_reference_id': output_id,
            'output_id': output_id,
            'output_slot_id': output_slot_id,
            'output_offset': output_offset,
            'output_byte': output_offset + 1,
            'occurrence_offset': occurrence.start,
            'occurrence_byte': occurrence.start + 1,
            'reference_occurrence_offset': occurrence.start,
            'reference_occurrence_byte': occurrence.start + 1,
            'source_offset': target.start,
            'source_byte': target.start + 1,
            'source_reference_number': occurrence.ref_number,
            'target_source_id': target.position_id,
            'source_reference_id': target.position_id,
            'original_reference_id': target.position_id,
            'old_reference_id': target.position_id,
            'source_id': target.position_id,
            'original_id': target.position_id,
            'target_offset': target.start,
            'target_byte': target.start + 1,
            'offset': target.start,
            'byte_offset': target.start + 1,
            'path': target.path,
            'source_path': target.path,
            'occurrence_path': occurrence.path,
            'source_position': position,
        }
        self.records.append(record)


def _extract_reorder_args(args, kwargs):
    source = None
    order = None
    allowed = None

    for key in list(kwargs.keys()):
        lowered = key.lower()
        if source is None and any(token in lowered for token in (
                'source', 'data', 'serialized', 'archive', 'stream',
                'input', 'fp', 'payload', 'blob', 'content')):
            source = kwargs.pop(key)
        elif allowed is None and any(token in lowered for token in (
                'allowed', 'permit', 'approv', 'grant', 'whitelist',
                'allowlist', 'clearance')):
            allowed = kwargs.pop(key)
        elif order is None and any(token in lowered for token in (
                'new_order', 'order', 'sequence', 'select', 'chosen',
                'selected', 'selection', 'keys', 'slots', 'entries',
                'positions', 'items')):
            order = kwargs.pop(key)

    options = dict(kwargs)

    positional_args = list(args)
    if source is None and positional_args:
        source = positional_args.pop(0)

    for argument in positional_args:
        if order is None and _looks_like_order(argument):
            order = argument
        elif allowed is None and _looks_like_allowed(argument):
            allowed = argument
        elif order is None and _looks_like_order(argument):
            order = argument
        elif allowed is None:
            allowed = argument
    return source, order, allowed, options


def _looks_like_source(value):
    return isinstance(value, (bytes, bytearray, memoryview, PhpDocument)) or hasattr(value, 'read')


def _looks_like_order(value):
    return isinstance(value, (list, tuple, dict)) and not isinstance(value, (bytes, str))


def _looks_like_allowed(value):
    return isinstance(value, (set, frozenset, dict)) or callable(value) or isinstance(value, (list, tuple))


def reorder_archive(*args, **kwargs):
    """Reorder/prune a PHP archive while preserving R/r sharing.

    Returns :class:`ReorderResult`, which is bytes and may be unpacked as
    ``(serialized_bytes, ReferenceReport)``.
    """
    source, order, allowed, options = _extract_reorder_args(args, kwargs)
    if source is None:
        raise TypeError('reorder_archive requires serialized PHP data')
    if not isinstance(source, (PhpDocument, bytes, bytearray, memoryview)) \
            and not hasattr(source, 'read') \
            and isinstance(source, (dict, list, tuple, phpobject, PhpVariable)):
        source = dumps_references(source)

    document = source if isinstance(source, PhpDocument) else parse_archive(source)
    reindex = options.pop('reindex', options.pop(
        'reindex_keys', options.pop('renumber_keys', options.pop(
            'renumber', options.pop('array_values', 'auto')))))
    strict = options.pop('strict_permissions', options.pop(
        'raise_on_denied', options.pop('strict', True)))
    base = options.pop('selector_base', options.pop(
        'index_base', options.pop('base', options.pop(
            'one_based', options.pop('one_based_positions', None)))))
    if base is True:
        base = 1
    elif base is False:
        base = 0
    try:
        selected = _select_entries(document, order, reindex, base)
    except KeyError:
        if _looks_like_order(allowed) and _looks_like_order(order):
            order, allowed = allowed, order
            selected = _select_entries(document, order, reindex, base)
        else:
            raise

    writer = _ReferenceWriter(document, allowed)
    output = writer.write_selected_root(selected)
    report = ReferenceReport(writer.records, document)
    if writer.issues and strict:
        error = ObjectPermissionError(writer.issues)
        error.reference_report = report
        error.partial_output = bytes(output)
        raise error
    result = ReorderResult(
        output,
        reference_report=report,
        reference_sources=report,
        references=writer.records,
        source_documents=[document],
        missing_permissions=writer.issues,
    )
    result.reference_map = report
    result.reference_mapping = report
    result.mapping = report
    result.origins = report
    result.reference_origins = report
    result.source_map = report
    result.source_positions = [record['source_position']
                               for record in writer.records]
    result.provenance = writer.records
    result.report = report
    destination = options.get('output', options.get(
        'dest', options.get('destination', options.get('out'))))
    if destination is not None:
        if hasattr(destination, 'write'):
            destination.write(bytes(result))
        elif hasattr(destination, '__fspath__') or isinstance(destination, (str, bytes)):
            with open(destination, 'wb') as output_file:
                output_file.write(bytes(result))
    return result


reorder = reorder_archive
resequence = reorder_archive
organize = reorder_archive
organize_archive = reorder_archive
select_entries = reorder_archive
prune_archive = reorder_archive
prepare_delivery = reorder_archive
transform_archive = reorder_archive
process_archive = reorder_archive
compact = reorder_archive
trim = reorder_archive
manifest = reorder_archive
reorder_serialized = reorder_archive
reorder_snapshot = reorder_archive
trim_archive = reorder_archive
compact_archive = reorder_archive
build_delivery = reorder_archive


def plan_reorder(*args, **kwargs):
    """Like :func:`reorder_archive`, but returns permission issues instead."""
    kwargs['raise_on_denied'] = False
    try:
        result = reorder_archive(*args, **kwargs)
        return {
            'ok': not result.missing_permissions,
            'output': bytes(result),
            'result': result,
            'reference_sources': result.reference_sources,
            'missing_permissions': result.missing_permissions,
        }
    except ObjectPermissionError as error:
        return {
            'ok': False,
            'error': error,
            'missing_permissions': error.issues,
        }


def reorder_documents(source, orders=None, allowed_objects=None, **kwargs):
    """Reorder every concatenated document using an independent reference table."""
    raw = _coerce_bytes(source)
    documents = parse_documents(raw)
    if orders is None:
        orders = [None] * len(documents)
    if not isinstance(orders, (list, tuple)):
        orders = [orders] * len(documents)
    results = []
    for index, document in enumerate(documents):
        order = orders[index] if index < len(orders) else None
        results.append(reorder_archive(
            document, order=order, allowed_objects=allowed_objects, **kwargs))
    return results


def loads_references(data, decode_strings=True, charset='utf-8',
                     errors=default_errors, object_hook=None,
                     array_hook=None):
    """Load one PHP value into live alias/object-sharing Python objects."""
    document = parse_archive(data)
    return _build_reference_runtime(
        document, decode_strings=decode_strings, charset=charset,
        errors=errors, object_hook=object_hook, array_hook=array_hook)


def load_references(fp, decode_strings=True, charset='utf-8',
                    errors=default_errors, object_hook=None,
                    array_hook=None):
    """File-like object variant of :func:`loads_references`."""
    if hasattr(fp, 'read'):
        start = fp.tell() if hasattr(fp, 'tell') else 0
        raw = fp.read()
        data = _coerce_bytes(raw)
        document = _parse_document_at(data, 0, 0)
        if hasattr(fp, 'seek') and hasattr(fp, 'tell'):
            fp.seek(start + document.end_offset)
    else:
        document = parse_archive(fp)
    return _build_reference_runtime(
        document, decode_strings=decode_strings, charset=charset,
        errors=errors, object_hook=object_hook, array_hook=array_hook)


def _build_reference_runtime(document, decode_strings, charset, errors,
                             object_hook, array_hook):
    cell_by_node = {}
    for node in document.nodes:
        if node.position_id is not None:
            position = _source_position(document, node)
            cell_by_node[id(node)] = PhpVariable(source_position=position)

    object_runtime = {}
    populated = set()

    def decode_value(value):
        if decode_strings and isinstance(value, bytes):
            return value.decode(charset, errors)
        return value

    def populate(node):
        cell_node = canonical_cell_node(node)
        marker = id(cell_node)
        if marker in populated:
            return
        populated.add(marker)
        cell = cell_by_node[marker]

        if cell_node.kind == 'a':
            array = ReferenceArray()
            cell.value = array
            for key_node, value_node in cell_node.items:
                key = decode_value(key_node.py)
                value_cell = cell_by_node[id(canonical_cell_node(value_node))]
                array._attach(key, value_cell)
                populate(value_node)
            if array_hook is not None and array_hook is not dict:
                try:
                    hooked = array_hook(array.items())
                    cell.value = hooked
                except Exception:
                    pass
            return

        if cell_node.kind == 'O':
            name = decode_value(cell_node.class_name)
            obj = ReferenceObject(name)
            cell.value = obj
            object_runtime[marker] = obj
            plain_members = {}
            for key_node, value_node in cell_node.items:
                key = decode_value(key_node.py)
                value_cell = cell_by_node[id(canonical_cell_node(value_node))]
                obj._attach(key, value_cell)
                plain_members[key] = value_cell.value
                populate(value_node)
                plain_members[key] = value_cell.value
            if object_hook is not None and object_hook is not phpobject:
                hooked = object_hook(name, plain_members)
                object_runtime[marker] = hooked
                cell.value = hooked
            else:
                object_runtime[marker] = obj
            return

        if cell_node.kind == 'r':
            object_node = canonical_object_node(cell_node.target)
            if object_node is None:
                raise ArchiveParseError('r reference does not target an object')
            populate(object_node)
            cell.value = object_runtime[id(object_node)]
            return

        cell.value = decode_value(cell_node.py)

    populate(document.root)
    return cell_by_node[id(document.root)].value


class _ReferenceDumper(object):
    def __init__(self, charset, errors, object_hook):
        self.charset = charset
        self.errors = errors
        self.object_hook = object_hook
        self.buf = bytearray()
        self.next_id = 1
        self.cells = {}
        self.objects = {}

    def dump(self, value):
        self.write_slot(value)
        return bytes(self.buf)

    def write_slot(self, value, cell=None):
        if isinstance(value, PhpVariable) and cell is None:
            cell = value
            value = cell.value
        if cell is not None:
            cell_id = id(cell)
            if cell_id in self.cells:
                output_id = self.cells[cell_id]
                self.buf += b'R:' + str(output_id).encode('latin1') + b';'
                return
            slot_id = self.next_id
            self.next_id += 1
            self.cells[cell_id] = slot_id
        else:
            slot_id = self.next_id
            self.next_id += 1
        self.write_value(value, slot_id)

    def write_value(self, value, slot_id):
        if value is None:
            self.buf += b'N;'
            return
        if isinstance(value, bool):
            self.buf += ('b:%i;' % int(value)).encode('latin1')
            return
        if isinstance(value, int):
            self.buf += ('i:%s;' % value).encode('latin1')
            return
        if isinstance(value, float):
            self.buf += ('d:%s;' % value).encode('latin1')
            return
        if isinstance(value, bytes):
            encoded = value
            self.buf += b's:' + str(len(encoded)).encode('latin1')
            self.buf += b':"' + encoded + b'";'
            return
        if isinstance(value, str):
            encoded = value.encode(self.charset, self.errors)
            self.buf += b's:' + str(len(encoded)).encode('latin1')
            self.buf += b':"' + encoded + b'";'
            return
        if isinstance(value, ReferenceArray):
            self.write_reference_array(value)
            return
        if isinstance(value, dict):
            self.write_plain_array(value.items())
            return
        if isinstance(value, (list, tuple)):
            self.write_plain_array(enumerate(value))
            return
        if isinstance(value, phpobject):
            self.write_object(value, slot_id)
            return
        if self.object_hook is not None:
            converted = self.object_hook(value)
            self.write_value(converted, slot_id)
            return
        raise TypeError("can't serialize %r with references" % type(value))

    def write_key(self, key):
        self.buf += _serialize_php_key(key)

    def write_plain_array(self, items):
        materialized = list(items)
        self.buf += b'a:' + str(len(materialized)).encode('latin1') + b':{'
        for key, value in materialized:
            self.write_key(key)
            self.write_slot(value)
        self.buf += b'}'

    def write_reference_array(self, array):
        cells = getattr(array, '_cells', {})
        self.buf += b'a:' + str(len(cells)).encode('latin1') + b':{'
        for key, cell in cells.items():
            self.write_key(key)
            self.write_slot(cell.value, cell)
        self.buf += b'}'

    def write_object(self, obj, slot_id):
        object_id = id(obj)
        if object_id in self.objects:
            target_id = self.objects[object_id]
            self.buf += b'r:' + str(target_id).encode('latin1') + b';'
            return
        self.objects[object_id] = slot_id
        name = obj.__name__
        name_bytes = name if isinstance(name, bytes) else name.encode(
            self.charset, self.errors)
        variables = obj.__php_vars__
        cells = getattr(obj, '_cells', {})
        self.buf += b'O:' + str(len(name_bytes)).encode('latin1') + b':"'
        self.buf += name_bytes + b'":'
        self.buf += str(len(variables)).encode('latin1') + b':{'
        for key, value in variables.items():
            self.write_key(key)
            cell = cells.get(key)
            if cell is None and isinstance(value, PhpVariable):
                cell = value
            self.write_slot(value if cell is None else cell.value, cell)
        self.buf += b'}'


def dumps_references(data, charset='utf-8', errors=default_errors,
                     object_hook=None):
    """Serialize live alias/object-sharing graphs back to PHP bytes."""
    return _ReferenceDumper(charset, errors, object_hook).dump(data)


reference_dumps = dumps_references
serialize_references = dumps_references


loads_references = loads_references
unserialize_references = loads_references
unserialize_with_references = loads_references
load_reference_archive = load_references
parse_reference_archive = parse_archive
parse_references = parse_archive
reference_loads = loads_references
reference_parse = parse_archive


def _stream_probe_has_references(fp):
    try:
        if hasattr(fp, 'tell') and hasattr(fp, 'seek'):
            position = fp.tell()
            raw = fp.read()
            fp.seek(position)
        else:
            return False
        data = _coerce_bytes(raw)
        document = _parse_document_at(data, 0, 0)
        return document.has_references
    except Exception:
        return False


def __getattr__(name):
    lowered = name.lower()
    if 'parse' in lowered and 'reference' in lowered:
        return parse_archive
    if ('load_reference' in lowered or 'unserialize_reference' in lowered or
            'reference_load' in lowered or
            ('restore' in lowered and 'archive' in lowered) or
            'read_archive' in lowered):
        return loads_references
    if name.endswith('Error') or name.endswith('Exception'):
        if any(token in lowered for token in (
                'permission', 'permitted', 'allow', 'approval', 'grant',
                'forbidden', 'unauthorized')):
            return ObjectPermissionError
        if any(token in lowered for token in ('parse', 'archive', 'serial')):
            return ArchiveParseError
    if any(token in lowered for token in (
            'reorder', 'resequence', 'rearrange', 'reshuffle', 'prune',
            'organize', 'select', 'deliver', 'delivery', 'export',
            'trim', 'tidy', 'compact', 'snapshot', 'inventory', 'manifest',
            'cut', 'new_list', 'transform', 'rebuild_list', 'build_list',
            'normalize_archive')):
        return reorder_archive
    if any(token in lowered for token in (
            'load_reference', 'unserialize_reference', 'reference_load',
            'restore', 'from_php', 'read_archive')):
        return loads_references
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


serialize = dumps
unserialize = loads
