#! /usr/bin/env python
"""A simple Entity store using a python dictionary"""

import hashlib
import threading
import logging

from . import csdl as edm
from . import core as odata
from .. import iso8601 as iso
from ..py2 import (
    dict_items,
    dict_keys,
    dict_values,
    range3)


class InMemoryEntityStore(object):

    """Implements an in-memory entity set using a python dictionary.

    Each entity is stored as a tuple of values in the order in which the
    properties of that entity type are declared.  Complex values are
    stored as nested tuples.

    Media streams are simply strings stored in a parallel dictionary
    mapping keys on to a tuple of media-type and string.

    All access to the data itself uses the *container*'s lock to ensure
    this object can be called from multi-threaded programs.  Although
    individual collections must not be shared across threads multiple
    threads can open separate collections and access the entities
    safely."""

    def __init__(self, container, entity_set=None):
        self.container = container
        """the :py:class:`InMemoryEntityContainer` that contains this
        entity set"""
        self.entity_set = entity_set    #: the entity set we're bound to
        self.data = {}                  #: simple dictionary of the values
        self.streams = {}               #: simple dictionary of streams
        self.associations = {}
        # a mapping of association set names to
        # :py:class:`InMemoryAssociation` instances *from* this entity
        # set
        self.reverseAssociations = {}
        # a mapping of association set names to
        # :py:class:`InMemoryAssociation` index instances *to* this
        # entity set
        self._deleting = set()
        if entity_set is not None:
            self.bind_to_entity_set(entity_set)

    def bind_to_entity_set(self, entity_set):
        """Binds this entity store to the given entity set.

        Not thread safe."""
        entity_set.bind(EntityCollection, entity_store=self)
        self.entity_set = entity_set

    def add_association(self, aindex, reverse):
        """Adds an association index from this entity set (if reverse is
        False) or to this entity set (reverse is True).

        Not thread safe."""
        if reverse:
            self.reverseAssociations[aindex.name] = aindex
        else:
            self.associations[aindex.name] = aindex

    def add_entity(self, e):
        key = e.key()
        value = []
        for pname in e.data_keys():
            p = e[pname]
            if not e.is_selected(pname) and pname not in e.entity_set.keys:
                # need to insert the default value
                if isinstance(p, edm.Complex):
                    value.append(self.get_tuple_from_complex_default(p))
                elif isinstance(p, edm.SimpleValue):
                    v = p.p_def()
                    v.set_default_value()
                    value.append(v.value)
                else:
                    raise RuntimeError("property not simple or complex")
            elif isinstance(p, edm.Complex):
                value.append(self.get_tuple_from_complex(p))
            elif isinstance(p, edm.SimpleValue):
                value.append(p.value)
            else:
                raise RuntimeError("property not simple or complex")
        with self.container.lock:
            if key in self.data:
                raise edm.ConstraintError("Duplicate key: %s", str(key))
            self.data[key] = tuple(value)
            # At this point the entity exists
            e.exists = True

    def count_entities(self):
        with self.container.lock:
            return len(self.data)

    def generate_entities(self, select=None):
        """A generator function that returns the entities in the entity set

        The implementation is a compromise, we don't lock the container
        for the duration of the iteration, instead we work on a copy of
        the list of keys.  This creates the slight paradox that an entity
        deleted during the iteration *may* not be yielded but an entity
        inserted during the iteration will never be yielded."""
        with self.container.lock:
            keys = dict_keys(self.data)
        for k in keys:
            e = self.read_entity(k, select)
            if e is not None:
                yield e

    def read_entity(self, key, select=None):
        with self.container.lock:
            value = self.data.get(key, None)
            if value is None:
                return None
            e = Entity(self.entity_set, self)
            if select is not None:
                e.expand(None, select)
            for pname, pvalue in zip(e.data_keys(), value):
                p = e[pname]
                if (select is None or e.is_selected(pname) or
                        pname in self.entity_set.keys):
                    # for speed, check if selection is an issue first
                    # we always include the keys
                    if isinstance(p, edm.Complex):
                        self.set_complex_from_tuple(p, pvalue)
                    else:
                        p.set_from_value(pvalue)
                else:
                    if isinstance(p, edm.Complex):
                        p.set_null()
                    else:
                        p.set_from_value(None)
            e.exists = True
        return e

    def set_complex_from_tuple(self, complex_value, t):
        for pname, pvalue in zip(complex_value.iterkeys(), t):
            p = complex_value[pname]
            if isinstance(p, edm.Complex):
                self.set_complex_from_tuple(p, pvalue)
            else:
                p.set_from_value(pvalue)

    def read_stream(self, key):
        """Returns a tuple of the entity's media stream

        The return value is a tuple: (data, StreamInfo)."""
        with self.container.lock:
            if key not in self.data:
                raise KeyError
            if key in self.streams:
                stream, sinfo = self.streams[key]
                return stream, sinfo
            else:
                return '', odata.StreamInfo(size=0)

    def update_entity(self, e, merge=True):
        # e is an EntityTypeInstance, we need to convert it to a tuple
        key = e.key()
        with self.container.lock:
            value = list(self.data[key])
            i = 0
            for pname in e.data_keys():
                if pname in e.entity_set.keys:
                    # always merge for key properties (no change)
                    pass
                elif e.is_selected(pname):
                    p = e[pname]
                    if isinstance(p, edm.Complex):
                        value[i] = self.get_tuple_from_complex(p)
                    elif isinstance(p, edm.SimpleValue):
                        value[i] = p.value
                elif not merge:
                    # replace semantics, use the default value
                    p = e[pname]
                    if isinstance(p, edm.Complex):
                        value[i] = self.get_tuple_from_complex_default(p)
                    elif isinstance(p, edm.SimpleValue):
                        v = p.p_def()
                        v.set_default_value()
                        value[i] = v.value
                i = i + 1
            self.data[key] = tuple(value)

    def update_entity_stream(self, key, stream, sinfo):
        with self.container.lock:
            self.streams[key] = (stream, sinfo)

    def get_tuple_from_complex(self, complex_value):
        value = []
        for pname in complex_value.iterkeys():
            p = complex_value[pname]
            if isinstance(p, edm.Complex):
                value.append(self.get_tuple_from_complex(p))
            else:
                value.append(p.value)
        return tuple(value)

    def get_tuple_from_complex_default(self, complex_value):
        value = []
        for pname in complex_value.iterkeys():
            p = complex_value[pname]
            if isinstance(p, edm.Complex):
                value.append(self.get_tuple_from_complex_default(p))
            else:
                v = p.p_def()
                v.set_default_value()
                value.append(v.value)
        return tuple(value)

    def start_deleting_entity(self, key):
        """Returns True if it is OK to start deleting the entity, False
        if it is already being deleted.

        Not thread-safe, must only be called if you have
        acquired the container lock."""
        if key in self._deleting:
            return False
        elif key not in self.data:
            raise KeyError(repr(key))
        else:
            self._deleting.add(key)
            return True

    def deleting(self, key):
        """Returns True if the entity with key is currently being
        deleted.

        Not thread-safe, must only be called if you have
        acquired the container lock."""
        return key in self._deleting

    def stop_deleting(self, key):
        """Removes *key* from the list of entities being deleted.

        Not thread-safe, must only be called if you have
        acquired the container lock."""
        if key in self._deleting:
            self._deleting.remove(key)

    def delete_entity(self, key):
        with self.container.lock:
            for aindex in dict_values(self.associations):
                aindex.delete_hook(key)
            for aindex in dict_values(self.reverseAssociations):
                aindex.rdelete_hook(key)
            del self.data[key]
            if key in self.streams:
                del self.streams[key]

    def test_key(self, key):
        """Return True if *key* is in the container.

        Not thread-safe, should only be called if you have the container
        lock."""
        return key in self.data


