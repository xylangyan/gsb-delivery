# mako/cache.py
# Copyright 2006-2026 the Mako authors and contributors <see AUTHORS file>
#
# This module is part of Mako and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

from mako import groups
from mako import util

_cache_plugins = util.PluginLoader("mako.cache")

register_plugin = _cache_plugins.register
register_plugin("beaker", "mako.ext.beaker_cache", "BeakerCacheImpl")


class Cache:
    """Represents a data content cache made available to the module
    space of a specific :class:`.Template` object.

    .. versionadded:: 0.6
       :class:`.Cache` by itself is mostly a
       container for a :class:`.CacheImpl` object, which implements
       a fixed API to provide caching services; specific subclasses exist to
       implement different
       caching strategies.   Mako includes a backend that works with
       the Beaker caching system.   Beaker itself then supports
       a number of backends (i.e. file, memory, memcached, etc.)

    The construction of a :class:`.Cache` is part of the mechanics
    of a :class:`.Template`, and programmatic access to this
    cache is typically via the :attr:`.Template.cache` attribute.

    """

    impl = None
    """Provide the :class:`.CacheImpl` in use by this :class:`.Cache`.

    This accessor allows a :class:`.CacheImpl` with additional
    methods beyond that of :class:`.Cache` to be used programmatically.

    """

    id = None
    """Return the 'id' that identifies this cache.

    This is a value that should be globally unique to the
    :class:`.Template` associated with this cache, and can
    be used by a caching system to name a local container
    for data specific to this template.

    """

    starttime = None
    """Epochal time value for when the owning :class:`.Template` was
    first compiled.

    A cache implementation may wish to invalidate data earlier than
    this timestamp; this has the effect of the cache for a specific
    :class:`.Template` starting clean any time the :class:`.Template`
    is recompiled, such as when the original template file changed on
    the filesystem.

    """

    def __init__(self, template, *args):
        # check for a stale template calling the
        # constructor
        if isinstance(template, str) and args:
            return
        self.template = template
        self._id = template.module.__name__
        self.starttime = template.module._modified_time
        self._def_regions = {}
        self.impl = self._load_impl(self.template.cache_impl)

    @property
    def id(self):
        scope = groups.current_scope()
        if scope is None:
            return self._id
        return scope.cache_id(self._id)

    @id.setter
    def id(self, value):
        self._id = value

    def _load_impl(self, name):
        return _cache_plugins.load(name)(self)

    def _scope_from_context(self, context):
        scope = getattr(context, "_mako_scope", None)
        if scope is not None:
            return scope
        return groups.current_scope()

    def _extract_scope(self, kw):
        explicit = kw.pop("_mako_scope", None)
        if groups.is_scope(explicit):
            return explicit
        for key, value in list(kw.items()):
            if groups.is_scope(value):
                kw.pop(key)
                return value

        lookup = getattr(self.template, "lookup", None)
        resolver = getattr(lookup, "_mako_resolve_cache_scope", None)
        if resolver is not None:
            scope = resolver(kw)
            if scope is not None:
                return scope

        for name in groups.SCOPE_KWARG_NAMES:
            if name in kw:
                value = kw.pop(name)
                if groups.is_scope(value):
                    return value
                scope = groups.as_scope(value)
                if scope is not None and scope.directories:
                    return scope
        for name in groups.DIRECTORY_KWARG_NAMES:
            if name in kw:
                directories = kw.pop(name)
                return groups.TemplateDirectoryGroup(directories=directories)
        return groups.current_scope()

    def _scope_key(self, key, scope):
        if scope is None:
            return key
        return scope.cache_key(key)

    def _run_scoped(self, scope, fn):
        with groups.scope_context(scope):
            return fn()

    def get_or_create(self, key, creation_function, **kw):
        """Retrieve a value from the cache, using the given creation function
        to generate a new value."""

        scope = self._extract_scope(kw)
        return self._run_scoped(
            scope,
            lambda: self._ctx_get_or_create(
                key, creation_function, None, **kw
            ),
        )

    def _ctx_get_or_create(self, key, creation_function, context, **kw):
        """Retrieve a value from the cache, using the given creation function
        to generate a new value."""

        scope = self._scope_from_context(context)
        key = self._scope_key(key, scope)

        if not self.template.cache_enabled:
            return creation_function()

        def invoke():
            return self.impl.get_or_create(
                key,
                creation_function,
                **self._get_cache_kw(kw, context),
            )

        return self._run_scoped(scope, invoke)

    def set(self, key, value, **kw):
        r"""Place a value in the cache.

        :param key: the value's key.
        :param value: the value.
        :param \**kw: cache configuration arguments.

        """

        scope = self._extract_scope(kw)
        key = self._scope_key(key, scope)
        self._run_scoped(
            scope,
            lambda: self.impl.set(
                key, value, **self._get_cache_kw(kw, None)
            ),
        )

    put = set
    """A synonym for :meth:`.Cache.set`.

    This is here for backwards compatibility.

    """

    def get(self, key, **kw):
        r"""Retrieve a value from the cache.

        :param key: the value's key.
        :param \**kw: cache configuration arguments.  The
         backend is configured using these arguments upon first request.
         Subsequent requests that use the same series of configuration
         values will use that same backend.

        """
        scope = self._extract_scope(kw)
        key = self._scope_key(key, scope)
        return self._run_scoped(
            scope,
            lambda: self.impl.get(key, **self._get_cache_kw(kw, None)),
        )

    def invalidate(self, key, **kw):
        r"""Invalidate a value in the cache.

        :param key: the value's key.
        :param \**kw: cache configuration arguments.  The
         backend is configured using these arguments upon first request.
         Subsequent requests that use the same series of configuration
         values will use that same backend.

        """
        scope = self._extract_scope(kw)
        key = self._scope_key(key, scope)
        self._run_scoped(
            scope,
            lambda: self.impl.invalidate(
                key, **self._get_cache_kw(kw, None)
            ),
        )

    def invalidate_body(self):
        """Invalidate the cached content of the "body" method for this
        template.

        """
        self.invalidate("render_body", __M_defname="render_body")

    def invalidate_def(self, name):
        """Invalidate the cached content of a particular ``<%def>`` within this
        template.

        """

        self.invalidate("render_%s" % name, __M_defname="render_%s" % name)

    def invalidate_closure(self, name):
        """Invalidate a nested ``<%def>`` within this template.

        Caching of nested defs is a blunt tool as there is no
        management of scope -- nested defs that use cache tags
        need to have names unique of all other nested defs in the
        template, else their content will be overwritten by
        each other.

        """

        self.invalidate(name, __M_defname=name)

    def for_scope(self, scope):
        """Return a cache proxy bound to one template directory scope."""

        if not groups.is_scope(scope):
            lookup = getattr(self.template, "lookup", None)
            finder = getattr(lookup, "_find_scope", None)
            if finder is not None:
                found = finder(scope)
                if found is not None:
                    scope = found
            if not groups.is_scope(scope):
                scope = groups.coerce_scope(scope)
        return ScopedCache(self, scope)

    for_group = for_scope
    for_release = for_scope
    bind = for_scope

    def _get_cache_kw(self, kw, context):
        defname = kw.pop("__M_defname", None)
        if not defname:
            tmpl_kw = self.template.cache_args.copy()
            tmpl_kw.update(kw)
        elif defname in self._def_regions:
            tmpl_kw = self._def_regions[defname]
        else:
            tmpl_kw = self.template.cache_args.copy()
            tmpl_kw.update(kw)
            self._def_regions[defname] = tmpl_kw
        if context and self.impl.pass_context:
            tmpl_kw = tmpl_kw.copy()
            tmpl_kw.setdefault("context", context)
        return tmpl_kw


