# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import io
import os
import six
import json
import hashlib
import threading
import warnings
import jsonschema
from jsonschema.validators import validator_for
from six.moves.urllib.parse import urldefrag, urljoin, urlparse, unquote
from six.moves.urllib.request import pathname2url, url2pathname
from . import exceptions

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    try:
        from jsonschema.validators import RefResolver
    except ImportError:
        from jsonschema import RefResolver


# Module API

class Profile(object):

    # Public

    def __init__(self, profile):
        self.__profile = profile
        self.__jsonschema = _PROFILES.get(profile)
        if not self.__jsonschema:
            message = 'Can\'t load profile "%s"' % profile
            raise exceptions.LoadError(message)

    @property
    def name(self):
        return self.__jsonschema.get('title', '').replace(' ', '-').lower() or None

    @property
    def jsonschema(self):
        return self.__jsonschema

    def validate(self, descriptor):

        # Other profiles
        if self.name != 'table-schema':
            return jsonschema.validate(descriptor, self.jsonschema)

        # Collect errors
        errors = []
        validator = _TableSchemaValidator(
            self.jsonschema, format_checker=jsonschema.FormatChecker())
        for error in validator.iter_errors(descriptor):
            if isinstance(error, jsonschema.exceptions.ValidationError):
                message = str(error.message)
                if six.PY2:
                    message = message.replace('u\'', '\'')
                descriptor_path = '/'.join(map(str, error.path))
                profile_path = '/'.join(map(str, error.schema_path))
                error = exceptions.ValidationError(
                    'Descriptor validation error: %s '
                    'at "%s" in descriptor and '
                    'at "%s" in profile'
                    % (message, descriptor_path, profile_path))
            errors.append(error)

        # Railse error
        if errors:
            message = 'There are %s validation errors (see exception.errors)' % len(errors)
            raise exceptions.ValidationError(message, errors=errors)

        return True


class LocalProfile(object):
    """JSON Schema profile loaded from a local rule package.

    A rule package is a local directory containing a root JSON Schema
    document and the files it references through relative `$ref` pointers.

    Prepared rules are cached by the fingerprint of the whole package
    contents (the root document and every transitively referenced file),
    so identical packages loaded from different directories are prepared
    only once, while packages differing in any referenced file are
    prepared independently. The number of actual rule preparations is
    available as the `LocalProfile.preparations` class attribute.

    Publishing is atomic: a new version becomes active only after the
    whole package has been loaded, checked and prepared. If publishing
    fails (invalid references, incomplete package, invalid rules) the
    previously published version stays active. A validation call always
    uses the single version that was active when the call started, even
    if another version is published concurrently.

    # Arguments
        root_path (str): path to the root document of the rule package

    # Raises
        LoadError: if the package can't be loaded, checked or prepared

    """

    # Shared prepared-rules cache: fingerprint -> LocalProfileVersion
    _cache = {}
    _cache_lock = threading.Lock()

    # Number of actual rule preparations (cache misses)
    preparations = 0

    # Public

    def __init__(self, root_path):
        self.__root_path = None
        self.__current = None
        self.publish(root_path)

    @property
    def version(self):
        """Identifier of the currently published rule version.

        # Returns
            str: version fingerprint

        """
        return self.__current.version

    @property
    def current(self):
        """Currently published rule version.

        The returned object stays fully usable after newer versions are
        published, so a validation started before a publish keeps using
        the rules that were active when it started.

        # Returns
            LocalProfileVersion: current rule version

        """
        return self.__current

    def publish(self, root_path=None):
        """Load, check and publish a rule package version.

        The new version becomes active only if the whole package is
        valid; otherwise the previously published version stays active.

        # Arguments
            root_path (str): package root document path
                (defaults to the path of the previous publish)

        # Raises
            LoadError: if the package can't be loaded, checked or prepared

        # Returns
            str: version fingerprint of the published rules

        """
        path = os.path.abspath(root_path) if root_path else self.__root_path
        package = _load_local_package(path)
        with LocalProfile._cache_lock:
            prepared = LocalProfile._cache.get(package.fingerprint)
            if prepared is None:
                prepared = LocalProfileVersion(package)
                LocalProfile._cache[package.fingerprint] = prepared
                LocalProfile.preparations += 1
        self.__root_path = path
        self.__current = prepared
        return prepared.version

    def validate(self, descriptor, strict=False):
        """Validate a descriptor against the currently published rules.

        # Arguments
            descriptor (dict): descriptor to validate
            strict (bool): if true, collected errors are raised
                as a ValidationError with `exception.errors` set

        # Raises
            ValidationError: if strict and there are validation errors

        # Returns
            Exception[]: collected validation errors (empty if valid)

        """
        return self.__current.validate(descriptor, strict=strict)


