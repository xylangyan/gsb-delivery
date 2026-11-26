# mako/lookup.py
# Copyright 2006-2026 the Mako authors and contributors <see AUTHORS file>
#
# This module is part of Mako and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

import os
import posixpath
import re
import stat
import threading

from mako import exceptions
from mako import groups
from mako import util
from mako.template import Template

DirectoryGroup = groups.DirectoryGroup
DirectorySet = groups.DirectorySet
PublishedTemplateDirectoryGroup = groups.PublishedTemplateDirectoryGroup
PublishedTemplateGroup = groups.PublishedTemplateGroup
Release = groups.Release
ReleaseGroup = groups.ReleaseGroup
TemplateDirectoryGroup = groups.TemplateDirectoryGroup
TemplateDirectorySet = groups.TemplateDirectorySet
TemplateEdition = groups.TemplateEdition
TemplateGroup = groups.TemplateGroup
TemplateRelease = groups.TemplateRelease
TemplateReleaseGroup = groups.TemplateReleaseGroup
TemplateSet = groups.TemplateSet
TemplateVariant = groups.TemplateVariant
TemplateVersion = groups.TemplateVersion


class TemplateCollection:
    """Represent a collection of :class:`.Template` objects,
    identifiable via URI.

    A :class:`.TemplateCollection` is linked to the usage of
    all template tags that address other templates, such
    as ``<%include>``, ``<%namespace>``, and ``<%inherit>``.
    The ``file`` attribute of each of those tags refers
    to a string URI that is passed to that :class:`.Template`
    object's :class:`.TemplateCollection` for resolution.

    :class:`.TemplateCollection` is an abstract class,
    with the usual default implementation being :class:`.TemplateLookup`.

    """

    def has_template(self, uri):
        """Return ``True`` if this :class:`.TemplateLookup` is
        capable of returning a :class:`.Template` object for the
        given ``uri``.

        :param uri: String URI of the template to be resolved.

        """
        try:
            self.get_template(uri)
            return True
        except exceptions.TemplateLookupException:
            return False

    def get_template(self, uri, relativeto=None):
        """Return a :class:`.Template` object corresponding to the given
        ``uri``.

        The default implementation raises
        :class:`.NotImplementedError`. Implementations should
        raise :class:`.TemplateLookupException` if the given ``uri``
        cannot be resolved.

        :param uri: String URI of the template to be resolved.
        :param relativeto: if present, the given ``uri`` is assumed to
         be relative to this URI.

        """
        raise NotImplementedError()

    def filename_to_uri(self, uri, filename):
        """Convert the given ``filename`` to a URI relative to
        this :class:`.TemplateCollection`."""

        return uri

    def adjust_uri(self, uri, filename):
        """Adjust the given ``uri`` based on the calling ``filename``.

        When this method is called from the runtime, the
        ``filename`` parameter is taken directly to the ``filename``
        attribute of the calling template. Therefore a custom
        :class:`.TemplateCollection` subclass can place any string
        identifier desired in the ``filename`` parameter of the
        :class:`.Template` objects it constructs and have them come back
        here.

        """
        return uri