class InMemoryAssociationIndex(object):

    """An in memory index that implements the association between two
    sets of entities.

    Instances of this class create storage for an association between
    *from_store* and *to_store* which are
    :py:class:`InMemoryEntityStore` instances.

    If *property_name* (and optionally *reverse_name*) is provided then
    the index is immediately bound to the associated entity sets, see
    :py:meth:`bind` for more information."""

    def __init__(
            self,
            container,
            association_set,
            from_store,
            to_store,
            property_name=None,
            reverse_name=None):
        #: the :py:class:`InMemoryEntityContainer` that contains this index
        self.container = container
        # : the name of the association set this index represents
        self.name = association_set.name
        #: a dictionary mapping source keys on to sets of target keys
        self.index = {}
        #: the reverse index mapping target keys on to sets of source keys
        self.reverseIndex = {}
        self.from_store = from_store
        from_store.add_association(self, reverse=False)
        self.to_store = to_store
        to_store.add_association(self, reverse=True)
        #: the :py:class:`AssociationSetEnd` bound to :py:attr:`from_store`
        self.from_end = None
        #: the :py:class:`AssociationSetEnd` bound to :py:attr:`to_store`
        self.to_end = None
        if property_name is not None:
            self.bind(property_name, reverse_name)

    def bind(self, property_name, reverse_name=None):
        """Binds this index to the named property of the entity set
        bound to :py:attr:`from_store`.

        If the association is reversible *reverse_name* can also be used
        to bind that property in the entity set bound to
        :py:attr:`to_store`"""
        from_end = self.from_store.entity_set.navigation[property_name]
        if self.from_end is None:
            self.from_end = from_end
            self.to_end = from_end.otherEnd
        self.from_store.entity_set.bind_navigation(
            property_name,
            NavigationCollection,
            aindex=self,
            reverse=False)
        if reverse_name is not None:
            self.to_store.entity_set.bind_navigation(
                reverse_name,
                NavigationCollection,
                aindex=self,
                reverse=True)

    def bind_reverse(self, reverse_name):
        """Binds this index to *reverse_name* in the :py:attr:`to_store`"""
        if reverse_name is not None:
            self.to_store.entity_set.bind_navigation(
                reverse_name,
                NavigationCollection,
                aindex=self,
                reverse=True)

    def add_link(self, from_key, to_key):
        """Adds a link from *from_key* to *to_key*"""
        with self.container.lock:
            self.index.setdefault(from_key, set()).add(to_key)
            self.reverseIndex.setdefault(to_key, set()).add(from_key)

    def get_links_from(self, from_key):
        """Returns a tuple of to_keys linked from *from_key*"""
        with self.container.lock:
            return tuple(self.index.get(from_key, ()))

    def get_links_to(self, to_key):
        """Returns a tuple of from_keys linked to *to_key*"""
        with self.container.lock:
            return tuple(self.reverseIndex.get(to_key, ()))

    def remove_link(self, from_key, to_key):
        """Removes a link from *from_key* to *to_key*"""
        with self.container.lock:
            self.index.get(from_key, set()).discard(to_key)
            self.reverseIndex.get(to_key, set()).discard(from_key)

    def delete_hook(self, from_key):
        """Called only by :py:meth:`InMemoryEntityStore.delete_entity`"""
        try:
            to_keys = self.index[from_key]
            for to_key in to_keys:
                from_keys = self.reverseIndex[to_key]
                from_keys.remove(from_key)
                if len(from_keys) == 0:
                    del self.reverseIndex[to_key]
            del self.index[from_key]
        except KeyError:
            pass

    def rdelete_hook(self, to_key):
        """Called only by :py:meth:`InMemoryEntityStore.delete_entity`"""
        try:
            from_keys = self.reverseIndex[to_key]
            for from_key in from_keys:
                to_keys = self.index[from_key]
                to_keys.remove(to_key)
                if len(to_keys) == 0:
                    del self.index[from_key]
            del self.reverseIndex[to_key]
        except KeyError:
            pass