class LocalProfileVersion(object):
    """Immutable prepared rules of one published local profile version.

    Instances are created by `LocalProfile` and shared through its cache;
    they can be obtained via `profile.current` to keep validating with
    exactly this version after newer versions are published.

    """

    # Public

    def __init__(self, package):
        self.__version = package.fingerprint[:12]
        self.__jsonschema = package.root_document
        store = {}
        for relpath in package.documents:
            path, _, document = package.documents[relpath]
            store[_file_uri(path)] = document
        resolver = RefResolver(
            base_uri=_file_uri(package.root_path),
            referrer=package.root_document,
            store=store)
        _check_package_references(package, resolver)
        validator_class = validator_for(package.root_document)
        self.__validator = validator_class(
            package.root_document, resolver=resolver)
        self.__sources = {}
        for relpath in package.documents:
            _, _, document = package.documents[relpath]
            _index_sources(self.__sources, document, relpath, [])
        self.__root_source = package.root_relpath

    @property
    def version(self):
        """Version fingerprint of the prepared rules.

        # Returns
            str: version fingerprint

        """
        return self.__version

    @property
    def jsonschema(self):
        """Root JSON Schema document of this version.

        # Returns
            dict: root JSON Schema document

        """
        return self.__jsonschema

    def validate(self, descriptor, strict=False):
        """Validate a descriptor against this exact rule version.

        # Arguments
            descriptor (dict): descriptor to validate
            strict (bool): if true, collected errors are raised
                as a ValidationError with `exception.errors` set

        # Raises
            ValidationError: if strict and there are validation errors

        # Returns
            Exception[]: collected validation errors (empty if valid)

        """
        errors = []
        for error in self.__validator.iter_errors(descriptor):
            if isinstance(error, jsonschema.exceptions.ValidationError):
                message = str(error.message)
                if six.PY2:
                    message = message.replace('u\'', '\'')
                descriptor_path = '/'.join(map(str, error.absolute_path))
                profile_path = '/'.join(map(str, error.absolute_schema_path))
                source = self.__sources.get(id(error.schema), self.__root_source)
                error = exceptions.ValidationError(
                    'Descriptor validation error: %s '
                    'at "%s" in descriptor and '
                    'at "%s" in profile '
                    '(profile version "%s", reference source "%s")'
                    % (message, descriptor_path, profile_path,
                       self.__version, source))
                error.rule_version = self.__version
                error.descriptor_path = descriptor_path
                error.reference_source = source
            errors.append(error)

        # Raise error
        if errors and strict:
            message = 'There are %s validation errors (see exception.errors)' % len(errors)
            raise exceptions.ValidationError(message, errors=errors)

        return errors


# Internal

class _LocalPackage(object):
    """Fully loaded contents of a local rule package."""

    def __init__(self, root_path, root_relpath, documents, fingerprint):
        self.root_path = root_path
        self.root_relpath = root_relpath
        self.documents = documents
        self.fingerprint = fingerprint

    @property
    def root_document(self):
        return self.documents[self.root_relpath][2]