class ScopedCache:
    """Cache proxy that adds one fixed template directory scope."""

    def __init__(self, cache, scope):
        self._cache = cache
        self._scope = scope

    def __getattr__(self, name):
        attribute = getattr(self._cache, name)
        if not callable(attribute):
            return attribute

        def wrapper(*args, **kwargs):
            kwargs.setdefault("_mako_scope", self._scope)
            return attribute(*args, **kwargs)

        return wrapper


class CacheImpl:
    """Provide a cache implementation for use by :class:`.Cache`."""

    def __init__(self, cache):
        self.cache = cache

    pass_context = False
    """If ``True``, the :class:`.Context` will be passed to
    :meth:`get_or_create <.CacheImpl.get_or_create>` as the name ``'context'``.
    """

    def get_or_create(self, key, creation_function, **kw):
        r"""Retrieve a value from the cache, using the given creation function
        to generate a new value.

        This function *must* return a value, either from
        the cache, or via the given creation function.
        If the creation function is called, the newly
        created value should be populated into the cache
        under the given key before being returned.

        :param key: the value's key.
        :param creation_function: function that when called generates
         a new value.
        :param \**kw: cache configuration arguments.

        """
        raise NotImplementedError()

    def set(self, key, value, **kw):
        r"""Place a value in the cache.

        :param key: the value's key.
        :param value: the value.
        :param \**kw: cache configuration arguments.

        """
        raise NotImplementedError()

    def get(self, key, **kw):
        r"""Retrieve a value from the cache.

        :param key: the value's key.
        :param \**kw: cache configuration arguments.

        """
        raise NotImplementedError()

    def invalidate(self, key, **kw):
        r"""Invalidate a value in the cache.

        :param key: the value's key.
        :param \**kw: cache configuration arguments.

        """
        raise NotImplementedError()