# class WEntityStream(StringIO):
#
#     def __init__(self, entity):
#         self.entity = entity
#         StringIO.__init__(self)
#
#     def close(self):
#         type, data = self.entity.get_stream_info()
#         if type is None:
#             type = params.APPLICATION_OCTETSTREAM
#         self.entity.set_stream(type, [self.getvalue()])
#         StringIO.close(self)
#
#
class Entity(odata.Entity):

    """We override the CSDL's Entity class for legacy reasons"""

    def __init__(self, entity_set, entity_store):
        super(Entity, self).__init__(entity_set)
        self.entity_store = entity_store  # : points to the entity storage


class EntityCollection(odata.EntityCollection):

    """An entity collection that provides access to entities stored in
    the :py:class:`InMemoryEntitySet` *entity_store*."""

    def __init__(self, entity_store, **kwargs):
        super(EntityCollection, self).__init__(**kwargs)
        self.entity_store = entity_store

    def new_entity(self):
        """Returns an OData aware instance"""
        e = Entity(self.entity_set, self.entity_store)
        return e

    def insert_entity(self, entity, from_end=None):
        """The optional *from_end* is an
        :py:class:`AssociationSetEnd` instance that is bound to *this*
        collection's entity set.  It indicates that we are being created
        by a deep insert or through direct insertion into a
        :py:class:`NavigationCollection` representing the
        corresponding association.  This information can be used to
        suppress a constraint check (on the assumption that it has
        already been checked) by passing *from_end* directly to
        :py:meth:`Entity.check_navigation_constraints`."""
        with self.entity_store.container.lock:
            # This is a bit clumsy, but we lock the whole container while we
            # check all constraints and perform any nested deletes
            try:
                key = entity.key()
            except KeyError:
                # if the entity doesn't have a key, autogenerate one
                # until we have one that is good
                for i in range3(100):
                    entity.auto_key()
                    key = entity.key()
                    if not self.entity_store.test_key(key):
                        break
                    else:
                        key = None
            if key is None:
                logging.error("Failed to find an unused key in %s "
                              "after 100 attempts", entity.entity_set.name)
                raise edm.EDMError("Auto-key failure" %
                                   odata.ODataURI.format_entity_key(entity))
            # Check constraints
            entity.check_navigation_constraints(from_end)
            self.entity_store.add_entity(entity)
            self.update_bindings(entity)

    def __len__(self):
        if self.filter is None:
            return self.entity_store.count_entities()
        else:
            result = 0
            for e in self.filter_entities(
                    self.entity_store.generate_entities()):
                result += 1
            return result

    def itervalues(self):
        return self.order_entities(
            self.expand_entities(
                self.filter_entities(
                    self.entity_store.generate_entities(self.select))))

    def __getitem__(self, key):
        e = self.entity_store.read_entity(key, self.select)
        if e is not None and self.check_filter(e):
            e.expand(self.expand, self.select)
            return e
        else:
            raise KeyError

    def update_entity(self, entity, merge=True):
        # force an error if we don't have a key
        with self.entity_store.container.lock:
            self.entity_store.update_entity(entity, merge)
            # now process any bindings
            self.update_bindings(entity)

    def __delitem__(self, key):
        """We do a cascade delete of everything that *must* be linked to
        us. We don't need to bother about deleting links because the
        delete hooks on entity_store do this automatically."""
        if not self.entity_store.start_deleting_entity(key):
            # we're already being deleted so do nothing
            return
        try:
            for linkEnd, navName in dict_items(self.entity_set.linkEnds):
                if linkEnd.associationEnd.multiplicity != edm.Multiplicity.One:
                    continue
                # there must be one of us, delete the other end with
                # the exception that if there is no navigation property
                # bound to this property then we won't do a cascade delete
                # We have to go straight to the storage layer to sort
                # this out. We are allowed to raise
                # edm.NavigationConstraintError here but then it would
                # be impossible to delete 1-1 related entities which is
                # a bit limited
                as_name = linkEnd.parent.name
                aindex = self.entity_store.associations.get(
                    linkEnd.parent.name, None)
                if aindex:
                    with aindex.to_store.entity_set.open() as \
                            toCollection:
                        for to_key in aindex.get_links_from(key):
                            if navName is None and \
                                    not aindex.to_store.deleting(to_key):
                                # if we are not in the process of
                                # deleting to_key and there is no
                                # navigation property linking us to it
                                # then raise an error
                                raise edm.NavigationConstraintError(
                                    "Can't cascade delete from an entity in "
                                    "%s as the association set %s is not "
                                    "bound to a navigation property" %
                                    (self.entity_set.name, as_name))
                            # delete this link first to prevent infinite
                            # recursion
                            aindex.remove_link(key, to_key)
                            del toCollection[to_key]
                else:
                    aindex = self.entity_store.reverseAssociations.get(
                        linkEnd.parent.name,
                        None)
                    with aindex.from_store.entity_set.open() as \
                            from_collection:
                        for from_key in aindex.get_links_to(key):
                            if navName is None and not \
                                    aindex.from_store.deleting(from_key):
                                raise edm.NavigationConstraintError(
                                    "Can't cascade delete from an entity in "
                                    "%s as the association set %s is not "
                                    "bound to a navigation property" %
                                    (self.entity_set.name, as_name))
                            aindex.remove_link(from_key, key)
                            del from_collection[from_key]
            self.entity_store.delete_entity(key)
        finally:
            self.entity_store.stop_deleting(key)

    def _read_src(self, src, max_bytes=None):
        value = []
        nbytes = max_bytes
        while nbytes is None or nbytes > 0:
            if nbytes is None:
                data = src.read()
            else:
                data = src.read(nbytes)
                nbytes -= len(data)
            if not data:
                break
            else:
                value.append(data)
        return b''.join(value)

    def new_stream(self, src, sinfo=None, key=None):
        e = self.new_entity()
        if sinfo is None:
            sinfo = odata.StreamInfo()
        etag = e.etag_values()
        if len(etag) == 1 and isinstance(etag[0], edm.BinaryValue):
            h = hashlib.sha256()
            etag = etag[0]
        else:
            h = None
        data = self._read_src(src, sinfo.size)
        if h is not None:
            h.update(data)
            etag.set_from_value(h.digest())
        if sinfo.created is None:
            sinfo.created = iso.TimePoint.from_now_utc()
        if sinfo.modified is None:
            sinfo.modified = sinfo.created
        sinfo.size = len(data)
        sinfo.md5 = hashlib.md5(data).digest()
        # we need the lock to ensure the entity and stream and updated
        # together
        with self.entity_store.container.lock:
            if key is None:
                e.auto_key()
            else:
                e.set_key(key)
            for i in range3(1000):
                key = e.key()
                if not self.entity_store.test_key(key):
                    break
                e.auto_key()
            self.insert_entity(e)
            self.entity_store.update_entity_stream(key, data, sinfo)
        return e

    def update_stream(self, src, key, sinfo=None):
        update = False
        e = self[key]
        old_data, oldinfo = self.entity_store.read_stream(key)
        if sinfo is None:
            sinfo = odata.StreamInfo()
        etag = e.etag_values()
        if len(etag) == 1 and isinstance(etag[0], edm.BinaryValue):
            h = hashlib.sha256()
            etag = etag[0]
        else:
            h = None
        data = self._read_src(src, sinfo.size)
        if h is not None:
            h.update(data)
            etag.set_from_value(h.digest())
            update = True
        if sinfo.created is None:
            sinfo.created = oldinfo.created
        if sinfo.created is None:
            sinfo.created = iso.TimePoint.from_now_utc()
        if sinfo.modified is None:
            sinfo.modified = iso.TimePoint.from_now_utc()
        sinfo.size = len(data)
        sinfo.md5 = hashlib.md5(data).digest()
        # we need the lock to ensure the entity and stream and updated
        # together
        with self.entity_store.container.lock:
            if update:
                self.update_entity(e)
            self.entity_store.update_entity_stream(key, data, sinfo)

    def read_stream(self, key, out=None):
        data, sinfo = self.entity_store.read_stream(key)
        if out is not None:
            nbytes = 0
            while nbytes < len(data):
                result = out.write(data[nbytes:])
                if result is not None:
                    nbytes += result
                else:
                    break
        return sinfo

    def read_stream_close(self, key):
        data, sinfo = self.entity_store.read_stream(key)
        return sinfo, self._stream_gen(data)

    def _stream_gen(self, data):
        try:
            yield data
        finally:
            self.close()