def _load_local_package(root_path):
    if not os.path.isfile(root_path):
        message = 'Can\'t load local profile "%s"' % root_path
        raise exceptions.LoadError(message)
    root_dir = os.path.dirname(root_path)
    documents = {}
    queue = [root_path]
    while queue:
        path = os.path.normpath(queue.pop())
        relpath = os.path.relpath(path, root_dir).replace(os.sep, '/')
        if relpath in documents:
            continue
        if not os.path.isfile(path):
            message = ('Can\'t load local profile "%s": '
                       'referenced file "%s" is missing' % (root_path, relpath))
            raise exceptions.LoadError(message)
        try:
            raw = io.open(path, 'rb').read()
            document = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exception:
            message = ('Can\'t load local profile "%s": '
                       'file "%s" is not valid JSON (%s)'
                       % (root_path, relpath, exception))
            raise exceptions.LoadError(message)
        documents[relpath] = (path, raw, document)
        for ref in _iter_refs(document):
            ref_path = _resolve_ref_path(ref, os.path.dirname(path), root_path)
            if ref_path is not None:
                queue.append(ref_path)
    fingerprint = _fingerprint_package(documents)
    root_relpath = os.path.relpath(root_path, root_dir).replace(os.sep, '/')
    return _LocalPackage(root_path, root_relpath, documents, fingerprint)


def _iter_refs(node):
    if isinstance(node, dict):
        ref = node.get('$ref')
        if isinstance(ref, six.string_types):
            yield ref
        for value in node.values():
            for ref in _iter_refs(value):
                yield ref
    elif isinstance(node, list):
        for item in node:
            for ref in _iter_refs(item):
                yield ref


def _resolve_ref_path(ref, base_dir, root_path):
    url, _ = urldefrag(ref)
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme and parsed.scheme != 'file':
        message = ('Can\'t load local profile "%s": '
                   'remote reference "%s" is not supported'
                   % (root_path, ref))
        raise exceptions.LoadError(message)
    if parsed.scheme == 'file':
        return os.path.normpath(url2pathname(unquote(parsed.path)))
    return os.path.normpath(os.path.join(base_dir, unquote(url)))


def _fingerprint_package(documents):
    digest = hashlib.sha256()
    for relpath in sorted(documents):
        _, raw, _ = documents[relpath]
        digest.update(hashlib.sha256(relpath.encode('utf-8')).hexdigest().encode('utf-8'))
        digest.update(hashlib.sha256(raw).hexdigest().encode('utf-8'))
    return digest.hexdigest()


def _file_uri(path):
    return urljoin('file:', pathname2url(os.path.abspath(path)))


def _check_package_references(package, resolver):
    for relpath in package.documents:
        path, _, document = package.documents[relpath]
        try:
            validator_for(document).check_schema(document)
        except Exception as exception:
            message = ('Can\'t publish local profile "%s": '
                       'file "%s" contains invalid rules (%s)'
                       % (package.root_path, relpath, exception))
            raise exceptions.LoadError(message)
        for ref in _iter_refs(document):
            url = urljoin(_file_uri(path), ref)
            try:
                resolver.resolve_from_url(url)
            except Exception as exception:
                message = ('Can\'t publish local profile "%s": '
                           'can\'t resolve reference "%s" in "%s" (%s)'
                           % (package.root_path, ref, relpath, exception))
                raise exceptions.LoadError(message)


def _index_sources(sources, node, relpath, path):
    if path:
        sources[id(node)] = '%s#/%s' % (relpath, '/'.join(path))
    else:
        sources[id(node)] = '%s#' % relpath
    if isinstance(node, dict):
        for key, value in node.items():
            _index_sources(sources, value, relpath, path + [str(key)])
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _index_sources(sources, item, relpath, path + [str(index)])


def _load_profile(filename):
    path = os.path.join(os.path.dirname(__file__), 'profiles', filename)
    profile = json.load(io.open(path, encoding='utf-8'))
    return profile


_PROFILES = {
    'table-schema': _load_profile('table-schema.json'),
    'geojson': _load_profile('geojson.json'),
}

_CONSTRAINT_ALLOWED_FIELD_TYPE = {
    'minLength': {None, 'string', 'array', 'object'},
    'maxLength': {None, 'string', 'array', 'object'},
    'minimum': {'integer', 'number', 'date', 'time', 'datetime', 'year', 'yearmonth'},
    'maximum': {'integer', 'number', 'date', 'time', 'datetime', 'year', 'yearmonth'},
    'pattern': {None, 'string'},
}