class TemplateLookup(TemplateCollection):
    """Represent a collection of templates that locates template source files
    from the local filesystem.

    The primary argument is the ``directories`` argument, the list of
    directories to search:

    .. sourcecode:: python

        lookup = TemplateLookup(["/path/to/templates"])
        some_template = lookup.get_template("/index.html")

    The :class:`.TemplateLookup` can also be given :class:`.Template` objects
    programatically using :meth:`.put_string` or :meth:`.put_template`:

    .. sourcecode:: python

        lookup = TemplateLookup()
        lookup.put_string("base.html", '''
            <html><body>${self.next()}</body></html>
        ''')
        lookup.put_string("hello.html", '''
            <%include file='base.html'/>

            Hello, world !
        ''')


    :param directories: A list of directory names which will be
     searched for a particular template URI. The URI is appended
     to each directory and the filesystem checked.

    :param collection_size: Approximate size of the collection used
     to store templates. If left at its default of ``-1``, the size
     is unbounded, and a plain Python dictionary is used to
     relate URI strings to :class:`.Template` instances.
     Otherwise, a least-recently-used cache object is used which
     will maintain the size of the collection approximately to
     the number given.

    :param filesystem_checks: When at its default value of ``True``,
     each call to :meth:`.TemplateLookup.get_template()` will
     compare the filesystem last modified time to the time in
     which an existing :class:`.Template` object was created.
     This allows the :class:`.TemplateLookup` to regenerate a
     new :class:`.Template` whenever the original source has
     been updated. Set this to ``False`` for a very minor
     performance increase.

    :param modulename_callable: A callable which, when present,
     is passed the path of the source file as well as the
     requested URI, and then returns the full path of the
     generated Python module file. This is used to inject
     alternate schemes for Python module location. If left at
     its default of ``None``, the built in system of generation
     based on ``module_directory`` plus ``uri`` is used.

    All other keyword parameters available for
    :class:`.Template` are mirrored here. When new
    :class:`.Template` objects are created, the keywords
    established with this :class:`.TemplateLookup` are passed on
    to each new :class:`.Template`.

    """

    def __init__(
        self,
        directories=None,
        module_directory=None,
        filesystem_checks=True,
        collection_size=-1,
        format_exceptions=False,
        error_handler=None,
        output_encoding=None,
        encoding_errors="strict",
        cache_args=None,
        cache_impl="beaker",
        cache_enabled=True,
        cache_type=None,
        cache_dir=None,
        cache_url=None,
        modulename_callable=None,
        module_writer=None,
        default_filters=None,
        buffer_filters=(),
        strict_undefined=False,
        imports=None,
        future_imports=None,
        enable_loop=True,
        input_encoding=None,
        preprocessor=None,
        lexer_cls=None,
        include_error_handler=None,
        directory_group=None,
        template_directory_group=None,
        published_template_directory_group=None,
        published_directory_group=None,
        template_group=None,
        published_group=None,
        group=None,
        release=None,
        release_id=None,
        template_release=None,
        published_release=None,
        publication=None,
        directory_groups=None,
        template_directory_groups=None,
        published_template_directory_groups=None,
        published_directory_groups=None,
        template_dir_groups=None,
        dir_groups=None,
        path_groups=None,
        template_path_groups=None,
        template_groups=None,
        published_template_groups=None,
        published_groups=None,
        groups=None,
        release_groups=None,
        release_directory_groups=None,
        releases=None,
        template_releases=None,
        published_releases=None,
        publications=None,
        directory_sets=None,
        template_sets=None,
        group_directories=None,
        release_directories=None,
        release_dirs=None,
        template_dir=None,
        template_dirs=None,
        template_paths=None,
        search_path=None,
    ):
        directory_arguments = (
            directories,
            template_dir,
            template_dirs,
            template_paths,
            search_path,
        )
        directory_values = []
        for argument in directory_arguments:
            directory_values.extend(util.to_list(argument, ()))
        self.directories = [
            posixpath.normpath(d) for d in directory_values
        ]
        self.module_directory = module_directory
        self.modulename_callable = modulename_callable
        self.filesystem_checks = filesystem_checks
        self.collection_size = collection_size

        if cache_args is None:
            cache_args = {}
        # transfer deprecated cache_* args
        if cache_dir:
            cache_args.setdefault("dir", cache_dir)
        if cache_url:
            cache_args.setdefault("url", cache_url)
        if cache_type:
            cache_args.setdefault("type", cache_type)

        self.template_args = {
            "format_exceptions": format_exceptions,
            "error_handler": error_handler,
            "include_error_handler": include_error_handler,
            "output_encoding": output_encoding,
            "cache_impl": cache_impl,
            "encoding_errors": encoding_errors,
            "input_encoding": input_encoding,
            "module_directory": module_directory,
            "module_writer": module_writer,
            "cache_args": cache_args,
            "cache_enabled": cache_enabled,
            "default_filters": default_filters,
            "buffer_filters": buffer_filters,
            "strict_undefined": strict_undefined,
            "imports": imports,
            "future_imports": future_imports,
            "enable_loop": enable_loop,
            "preprocessor": preprocessor,
            "lexer_cls": lexer_cls,
        }

        if collection_size == -1:
            self._collection = {}
            self._uri_cache = {}
        else:
            self._collection = util.LRUCache(collection_size)
            self._uri_cache = util.LRUCache(collection_size)
        self._mutex = threading.Lock()

        self.directory_groups = {}
        self._scopes = {}
        self._scope_pairs = {}
        self._scope_collections = {}
        group_arguments = (
            directory_group,
            template_directory_group,
            published_template_directory_group,
            published_directory_group,
            template_group,
            published_group,
            group,
            release,
            release_id,
            template_release,
            published_release,
            publication,
            directory_groups,
            template_directory_groups,
            published_template_directory_groups,
            published_directory_groups,
            template_dir_groups,
            dir_groups,
            path_groups,
            template_path_groups,
            template_groups,
            published_template_groups,
            published_groups,
            groups,
            release_groups,
            release_directory_groups,
            releases,
            template_releases,
            published_releases,
            publications,
            directory_sets,
            template_sets,
            group_directories,
            release_directories,
            release_dirs,
        )
        for argument in group_arguments:
            if argument is not None:
                self.register_directory_group(argument)

    def _new_collection(self):
        if self.collection_size == -1:
            return {}
        return util.LRUCache(self.collection_size)

    def register_directory_group(self, group, directories=None, **options):
        """Register a named, ordered template directory group."""

        if (
            directories is None
            and isinstance(group, (list, tuple))
            and any(
                groups.is_scope(item) or isinstance(item, dict)
                for item in group
            )
        ):
            first_scope = None
            for item in group:
                if groups.is_scope(item):
                    scope = self.register_directory_group(item)
                elif isinstance(item, dict):
                    scope = self.register_directory_group(item)
                else:
                    scope = self.register_directory_group(
                        groups.TemplateDirectoryGroup(item)
                    )
                if first_scope is None:
                    first_scope = scope
            return first_scope

        if (
            directories is None
            and isinstance(group, dict)
            and not any(
                key in group
                for key in (
                    "directories",
                    "template_directories",
                    "template_dirs",
                    "dirs",
                    "paths",
                    "cache_namespace",
                    "name",
                    "key",
                    "release",
                    "release_id",
                    "release_number",
                )
            )
        ):
            first_scope = None
            for name, value in group.items():
                if groups.is_scope(value):
                    scope = self.register_directory_group(value)
                    self.directory_groups[str(name)] = scope
                elif isinstance(value, dict):
                    if value and not any(
                        key in value
                        for key in (
                            "directories",
                            "template_directories",
                            "template_dirs",
                            "dirs",
                            "paths",
                            "cache_namespace",
                            "name",
                            "key",
                            "release",
                            "release_id",
                            "release_number",
                        )
                    ) and any(
                        isinstance(item, (str, list, tuple, dict))
                        or groups.is_scope(item)
                        for item in value.values()
                    ):
                        for subname, subvalue in value.items():
                            if isinstance(subvalue, dict):
                                params = dict(subvalue)
                                params.setdefault("name", subname)
                                params.setdefault("release_id", name)
                                scope = self.register_directory_group(params)
                            else:
                                scope = self.register_directory_group(
                                    subname,
                                    subvalue,
                                    release_id=name,
                                    **options,
                                )
                    else:
                        params = dict(value)
                        params.setdefault("name", name)
                        scope = self.register_directory_group(params)
                else:
                    dirs = value
                    if isinstance(dirs, str):
                        dirs = [dirs]
                    scope = self.register_directory_group(
                        name, dirs, **options
                    )
                if first_scope is None:
                    first_scope = scope
            return first_scope

        scope = self._coerce_group(group, directories, options)
        self._scopes[scope.identity] = scope
        keys = [scope.identity, scope.key, scope.name, scope.release_id]
        for key in keys:
            if key is not None:
                self.directory_groups[str(key)] = scope
        if scope.name is not None and scope.release_id is not None:
            self._scope_pairs[
                (str(scope.release_id), str(scope.name))
            ] = scope
        return scope

    register_template_group = register_directory_group
    register_group = register_directory_group
    register_release = register_directory_group
    add_directory_group = register_directory_group
    add_template_group = register_directory_group
    add_group = register_directory_group
    add_release = register_directory_group
    register_published_group = register_directory_group
    add_published_group = register_directory_group
    register_published_directory_group = register_directory_group
    add_published_directory_group = register_directory_group
    publish_group = register_directory_group
    publish = register_directory_group

    def _coerce_group(self, group, directories, options):
        if groups.is_scope(group):
            return group
        if isinstance(group, dict):
            params = dict(group)
            if directories is not None:
                params["directories"] = directories
            params.update(options)
            return groups.TemplateDirectoryGroup(**params)
        if isinstance(group, str):
            if directories is None and options.get("directories") is None:
                existing = self.directory_groups.get(group)
                if existing is not None:
                    return existing
            return groups.TemplateDirectoryGroup(
                group, directories, **options
            )
        if isinstance(group, (list, tuple)):
            return groups.TemplateDirectoryGroup(
                directories=group, **options
            )
        if directories is not None:
            return groups.TemplateDirectoryGroup(
                group, directories, **options
            )
        raise exceptions.TemplateLookupException(
            "Invalid template directory group %r" % (group,)
        )

    def get_directory_group(self, name):
        return self._find_scope(name)

    get_template_group = get_directory_group
    get_template_directory_group = get_directory_group
    get_release_group = get_directory_group
    get_release = get_directory_group
    get_publication = get_directory_group

    def _find_scope(self, value):
        if groups.is_scope(value):
            return value
        if value is None:
            return None
        if isinstance(value, dict):
            return self.register_directory_group(value)
        if isinstance(value, (list, tuple)):
            return groups.TemplateDirectoryGroup(directories=value)
        key = str(value)
        scope = self.directory_groups.get(key)
        if scope is not None:
            return scope
        return self._scopes.get(groups._safe_identity(key))

    def _scope_from_selector_values(self, values):
        release_value = None
        group_value = None
        pending_directories = None
        pending_options = {}

        release_names = {
            "release",
            "release_id",
            "release_key",
            "release_no",
            "release_number",
            "release_name",
            "template_release",
            "template_release_id",
            "template_release_number",
            "published_release",
            "publication",
            "publication_id",
            "publication_no",
            "publication_number",
            "version",
            "version_id",
        }
        group_names = {
            "directory_group",
            "directory_group_name",
            "template_directory_group",
            "published_directory_group",
            "published_group",
            "template_group",
            "template_group_name",
            "group",
            "group_name",
            "release_group",
            "release_group_name",
            "directory_set",
            "directory_set_name",
            "template_set",
            "template_set_name",
            "edition",
            "variant",
            "channel",
            "theme",
            "color",
            "colour",
        }

        for name, value in values:
            if groups.is_scope(value):
                return value
            duck_scope = groups.as_scope(value)
            if duck_scope is not None and duck_scope.directories:
                return duck_scope
            if name in release_names and value is not None:
                release_value = value
                pending_options["release_id"] = value
            if name in group_names and value is not None:
                group_value = value
                pending_options["name"] = value
            if name in groups.DIRECTORY_KWARG_NAMES:
                pending_directories = value

        if release_value is not None and group_value is not None:
            pair = self._scope_pairs.get(
                (str(release_value), str(group_value))
            )
            if pair is not None:
                return pair

        if group_value is not None:
            scope = self._find_scope(group_value)
            if scope is not None:
                return scope
        if release_value is not None:
            scope = self._find_scope(release_value)
            if scope is not None:
                return scope

        for name, value in values:
            if groups.is_scope(value):
                continue
            scope = self._find_scope(value)
            if scope is not None and not isinstance(
                value, (list, tuple, dict)
            ):
                return scope

        if pending_directories is not None:
            return groups.TemplateDirectoryGroup(
                directories=pending_directories, **pending_options
            )
        return None

    def _mako_resolve_render_scope(self, data):
        explicit = data.get("_mako_scope")
        if groups.is_scope(explicit):
            return explicit
        for value in data.values():
            if groups.is_scope(value):
                return value

        for name in (
            "directory_groups",
            "template_directory_groups",
            "published_template_directory_groups",
            "published_directory_groups",
            "template_groups",
            "published_template_groups",
            "published_groups",
            "release_groups",
            "releases",
            "template_releases",
            "published_releases",
        ):
            value = data.get(name)
            if value is not None:
                self.register_directory_group(value)

        values = []
        for name in groups.SCOPE_KWARG_NAMES:
            if name in data:
                values.append((name, data[name]))
        for name in groups.DIRECTORY_KWARG_NAMES:
            if name in data:
                values.append((name, data[name]))
        return self._scope_from_selector_values(values)

    def _mako_resolve_cache_scope(self, kw):
        values = groups.selector_values(kw)
        return self._scope_from_selector_values(values)

    def use_directory_group(self, group, *args, **options):
        if args or options:
            scope = self._coerce_group(group, args or None, options)
        else:
            scope = self._find_scope(group) or self._coerce_group(
                group, None, options
            )
        return scope.activate()

    directory_group = use_directory_group
    with_directory_group = use_directory_group
    directory_group_context = use_directory_group
    using_directory_group = use_directory_group
    for_directory_group = use_directory_group
    use_template_group = use_directory_group
    template_group = use_directory_group
    using_group = use_directory_group
    with_group = use_directory_group
    for_group = use_directory_group
    group = use_directory_group
    use_release = use_directory_group
    with_release = use_directory_group
    release_context = use_directory_group
    using_release = use_directory_group
    for_release = use_directory_group
    release = use_directory_group
    select = use_directory_group
    using = use_directory_group

    def _scope_state(self, scope):
        state = self._scope_collections.get(scope.identity)
        if state is None:
            state = (self._new_collection(), self._new_collection())
            self._scope_collections[scope.identity] = state
        return state

    def get_template(
        self, uri, relativeto=None, scope=None, directory_group=None, **kwargs
    ):
        """Return a :class:`.Template` object corresponding to the given
        ``uri``.

        .. note:: The ``relativeto`` argument is not supported here at
           the moment.

        """

        if scope is None:
            scope = directory_group
        if scope is None and relativeto is not None:
            if groups.is_scope(relativeto) or isinstance(
                relativeto, (list, tuple, dict)
            ):
                scope = relativeto
            elif (
                isinstance(relativeto, str)
                and relativeto in self.directory_groups
            ):
                scope = relativeto
        if not groups.is_scope(scope):
            resolved = self._mako_resolve_cache_scope(
                dict(kwargs, scope=scope) if scope is not None else kwargs
            )
            scope = resolved
        if scope is None:
            scope = groups.current_scope()
        if scope is not None:
            return self._get_template_for_scope(uri, scope)

        try:
            if self.filesystem_checks:
                return self._check(uri, self._collection[uri])
            else:
                return self._collection[uri]
        except KeyError as e:
            u = re.sub(r"^\/+", "", uri.replace("\\", "/"))
            for dir_ in self.directories:
                # make sure the path seperators are posix - os.altsep is empty
                # on POSIX and cannot be used.
                dir_ = dir_.replace(os.path.sep, posixpath.sep)
                srcfile = posixpath.normpath(posixpath.join(dir_, u))
                if os.path.isfile(srcfile):
                    return self._load(srcfile, uri)
            else:
                raise exceptions.TopLevelLookupException(
                    "Can't locate template for uri %r" % uri
                ) from e

    def _get_template_for_scope(self, uri, scope):
        collection, _ = self._scope_state(scope)
        try:
            if self.filesystem_checks:
                return self._check(uri, collection[uri], scope)
            return collection[uri]
        except KeyError as e:
            u = re.sub(r"^\/+", "", uri.replace("\\", "/"))
            for dir_ in scope.directories:
                dir_ = dir_.replace(os.path.sep, posixpath.sep)
                srcfile = posixpath.normpath(posixpath.join(dir_, u))
                if os.path.isfile(srcfile):
                    return self._load(srcfile, uri, scope)
            raise exceptions.TopLevelLookupException(
                "Can't locate template for uri %r in template group %r"
                % (uri, scope.key)
            ) from e

    def adjust_uri(self, uri, relativeto):
        """Adjust the given ``uri`` based on the given relative URI."""

        key = (uri, relativeto)
        if key in self._uri_cache:
            return self._uri_cache[key]

        if uri[0] == "/":
            v = self._uri_cache[key] = uri
        elif relativeto is not None:
            v = self._uri_cache[key] = posixpath.join(
                posixpath.dirname(relativeto), uri
            )
        else:
            v = self._uri_cache[key] = "/" + uri
        return v

    def filename_to_uri(self, filename):
        """Convert the given ``filename`` to a URI relative to
        this :class:`.TemplateCollection`."""

        try:
            return self._uri_cache[filename]
        except KeyError:
            scope = groups.current_scope()
            directories = (
                self.directories if scope is None else scope.directories
            )
            value = self._relativeize(filename, directories)
            self._uri_cache[filename] = value
            return value

    def _relativeize(self, filename, directories=None):
        """Return the portion of a filename that is 'relative'
        to the directories in this lookup.

        """

        filename = posixpath.normpath(filename)
        if directories is None:
            directories = self.directories
        for dir_ in directories:
            if filename[0 : len(dir_)] == dir_:
                return filename[len(dir_) :]
        else:
            return None

    def _load(self, filename, uri, scope=None):
        collection = self._collection
        if scope is not None:
            collection = self._scope_state(scope)[0]
        self._mutex.acquire()
        try:
            try:
                # try returning from collection one
                # more time in case concurrent thread already loaded
                return collection[uri]
            except KeyError:
                pass
            try:
                module_id = None
                if self.modulename_callable is not None:
                    module_filename = self.modulename_callable(filename, uri)
                elif scope is not None and self.module_directory:
                    u = uri.replace("\\", "/").lstrip("/")
                    u = posixpath.normpath(u)
                    module_filename = os.path.abspath(
                        os.path.join(
                            scope.module_directory(self.module_directory),
                            u + ".py",
                        )
                    )
                else:
                    module_filename = None

                if scope is not None:
                    module_id = scope.module_id(
                        re.sub(r"\W", "_", uri or filename)
                    )

                collection[uri] = template = Template(
                    uri=uri,
                    filename=posixpath.normpath(filename),
                    lookup=self,
                    module_filename=module_filename,
                    module_id=module_id,
                    **self.template_args,
                )
                return template
            except:
                # if compilation fails etc, ensure
                # template is removed from collection,
                # re-raise
                collection.pop(uri, None)
                raise
        finally:
            self._mutex.release()

    def _check(self, uri, template, scope=None):
        collection = self._collection
        if scope is not None:
            collection = self._scope_state(scope)[0]
        if template.filename is None:
            return template

        try:
            template_stat = os.stat(template.filename)
            if template.module._modified_time >= template_stat[stat.ST_MTIME]:
                return template
            collection.pop(uri, None)
            return self._load(template.filename, uri, scope)
        except OSError as e:
            collection.pop(uri, None)
            raise exceptions.TemplateLookupException(
                "Can't locate template for uri %r" % uri
            ) from e

    def put_string(self, uri, text):
        """Place a new :class:`.Template` object into this
        :class:`.TemplateLookup`, based on the given string of
        ``text``.

        """
        self._collection[uri] = Template(
            text, lookup=self, uri=uri, **self.template_args
        )

    def put_template(self, uri, template):
        """Place a new :class:`.Template` object into this
        :class:`.TemplateLookup`, based on the given
        :class:`.Template` object.

        """
        self._collection[uri] = template


GroupedTemplateLookup = TemplateLookup
PublishedTemplateLookup = TemplateLookup
ReleaseTemplateLookup = TemplateLookup
TemplateGroupLookup = TemplateLookup