class NavigationCollection(odata.NavigationCollection):

    def __init__(self, aindex, reverse, **kwargs):
        super(NavigationCollection, self).__init__(**kwargs)
        self.aindex = aindex
        self.reverse = reverse
        if self.reverse:
            self.lookupMethod = self.aindex.get_links_to
            self.rLookupMethod = self.aindex.get_links_from
        else:
            self.lookupMethod = self.aindex.get_links_from
            self.rLookupMethod = self.aindex.get_links_to
        self.collection = self.entity_set.open()
        self.key = self.from_entity.key()

    def new_entity(self):
        """Returns an OData aware instance"""
        return self.collection.new_entity()

    def close(self):
        if self.collection is not None:
            self.collection.close()
            self.collection = None

    def insert_entity(self, entity):
        """Inserts a new *entity* into the target entity set *and*
        simultaneously creates a link to it from the source entity."""
        with self.entity_set.open() as baseCollection:
            baseCollection.insert_entity(entity,
                                         from_end=self.from_end.otherEnd)
            self[entity.key()] = entity

    def __len__(self):
        if self.filter is None:
            result_set = self.lookupMethod(self.key)
            return len(result_set)
        else:
            result = 0
            for e in self.filter_entities(self.entity_generator()):
                result += 1
            return result

    def entity_generator(self):
        # we create a collection from the appropriate entity set first
        result_set = self.lookupMethod(self.key)
        for k in result_set:
            yield self.collection[k]

    def itervalues(self):
        return self.order_entities(
            self.expand_entities(
                self.filter_entities(
                    self.entity_generator())))

    def __getitem__(self, key):
        result_set = self.lookupMethod(self.key)
        if key in result_set:
            result = self.collection[key]
            if self.filter is None:
                if self.check_filter(result):
                    return result
            else:
                return result
        raise KeyError(key)

    def __setitem__(self, key, value):
        result_set = self.lookupMethod(self.key)
        if key in result_set:
            # no operation
            return
        # forces a check of value to ensure it is good
        self.collection[key] = value
        if self.toMultiplicity != edm.Multiplicity.Many:
            if result_set:
                raise edm.NavigationConstraintError(
                    "Can't add multiple links to navigation property %s" %
                    self.name)
        if self.fromMultiplicity != edm.Multiplicity.Many:
            if self.rLookupMethod(key):
                raise edm.NavigationConstraintError(
                    "Entity %s is already bound through this association" %
                    value.get_location())
        # clear to add this one to the index
        if self.reverse:
            self.aindex.add_link(key, self.key)
        else:
            self.aindex.add_link(self.key, key)

    def __delitem__(self, key):
        # Before we remove a link we need to know if either entity
        # requires a link, if so, this deletion will result in a
        # constraint violation
        if (self.fromMultiplicity == edm.Multiplicity.One or
                self.toMultiplicity == edm.Multiplicity.One):
            raise edm.NavigationConstraintError("Can't remove a required link")
        result_set = self.lookupMethod(self.key)
        if key not in result_set:
            raise KeyError
        if self.reverse:
            self.aindex.remove_link(key, self.key)
        else:
            self.aindex.remove_link(self.key, key)

    def replace(self, entity):
        key = entity.key()
        result_set = list(self.lookupMethod(self.key))
        if result_set == [key]:
            # nothing to do!
            return
        if self.fromMultiplicity == edm.Multiplicity.One:
            if result_set:
                # we can't delete these links because we are required
                raise edm.NavigationConstraintError(
                    "Can't remove a required link")
            else:
                self[key] = entity
        else:
            # add the new link first
            if key not in result_set:
                if self.reverse:
                    self.aindex.add_link(key, self.key)
                else:
                    self.aindex.add_link(self.key, key)
            for oldKey in result_set:
                # now remove all the old keys.  This implementation
                # is the same regardless of the allowed multiplicity.
                # This doesn't just save coding, it ensures that
                # corrupted indexes are self-correcting
                if oldKey != key:
                    if self.reverse:
                        self.aindex.remove_link(oldKey, self.key)
                    else:
                        self.aindex.remove_link(self.key, oldKey)