class _TableSchemaValidator(validator_for(_PROFILES['table-schema'])):
    @classmethod
    def check_schema(cls, schema):
        # When checking against the metaschema, we do not want to run the
        # additional checking added in iter_errors
        parent_cls = cls.__bases__[0]
        for error in parent_cls(cls.META_SCHEMA).iter_errors(schema):
            raise jsonschema.exceptions.SchemaError.create_from(error)

    def iter_errors(self, instance, _schema=None):

        # iter jsonschema validation errors
        for error in super(_TableSchemaValidator, self).iter_errors(instance, _schema):
            yield jsonschema.exceptions.ValidationError(
                error.message, error.validator, error.path, error.cause,
                error.context, error.validator_value, error.instance,
                error.schema, error.schema_path, error.parent)

        # get field names
        try:
            field_names = [f['name'] for f in instance['fields']]
        except (TypeError, KeyError):
            field_names = []

        # ensure constraint and field type consistency
        if isinstance(instance, dict) and instance.get('fields'):
            for field in instance['fields']:
                if not isinstance(field, dict):
                    continue
                field_type = field.get('type')
                field_type_str = 'default' if field_type is None else field_type
                field_name = field.get('name', '[noname]')
                constraints = field.get('constraints', {})
                for constraint_name in constraints:
                    if constraint_name in _CONSTRAINT_ALLOWED_FIELD_TYPE:
                        if field_type not in _CONSTRAINT_ALLOWED_FIELD_TYPE[constraint_name]:
                            yield exceptions.ValidationError(
                                "field {}: built-in {} constraint can't be applied to {} type field"
                                .format(field_name, constraint_name, field_type_str))

        # the hash MAY contain a key `primaryKey`
        if isinstance(instance, dict) and instance.get('primaryKey'):

            # ensure that the primary key matches field names
            if isinstance(instance['primaryKey'], six.string_types):
                if not instance['primaryKey'] in field_names:
                    yield exceptions.ValidationError(
                        'A JSON Table Schema primaryKey value must be found in'
                        ' the schema field names')
            elif isinstance(instance['primaryKey'], list):
                for k in instance['primaryKey']:
                    if k not in field_names:
                        yield exceptions.ValidationError(
                            'A JSON Table Schema primaryKey value must be '
                            'found in the schema field names')

        # the hash may contain a key `foreignKeys`
        if isinstance(instance, dict) and instance.get('foreignKeys'):
            for fk in instance['foreignKeys']:

                # ensure that `foreignKey.fields` match field names
                if isinstance(fk.get('fields'), six.string_types):
                    if fk.get('fields') not in field_names:
                        yield exceptions.ValidationError(
                            'A JSON Table Schema foreignKey.fields value must '
                            'correspond with field names.')
                elif isinstance(fk.get('fields', []), list):
                    for field in fk.get('fields'):
                        if field not in field_names:
                            yield exceptions.ValidationError(
                                'A JSON Table Schema foreignKey.fields value '
                                'must correspond with field names.')

                # ensure that `foreignKey.reference.fields`
                # matches outer `fields`
                if isinstance(fk.get('fields'), six.string_types):
                    fields = fk.get('reference', {}).get('fields', {})
                    if not isinstance(fields, six.string_types):
                        yield exceptions.ValidationError(
                            'A JSON Table Schema foreignKey.reference.fields '
                            'must match field names.')
                else:
                    if isinstance(fk['reference']['fields'], six.string_types):
                        yield exceptions.ValidationError(
                            'A JSON Table Schema foreignKey.fields cannot '
                            'be a string when foreignKey.reference.fields.'
                            'is a string')
                    if not (len(fk.get('fields')) ==
                            len(fk['reference']['fields'])):
                        yield exceptions.ValidationError(
                            'A JSON Table Schema foreignKey.fields must '
                            'contain the same number entries as '
                            'foreignKey.reference.fields.')