class BatchImportError(edm.ConstraintError):

    """Raised when a batch import cannot be committed.

    The :py:attr:`line` attribute identifies the input line (counting
    from 1) of the offending entity or link when the batch was parsed
    from an ordered input, it is None when no line information is
    available."""

    def __init__(self, msg, line=None):
        super(BatchImportError, self).__init__(msg)
        #: the 1-based input line of the offending row, or None
        self.line = line


class BatchImport(object):

    """Stages a whole file's worth of entities for a single commit.

    Entities and the links between them are held in private staging
    storage until :py:meth:`commit` is called, so an observer reading
    the container through an ordinary collection never sees a partially
    imported file.  Entities may be supplied in any order: an entity can
    be bound (by key) to another entity that appears later in the same
    input and the forward reference is resolved when the batch commits.

    Bindings must use the relationship multiplicity already declared in
    the model; the batch does not invent or relax any associations.

    A batch is used once.  If a commit fails nothing is published and the
    batch's staging storage is discarded automatically, so a later retry
    with a corrected input is not obstructed by keys held by the failed
    attempt."""

    #: attribute used to mark stub entities created to hold the place of
    #: a link target that has not been staged yet
    stub_attr = '_pyslet_batch_stub'

    def __init__(self, container):
        #: the :py:class:`InMemoryEntityContainer` the batch targets
        self.container = container
        #: mapping of entity set name to a dict of staged entities, keyed
        #: by entity key and holding (entity, line) tuples
        self.entities = {}
        for es_name in dict_keys(container.entityStorage):
            self.entities[es_name] = {}
        #: mapping of association set name to a list of staged links,
        #: each a (from_key, to_key, line) tuple
        self.links = {}
        for as_name in dict_keys(container.associationStorage):
            self.links[as_name] = []
        #: False once the batch has committed or been rolled back
        self.active = True

    def _require_active(self):
        if not self.active:
            raise BatchImportError(
                "This batch import has already been committed or closed")

    def add_entity(self, entity, line=None):
        """Stages a new *entity* in the batch.

        The entity must not already exist in the container and its key
        must not already be present in the batch.  Any navigation
        bindings attached to the entity (via
        :py:meth:`~pyslet.odata2.csdl.DeferredValue.bind_entity`) are
        staged along with it.  Returns the entity's key.

        *line* is an optional 1-based index into the input file, used to
        pinpoint the offending row in any error report."""
        self._require_active()
        es_name = entity.entity_set.name
        if es_name not in self.entities:
            raise BatchImportError(
                "Entity set %s is not part of this container" % es_name,
                line)
        if entity.exists:
            raise BatchImportError(
                "Entity %s already exists and cannot be imported" %
                str(entity.get_location()), line)
        try:
            key = entity.key()
        except KeyError:
            raise BatchImportError(
                "Entity in %s has a NULL key" % es_name, line)
        store = self.container.entityStorage[es_name]
        key_text = odata.ODataURI.format_key_dict(
            entity.entity_set.get_key_dict(key))
        if key in self.entities[es_name]:
            raise BatchImportError(
                "Duplicate key in input: %s%s" % (es_name, key_text),
                line)
        if store.test_key(key):
            raise BatchImportError(
                "Duplicate key: %s%s already exists in %s" % (
                    es_name, key_text, es_name),
                line)
        self.entities[es_name][key] = (entity, line)
        for nav_name, dv in entity.navigation_items():
            self._stage_bindings(entity, nav_name, dv, line)
        return key

    def add_link(self, entity_set, navigation_name, from_key, target,
                 line=None):
        """Stages a single navigation link without staging an entity.

        *entity_set* is the source :py:class:`~pyslet.odata2.csdl.EntitySet`,
        *navigation_name* the name of the navigation property carrying
        the relationship and *from_key* the key of the source entity.
        *target* is the target entity's key, or a target entity instance
        (which must itself be staged in the batch or already present in
        the container).

        The source must be part of the batch (a link cannot be attached
        to an existing entity) but the target may appear later in the
        same input."""
        self._require_active()
        es_name = entity_set.name
        if es_name not in self.entities:
            raise BatchImportError(
                "Entity set %s is not part of this container" % es_name,
                line)
        if from_key not in self.entities[es_name]:
            raise BatchImportError(
                "Cannot link from %s%s: the source entity is not part of "
                "this import" % (
                    es_name,
                    odata.ODataURI.format_key_dict(
                        entity_set.get_key_dict(from_key))),
                line)
        target_key = self._binding_key(entity_set, navigation_name,
                                       target, line)
        as_name = entity_set.navigation[navigation_name].parent.name
        self.links[as_name].append((from_key, target_key, line))

    def _stage_bindings(self, entity, nav_name, deferred, line):
        as_name = entity.entity_set.navigation[nav_name].parent.name
        from_key = entity.key()
        for binding in deferred.bindings:
            target_key = self._binding_key(entity.entity_set, nav_name,
                                           binding, line)
            self.links[as_name].append(
                (from_key, target_key, line))
        # the bindings are now owned by the batch, prevent them being
        # actioned again if the entity object is reused by the caller
        deferred.clear_bindings()

    def _binding_key(self, entity_set, nav_name, binding, line):
        target_set = entity_set.get_target(nav_name)
        if isinstance(binding, edm.Entity):
            if binding.entity_set is not target_set:
                raise BatchImportError(
                    "Binding for %s.%s targets the wrong entity set" %
                    (entity_set.name, nav_name), line)
            target_key = binding.key()
            if not binding.exists and not getattr(
                    binding, self.stub_attr, False) and \
                    target_key not in self.entities[target_set.name]:
                # a freshly created entity handed to us, stage it too so
                # that the forward reference can be resolved
                self.add_entity(binding, line)
            return target_key
        else:
            return target_set.get_key(binding)

    def _validate(self):
        # check that every link endpoint exists either in the container
        # or in the batch, applying the multiplicity declared by the model
        for as_name, link_list in dict_items(self.links):
            aindex = self.container.associationStorage[as_name]
            for from_key, to_key, line in link_list:
                self._check_endpoint(aindex.to_store, to_key, as_name,
                                     line)
                self._check_endpoint(aindex.from_store, from_key,
                                     as_name, line)
                self._check_multiplicity(aindex, from_key, to_key, line)
        # required navigation properties must be bound for every
        # staged entity
        for es_name, staged in dict_items(self.entities):
            entity_set = self.container.entityStorage[es_name].entity_set
            for key, entry in dict_items(staged):
                self._check_required(entity_set, entry[0], key,
                                     entry[1])

    def _check_endpoint(self, store, key, as_name, line):
        if key in self.entities[store.entity_set.name]:
            return
        if store.test_key(key):
            return
        raise BatchImportError(
            "Missing target for relationship %s: %s%s" % (
                as_name, store.entity_set.name,
                odata.ODataURI.format_key_dict(
                    store.entity_set.get_key_dict(key))),
            line)

    def _check_multiplicity(self, aindex, from_key, to_key, line):
        to_end = aindex.to_end
        if to_end.associationEnd.multiplicity != edm.Multiplicity.Many:
            others = set(aindex.get_links_from(from_key))
            others.update(self._staged_targets(aindex.name, from_key))
            others.discard(to_key)
            if others:
                self._multiplicity_error(to_end, aindex.name, line)
        from_end = aindex.from_end
        if from_end.associationEnd.multiplicity != \
                edm.Multiplicity.Many:
            others = set(aindex.get_links_to(to_key))
            others.update(self._staged_sources(aindex.name, to_key))
            others.discard(from_key)
            if others:
                self._multiplicity_error(from_end, aindex.name, line)

    def _staged_targets(self, as_name, from_key):
        result = set()
        for staged_from, staged_to, line in self.links[as_name]:
            if staged_from == from_key:
                result.add(staged_to)
        return result

    def _staged_sources(self, as_name, to_key):
        result = set()
        for staged_from, staged_to, line in self.links[as_name]:
            if staged_to == to_key:
                result.add(staged_from)
        return result

    def _multiplicity_error(self, end, as_name, line):
        encoded = edm.Multiplicity.Encode[end.associationEnd.multiplicity]
        raise BatchImportError(
            "Multiple links violate the %s multiplicity at end %s of %s"
            % (encoded, end.name, as_name), line)

    def _check_required(self, entity_set, entity, key, line):
        bad_end = entity_set.unboundPrincipal
        if bad_end is not None:
            aindex = self.container.associationStorage[
                bad_end.parent.name]
            if not self._end_is_bound(aindex, bad_end, key):
                raise BatchImportError(
                    "Entity %s%s has an unbound principal" % (
                        entity_set.name,
                        odata.ODataURI.format_key_dict(
                            entity_set.get_key_dict(key))),
                    line)
        for nav_name, dv in entity.navigation_items():
            if not dv.isRequired:
                continue
            as_name = entity_set.navigation[nav_name].parent.name
            aindex = self.container.associationStorage[as_name]
            from_end = entity_set.navigation[nav_name]
            if not self._end_is_bound(aindex, from_end, key):
                raise BatchImportError(
                    "Required navigation property %s of %s%s is not "
                    "bound" % (
                        nav_name, entity_set.name,
                        odata.ODataURI.format_key_dict(
                            entity_set.get_key_dict(key))),
                    line)

    def _end_is_bound(self, aindex, end, key):
        if aindex.from_end is end:
            for from_key, to_key, line in self.links[aindex.name]:
                if from_key == key:
                    return True
            return bool(aindex.get_links_from(key))
        else:
            for from_key, to_key, line in self.links[aindex.name]:
                if to_key == key:
                    return True
            return bool(aindex.get_links_to(key))

    def commit(self):
        """Validates and publishes the whole batch atomically.

        On success every staged entity and link becomes visible together
        and the batch is closed.  On failure nothing is published, the
        staging storage is discarded and a :py:class:`BatchImportError`
        is raised."""
        self._require_active()
        try:
            with self.container.lock:
                self._validate()
                self._publish()
        except Exception:
            self.rollback()
            raise
        self.active = False

    def _publish(self):
        # re-check keys under the lock as the container may have changed
        # while the batch was being assembled
        for es_name, staged in dict_items(self.entities):
            store = self.container.entityStorage[es_name]
            for key, entry in dict_items(staged):
                if store.test_key(key):
                    raise BatchImportError(
                        "Duplicate key: %s%s already exists in %s" % (
                            es_name,
                            odata.ODataURI.format_key_dict(
                                store.entity_set.get_key_dict(key)),
                            es_name),
                        entry[1])
        published_entities = []
        published_links = []
        try:
            # publish all the entities first, links become visible only
            # once every entity exists
            for es_name, staged in dict_items(self.entities):
                store = self.container.entityStorage[es_name]
                for key, entry in dict_items(staged):
                    store.add_entity(entry[0])
                    published_entities.append((store, key))
            for as_name, link_list in dict_items(self.links):
                aindex = self.container.associationStorage[as_name]
                for from_key, to_key, line in link_list:
                    aindex.add_link(from_key, to_key)
                    published_links.append(
                        (aindex, from_key, to_key))
        except Exception:
            # undo anything already published so the container is left
            # exactly as it was before the commit
            for aindex, from_key, to_key in reversed(published_links):
                aindex.remove_link(from_key, to_key)
            for store, key in reversed(published_entities):
                if store.test_key(key):
                    store.delete_entity(key)
            raise

    def rollback(self):
        """Discards all staged entities and links.

        Already committed batches and the existing contents of the
        container are unaffected."""
        self.entities = {}
        self.links = {}
        self.active = False

    def import_feed(self, entity_set, objects, line_offset=0):
        """Stages a sequence of JSON-style entity objects in file order.

        *objects* is an iterable given in file order; each item is either
        a python dictionary in the same shape accepted by
        :py:meth:`pyslet.odata2.core.Entity.set_from_json_object` or a
        ``(entity_set, obj)`` tuple targeting a different set.  A plain
        dictionary is placed in *entity_set* unless its
        ``__metadata.uri`` names another entity set in the container, so
        a single file may interleave entities from several sets (for
        example products arriving before the categories they reference).
        *line_offset* is added to the zero-based position of each object
        when reporting its input line (useful when the feed does not
        start on the first line of the file).

        Link objects use the standard ``{"__metadata": {"uri": ...}}``
        form and may point at entities appearing later in the sequence:
        such forward references are held as stubs and resolved when the
        batch commits."""
        self._require_active()
        resolver = _BatchResolver(self)
        for index, item in enumerate(objects):
            line = line_offset + index + 1
            if isinstance(item, tuple):
                target_set, obj = item
            else:
                obj = item
                target_set = self._feed_entity_set(entity_set, obj)
            with target_set.open() as base_collection:
                entity = base_collection.new_entity()
                try:
                    entity.set_from_json_object(obj, resolver.resolve)
                    self.add_entity(entity, line)
                except BatchImportError:
                    self.rollback()
                    raise
                except Exception as err:
                    # any malformed input must discard the partial batch
                    self.rollback()
                    raise BatchImportError(str(err), line)

    def _feed_entity_set(self, default_set, obj):
        metadata = obj.get('__metadata', None)
        if not metadata or 'uri' not in metadata:
            return default_set
        request = odata.ODataURI(metadata['uri'])
        for name, key_predicate in request.nav_path:
            if name in self.container.entityStorage:
                return self.container.container_def[name]
        return default_set


class _BatchResolver(object):

    """Resolves link URIs found in a batch feed against the container
    and the entities already staged by the batch.

    A URI that points at neither a published nor a staged entity yields
    a non-existent stub entity carrying just the key, allowing links to
    entities that appear later in the same input to be staged."""

    def __init__(self, batch):
        self.batch = batch

    def resolve(self, href):
        container = self.batch.container
        request = odata.ODataURI(href)
        target_set = None
        key = None
        for name, key_predicate in request.nav_path:
            if key_predicate is not None:
                target_set = container.container_def[name]
                key = target_set.get_key(key_predicate)
                break
        if target_set is None:
            raise odata.InvalidData(
                "Link URI does not identify an entity: %s" % str(href))
        staged = self.batch.entities[target_set.name].get(key, None)
        if staged is not None:
            return staged[0]
        with target_set.open() as collection:
            try:
                return collection[key]
            except KeyError:
                stub = collection.new_entity()
                stub.set_key(key)
                setattr(stub, BatchImport.stub_attr, True)
                return stub


class InMemoryEntityContainer(object):

    def __init__(self, container_def):
        #: the :py:class:`csdl.EntityContainer` that defines this container
        self.container_def = container_def
        """a lock that must be acquired before modifying any entity or
        association in this container"""
        self.lock = threading.RLock()
        """a mapping from entity set names to
        :py:class:`InMemoryEntityStore` instances"""
        self.entityStorage = {}
        """a mapping from association set name to
        :py:class:`InMemoryAssociationIndex` instances"""
        self.associationStorage = {}
        # for each entity set in this container, bind some storage
        for es in self.container_def.EntitySet:
            self.entityStorage[es.name] = InMemoryEntityStore(self, es)
        for es in self.container_def.EntitySet:
            from_storage = self.entityStorage[es.name]
            if es.entityType is None:
                raise edm.ModelIncomplete(
                    "EntitySet %s is not bound to an entity type" % es.name)
            for np in es.entityType.NavigationProperty:
                if np.association is None:
                    raise edm.ModelIncomplete(
                        "NavigationProperty %s.%s is not bound to an "
                        "AssociationSet" % (es.name, np.name))
                association_set_end = es.navigation[np.name]
                association_set = association_set_end.parent
                if association_set.name in self.associationStorage:
                    # we already have it, do the reverse binding
                    self.associationStorage[
                        association_set.name].bind_reverse(np.name)
                else:
                    target = es.get_target(np.name)
                    if target is None:
                        raise edm.ModelIncomplete(
                            "Target of navigation property %s.%s is not bound "
                            "to an entity set" % (es.name, np.name))
                    to_storage = self.entityStorage[target.name]
                    self.associationStorage[
                        association_set.name] = InMemoryAssociationIndex(
                        self,
                        association_set,
                        from_storage,
                        to_storage,
                        np.name)
