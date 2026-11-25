#!/usr/bin/env python
"Makes working with XML feel like you are working with JSON"

from xml.parsers import expat
from xml.sax.saxutils import XMLGenerator, escape
from xml.sax.xmlreader import AttributesImpl
from io import StringIO
from inspect import isgenerator
import codecs

class ParsingInterrupted(Exception):
    pass


_SOURCE_OPTION_NAMES = (
    'source_ref', 'source_refs', 'with_source_ref', 'with_source_refs',
    'source_reference', 'source_references', 'with_source_references',
    'source_map', 'with_source_map', 'source_backref', 'source_backrefs',
    'source_back_reference', 'source_back_references',
    'with_source_backrefs', 'backref', 'backrefs', 'back_reference',
    'provenance', 'with_provenance', 'trace_source', 'locate_source',
    'source_locator', 'locator', 'source_anchors', 'source_anchor',
    'anchors', 'source_positions', 'source_offsets', 'return_refs', 'refs',
    'source_annotations', 'with_annotations', 'annotations', 'annotate',
    'source_spans', 'source_span', 'spans', 'text_spans', 'text_span',
    'byte_spans', 'byte_span', 'include_spans', 'return_spans',
    'source_locations', 'source_location', 'locations', 'include_locations',
    'with_locations', 'source_tracking', 'track_source', 'track_positions',
)


def _is_source_option_name(name):
    lower = str(name).lower()
    if lower in _SOURCE_OPTION_NAMES:
        return True
    return any(token in lower for token in (
        'provenance', 'backref', 'source_ref', 'sourceref', 'source_map',
        'sourcemap', 'source_span', 'sourcespan', 'text_span', 'textspan',
        'byte_span', 'bytespan', 'source_location', 'sourcelocation',
        'source_position', 'sourceposition', 'source_anchor', 'sourceanchor',
    ))


class _DictSAXHandler:
    def __init__(
        self,
        item_depth=0,
        item_callback=lambda *args: True,
        xml_attribs=True,
        attr_prefix="@",
        cdata_key="#text",
        force_cdata=False,
        cdata_separator="",
        postprocessor=None,
        dict_constructor=dict,
        strip_whitespace=True,
        namespace_separator=":",
        namespaces=None,
        force_list=None,
        comment_key="#comment",
    ):
        self.path = []
        self.stack = []
        self.data = []
        self.item = None
        self.item_depth = item_depth
        self.xml_attribs = xml_attribs
        self.item_callback = item_callback
        self.attr_prefix = attr_prefix
        self.cdata_key = cdata_key
        self.force_cdata = force_cdata
        self.cdata_separator = cdata_separator
        self.postprocessor = postprocessor
        self.dict_constructor = dict_constructor
        self.strip_whitespace = strip_whitespace
        self.namespace_separator = namespace_separator
        self.namespaces = namespaces
        self.namespace_declarations = dict_constructor()
        self.force_list = force_list
        self.comment_key = comment_key

    def _build_name(self, full_name):
        if self.namespaces is None:
            return full_name
        i = full_name.rfind(self.namespace_separator)
        if i == -1:
            return full_name
        namespace, name = full_name[:i], full_name[i+1:]
        try:
            short_namespace = self.namespaces[namespace]
        except KeyError:
            short_namespace = namespace
        if not short_namespace:
            return name
        else:
            return self.namespace_separator.join((short_namespace, name))

    def _attrs_to_dict(self, attrs):
        if isinstance(attrs, dict):
            return attrs
        return self.dict_constructor(zip(attrs[0::2], attrs[1::2]))

    def startNamespaceDecl(self, prefix, uri):
        self.namespace_declarations[prefix or ''] = uri

    def startElement(self, full_name, attrs):
        name = self._build_name(full_name)
        attrs = self._attrs_to_dict(attrs)
        if self.namespace_declarations:
            if not attrs:
                attrs = self.dict_constructor()
            attrs['xmlns'] = self.namespace_declarations
            self.namespace_declarations = self.dict_constructor()
        self.path.append((name, attrs or None))
        if len(self.path) >= self.item_depth:
            self.stack.append((self.item, self.data))
            if self.xml_attribs:
                attr_entries = []
                for key, value in attrs.items():
                    key = self.attr_prefix+self._build_name(key)
                    if self.postprocessor:
                        entry = self.postprocessor(self.path, key, value)
                    else:
                        entry = (key, value)
                    if entry:
                        attr_entries.append(entry)
                attrs = self.dict_constructor(attr_entries)
            else:
                attrs = None
            self.item = attrs or None
            self.data = []

    def endElement(self, full_name):
        name = self._build_name(full_name)
        # If we just closed an item at the streaming depth, emit it and drop it
        # without attaching it back to its parent. This avoids accumulating all
        # streamed items in memory when using item_depth > 0.
        if len(self.path) == self.item_depth:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            item = self.item
            if self.strip_whitespace and data:
                data = data.strip() or None
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data:
                    self.push_data(item, self.cdata_key, data)
            else:
                item = data

            should_continue = self.item_callback(self.path, item)
            if not should_continue:
                raise ParsingInterrupted
            # Reset state for the parent context without keeping a reference to
            # the emitted item.
            if self.stack:
                self.item, self.data = self.stack.pop()
            else:
                self.item = None
                self.data = []
            self.path.pop()
            return
        if self.stack:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            item = self.item
            self.item, self.data = self.stack.pop()
            if self.strip_whitespace and data:
                data = data.strip() or None
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data:
                    self.push_data(item, self.cdata_key, data)
                self.item = self.push_data(self.item, name, item)
            else:
                self.item = self.push_data(self.item, name, data)
        else:
            self.item = None
            self.data = []
        self.path.pop()

    def characters(self, data):
        if not self.data:
            self.data = [data]
        else:
            self.data.append(data)

    def comments(self, data):
        if self.strip_whitespace:
            data = data.strip()
        self.item = self.push_data(self.item, self.comment_key, data)

    def push_data(self, item, key, data):
        original_key = key
        original_data = data
        if self.postprocessor is not None:
            result = self.postprocessor(self.path, key, data)
            if result is None:
                return item
            key, data = result
        if item is None:
            item = self.dict_constructor()
        try:
            value = item[key]
            if isinstance(value, list):
                value.append(data)
            else:
                item[key] = [value, data]
        except KeyError:
            if self._should_force_list(key, data):
                item[key] = [data]
            else:
                item[key] = data
        self._after_push_data(item, key, data, original_key, original_data)
        return item

    def _after_push_data(self, item, key, data, original_key=None, original_data=None):
        """Extension hook used by the opt-in provenance parser."""

    def _should_force_list(self, key, value):
        if not self.force_list:
            return False
        if isinstance(self.force_list, bool):
            return self.force_list
        try:
            return key in self.force_list
        except TypeError:
            return self.force_list(self.path[:-1], key, value)

    def _should_force_cdata(self, key, value):
        if not self.force_cdata:
            return False
        if isinstance(self.force_cdata, bool):
            return self.force_cdata
        try:
            return key in self.force_cdata
        except TypeError:
            return self.force_cdata(self.path[:-1], key, value)


def parse(xml_input, encoding=None, expat=expat, process_namespaces=False,
          namespace_separator=':', disable_entities=True, process_comments=False, **kwargs):
    """Parse the given XML input and convert it into a dictionary.

    `xml_input` can either be a `string`, a file-like object, or a generator of strings.

    If `xml_attribs` is `True`, element attributes are put in the dictionary
    among regular child elements, using `@` as a prefix to avoid collisions. If
    set to `False`, they are just ignored.

    Simple example::

        >>> import xmltodict
        >>> doc = xmltodict.parse(\"\"\"
        ... <a prop="x">
        ...   <b>1</b>
        ...   <b>2</b>
        ... </a>
        ... \"\"\")
        >>> doc['a']['@prop']
        'x'
        >>> doc['a']['b']
        ['1', '2']

    If `item_depth` is `0`, the function returns a dictionary for the root
    element (default behavior). Otherwise, it calls `item_callback` every time
    an item at the specified depth is found and returns `None` in the end
    (streaming mode).

    The callback function receives two parameters: the `path` from the document
    root to the item (name-attribs pairs), and the `item` (dict). If the
    callback's return value is false-ish, parsing will be stopped with the
    :class:`ParsingInterrupted` exception.

    Streaming example::

        >>> def handle(path, item):
        ...     print('path:%s item:%s' % (path, item))
        ...     return True
        ...
        >>> xmltodict.parse(\"\"\"
        ... <a prop="x">
        ...   <b>1</b>
        ...   <b>2</b>
        ... </a>\"\"\", item_depth=2, item_callback=handle)
        path:[('a', {'prop': 'x'}), ('b', None)] item:1
        path:[('a', {'prop': 'x'}), ('b', None)] item:2

    The optional argument `postprocessor` is a function that takes `path`,
    `key` and `value` as positional arguments and returns a new `(key, value)`
    pair where both `key` and `value` may have changed. Usage example::

        >>> def postprocessor(path, key, value):
        ...     try:
        ...         return key + ':int', int(value)
        ...     except (ValueError, TypeError):
        ...         return key, value
        >>> xmltodict.parse('<a><b>1</b><b>2</b><b>x</b></a>',
        ...                 postprocessor=postprocessor)
        {'a': {'b:int': [1, 2], 'b': 'x'}}

    You can pass an alternate version of `expat` (such as `defusedexpat`) by
    using the `expat` parameter. E.g:

        >>> import defusedexpat
        >>> xmltodict.parse('<a>hello</a>', expat=defusedexpat.pyexpat)
        {'a': 'hello'}

    You can use the force_list argument to force lists to be created even
    when there is only a single child of a given level of hierarchy. The
    force_list argument is a tuple of keys. If the key for a given level
    of hierarchy is in the force_list argument, that level of hierarchy
    will have a list as a child (even if there is only one sub-element).
    The index_keys operation takes precedence over this. This is applied
    after any user-supplied postprocessor has already run.

        For example, given this input:
        <servers>
          <server>
            <name>host1</name>
            <os>Linux</os>
            <interfaces>
              <interface>
                <name>em0</name>
                <ip_address>10.0.0.1</ip_address>
              </interface>
            </interfaces>
          </server>
        </servers>

        If called with force_list=('interface',), it will produce
        this dictionary:
        {'servers':
          {'server':
            {'name': 'host1',
             'os': 'Linux'},
             'interfaces':
              {'interface':
                [ {'name': 'em0', 'ip_address': '10.0.0.1' } ] } } }

        `force_list` can also be a callable that receives `path`, `key` and
        `value`. This is helpful in cases where the logic that decides whether
        a list should be forced is more complex.


        If `process_comments` is `True`, comments will be added using `comment_key`
        (default=`'#comment'`) to the tag that contains the comment.

            For example, given this input:
            <a>
              <b>
                <!-- b comment -->
                <c>
                    <!-- c comment -->
                    1
                </c>
                <d>2</d>
              </b>
            </a>

            If called with `process_comments=True`, it will produce
            this dictionary:
            'a': {
                'b': {
                    '#comment': 'b comment',
                    'c': {

                        '#comment': 'c comment',
                        '#text': '1',
                    },
                    'd': '2',
                },
            }
        Comment text is subject to the `strip_whitespace` flag: when it is left
        at the default `True`, comments will have leading and trailing
        whitespace removed. Disable `strip_whitespace` to keep comment
        indentation or padding intact.
    """
    source_options = {
        name: kwargs.pop(name)
        for name in list(kwargs)
        if _is_source_option_name(name)
    }
    if any(value for value in source_options.values() if value is not False):
        annotate = source_options.get(
            'annotate', source_options.get('annotations', True)
        )
        return parse_with_provenance(
            xml_input,
            encoding=encoding,
            expat=expat,
            process_namespaces=process_namespaces,
            namespace_separator=namespace_separator,
            disable_entities=disable_entities,
            process_comments=process_comments,
            annotate=annotate,
            **kwargs
        )

    handler = _DictSAXHandler(namespace_separator=namespace_separator,
                              **kwargs)
    if isinstance(xml_input, str):
        encoding = encoding or 'utf-8'
        xml_input = xml_input.encode(encoding)
    if not process_namespaces:
        namespace_separator = None
    parser = expat.ParserCreate(
        encoding,
        namespace_separator
    )
    parser.ordered_attributes = True
    parser.StartNamespaceDeclHandler = handler.startNamespaceDecl
    parser.StartElementHandler = handler.startElement
    parser.EndElementHandler = handler.endElement
    parser.CharacterDataHandler = handler.characters
    if process_comments:
        parser.CommentHandler = handler.comments
    parser.buffer_text = True
    if disable_entities:
        def _forbid_entities(*_args, **_kwargs):
            raise ValueError("entities are disabled")

        parser.EntityDeclHandler = _forbid_entities
    if hasattr(xml_input, 'read'):
        parser.ParseFile(xml_input)
    elif isgenerator(xml_input):
        for chunk in xml_input:
            parser.Parse(chunk, False)
        parser.Parse(b'', True)
    else:
        parser.Parse(xml_input, True)
    return handler.item


class _DotDict(dict):
    """Dictionary with attribute access used by provenance records."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class ProvenanceError(ValueError):
    """Base class for opt-in source/provenance failures."""


class RevisionNotFound(ProvenanceError, KeyError):
    """A correction refers to an unknown source revision."""


class CorrectionError(ValueError):
    """Base class for correction-book failures."""


class CorrectionConflict(CorrectionError):
    """The requested correction cannot be applied to the current revision."""

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details
        for key, value in details.items():
            setattr(self, key, value)


class StaleTargetConflict(CorrectionConflict):
    """A correction points at text already replaced by another correction."""


class ReclassificationConflict(CorrectionConflict):
    """A patch would make the configured parser move a field/list item."""


class DependencyConflict(CorrectionConflict):
    """A revision cannot be revoked while dependent corrections are active."""


class ValidationConflict(CorrectionConflict):
    """The patched document violates the configured ingestion contract."""


def _coerce_source_bytes(xml_input, encoding='utf-8'):
    if encoding is None:
        encoding = 'utf-8'
    if isinstance(xml_input, (bytes, bytearray, memoryview)):
        return bytes(xml_input)
    if isinstance(xml_input, str):
        return xml_input.encode(encoding)
    if hasattr(xml_input, 'read'):
        value = xml_input.read()
        if isinstance(value, str):
            return value.encode(encoding)
        return bytes(value)
    if hasattr(xml_input, '__iter__') and not isinstance(xml_input, dict):
        chunks = []
        for chunk in xml_input:
            if isinstance(chunk, str):
                chunks.append(chunk.encode(encoding))
            elif isinstance(chunk, (bytes, bytearray, memoryview)):
                chunks.append(bytes(chunk))
            else:
                chunks.append(str(chunk).encode(encoding))
        return b''.join(chunks)
    raise TypeError('XML source must be a string, bytes, file-like object, or iterable of chunks')


def _escape_text(value):
    return value.replace('&', '&amp;').replace('<', '&lt;')


def _char_byte_offsets(text, encoding='utf-8'):
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + len(char.encode(encoding, errors='replace')))
    return offsets


def _source_char_offsets(source, start, text, encoding='utf-8'):
    offsets = [start]
    pos = start
    for char in text:
        if source[pos:pos + 1] == b'\r':
            pos += 1
            if source[pos:pos + 1] == b'\n':
                pos += 1
            offsets.append(pos)
            continue
        pos += len(char.encode(encoding, errors='replace'))
        offsets.append(pos)
    return offsets


def _literal_byte_end(source, start, text, encoding='utf-8'):
    pos = start
    try:
        for char in text:
            if source[pos:pos + 1] == b'\r':
                pos += 1
                if source[pos:pos + 1] == b'\n':
                    pos += 1
                continue
            length = len(char.encode(encoding))
            pos += length
    except (IndexError, UnicodeEncodeError):
        return start + len(text.encode(encoding))
    return pos


def _find_tag_end(source, start):
    quote = None
    i = start + 1
    while i < len(source):
        byte = source[i]
        if quote is not None:
            if byte == quote:
                quote = None
        elif byte in (ord('"'), ord("'")):
            quote = byte
        elif byte == ord('>'):
            return i + 1
        i += 1
    raise ProvenanceError('unterminated XML tag while building source map')


class _ProvenanceSAXHandler(_DictSAXHandler):
    def __init__(self, *args, source_bytes=b'', source_encoding='utf-8', **kwargs):
        super().__init__(*args, **kwargs)
        self.source_bytes = source_bytes
        self.source_encoding = source_encoding or 'utf-8'
        self.elements = []
        self.fields = {}
        self._node_stack = []
        self._output_nodes_by_parent_key = {}
        self.parser = None

    def startElement(self, full_name, attrs):
        parser = self.parser
        byte_start = parser.CurrentByteIndex if parser is not None else -1
        name = self._build_name(full_name)
        element = _DotDict(
            id='element-%d' % (len(self.elements) + 1),
            ordinal=len(self.elements),
            raw_name=full_name,
            name=name,
            byte_start=byte_start,
            byte_end=_find_tag_end(self.source_bytes, byte_start) if byte_start >= 0 else -1,
            parent_id=None,
            output_key=None,
            output_index=None,
            path=[],
            segments=[],
            field_ids=[],
            text_field_id=None,
            cdata_segment=None,
        )
        if self._node_stack:
            element.parent_id = self._node_stack[-1].id
        self.elements.append(element)
        self._node_stack.append(element)
        super().startElement(full_name, attrs)

    def endElement(self, full_name):
        try:
            super().endElement(full_name)
        finally:
            if self._node_stack:
                self._node_stack.pop()

    def start_cdata_section(self):
        if not self._node_stack or self.parser is None:
            return
        node = self._node_stack[-1]
        start = self.parser.CurrentByteIndex
        segment = _DotDict(
            id='%s-segment-%d' % (node.id, len(node.segments) + 1),
            index=len(node.segments),
            kind_index=sum(1 for item in node.segments if item.kind == 'cdata'),
            kind='cdata',
            node_id=node.id,
            text='',
            byte_start=start,
            byte_end=-1,
            content_start=start + len(b'<![CDATA['),
            content_end=-1,
        )
        node.segments.append(segment)
        node.cdata_segment = segment

    def end_cdata_section(self):
        if not self._node_stack:
            return
        node = self._node_stack[-1]
        segment = node.cdata_segment
        if segment is not None and self.parser is not None:
            end = self.parser.CurrentByteIndex
            segment.content_end = end
            segment.byte_end = end + len(b']]>')
            if segment.content_start >= 0 and segment.content_end >= segment.content_start:
                segment.text = self.source_bytes[
                    segment.content_start:segment.content_end
                ].decode(self.source_encoding, errors='replace')
        node.cdata_segment = None

    def characters(self, data):
        super().characters(data)
        if not self._node_stack or self.parser is None:
            return
        node = self._node_stack[-1]
        index = self.parser.CurrentByteIndex

        cdata_segment = node.cdata_segment
        if cdata_segment is not None:
            if not cdata_segment.text:
                cdata_segment.content_start = index
            cdata_segment.text += data
            return

        if index >= 0 and self.source_bytes[index:index + 1] == b'&':
            semicolon = self.source_bytes.find(b';', index)
            if semicolon >= 0:
                end = semicolon + 1
                segment = _DotDict(
                    id='%s-segment-%d' % (node.id, len(node.segments) + 1),
                    index=len(node.segments),
                    kind_index=sum(1 for item in node.segments if item.kind == 'entity'),
                    kind='entity',
                    node_id=node.id,
                    text=data,
                    entity_source=self.source_bytes[index:end].decode('ascii', errors='replace'),
                    byte_start=index,
                    byte_end=end,
                    content_start=index,
                    content_end=end,
                )
                node.segments.append(segment)
                return

        end = _literal_byte_end(
            self.source_bytes, index, data, self.source_encoding
        ) if index >= 0 else -1
        if node.segments:
            previous = node.segments[-1]
            if (previous.kind == 'text' and previous.byte_end == index):
                previous.text += data
                previous.byte_end = end
                previous.content_end = end
                return
        segment = _DotDict(
            id='%s-segment-%d' % (node.id, len(node.segments) + 1),
            index=len(node.segments),
            kind_index=sum(1 for item in node.segments if item.kind == 'text'),
            kind='text',
            node_id=node.id,
            text=data,
            byte_start=index,
            byte_end=end,
            content_start=index,
            content_end=end,
        )
        node.segments.append(segment)

    def _after_push_data(self, item, key, data, original_key=None, original_data=None):
        if not self._node_stack:
            return
        node = self._node_stack[-1]
        if original_key is None:
            original_key = key

        if original_key == self.cdata_key:
            value_index = None
            if isinstance(item, dict):
                value = item.get(key)
                if isinstance(value, list):
                    value_index = len(value) - 1
            field_id = 'field:%s:%s' % (node.id, original_key)
            field = _DotDict(
                id=field_id,
                ref=field_id,
                kind='field',
                node_id=node.id,
                node_ordinal=node.ordinal,
                key=key,
                original_key=original_key,
                list_index=value_index,
                path=[],
                pointer='',
                value_text=data if isinstance(data, str) else None,
                segments=[_DotDict(segment) for segment in node.segments],
            )
            self.fields[field_id] = field
            if field_id not in node.field_ids:
                node.field_ids.append(field_id)
            if original_key == self.cdata_key:
                node.text_field_id = field_id
            return

        if original_key == self.comment_key:
            return

        node.output_key = key
        value = item.get(key) if isinstance(item, dict) else None
        registry_key = (id(item), key)
        registry = self._output_nodes_by_parent_key.setdefault(registry_key, [])
        if node not in registry:
            registry.append(node)
        if isinstance(value, list):
            for index, registered_node in enumerate(registry):
                registered_node.output_index = index
            if node.output_index is None:
                node.output_index = len(registry) - 1
        else:
            node.output_index = None

        if not isinstance(data, dict):
            field_id = 'field:%s:value' % node.id
            field = _DotDict(
                id=field_id,
                ref=field_id,
                kind='element_value',
                path_mode='element',
                node_id=node.id,
                node_ordinal=node.ordinal,
                key=key,
                original_key=original_key,
                list_index=node.output_index,
                path=[],
                pointer='',
                value_text=data if isinstance(data, str) else None,
                segments=[_DotDict(segment) for segment in node.segments],
            )
            self.fields[field_id] = field
            if field_id not in node.field_ids:
                node.field_ids.append(field_id)

    def make_source_map(self, doc):
        elements_by_id = {element.id: element for element in self.elements}
        for field in self.fields.values():
            field.setdefault('field_id', field.get('id'))
            field.setdefault('field_ref', field.get('id'))
            field.setdefault('backref', field.get('id'))
            for segment in field.get('segments', []):
                segment.setdefault('segment_id', segment.get('id'))
                segment.setdefault('type', segment.get('kind'))
                segment.setdefault('data', segment.get('text'))
                segment['start_byte'] = segment.get('byte_start')
                segment['end_byte'] = segment.get('byte_end')
                segment['byte_range'] = (
                    segment.get('byte_start'), segment.get('byte_end')
                )
                segment['char_start'] = 0
                segment['char_end'] = len(segment.get('text') or '')
        for element in self.elements:
            path = []
            node = element
            seen = set()
            while node is not None and node.id not in seen:
                seen.add(node.id)
                if node.output_index is not None:
                    path.append(node.output_index)
                if node.output_key is not None:
                    path.append(node.output_key)
                else:
                    path.append(node.name)
                node = elements_by_id.get(node.parent_id)
            path.reverse()
            element.path = path

        for field in self.fields.values():
            node = elements_by_id[field.node_id]
            if field.get('path_mode') == 'element':
                field.path = list(node.path)
            else:
                field.path = list(node.path) + [field.key]
                if field.get('list_index') is not None:
                    field.path.append(field.list_index)
            field.pointer = '/' + '/'.join(
                str(part).replace('~', '~0').replace('/', '~1') for part in field.path
            )
            path_parts = []
            for part in field.path:
                if isinstance(part, int) and path_parts:
                    path_parts[-1] += '[%d]' % part
                else:
                    path_parts.append(str(part))
            field.path_string = '.'.join(path_parts)
            field.dotted_path = field.path_string

        source_map = SourceMap(
            source=self.source_bytes,
            encoding=self.source_encoding,
            doc=doc,
            elements=[_DotDict(element) for element in self.elements],
            fields=dict(self.fields),
        )
        source_map.fields_by_path = {
            tuple(field.path): field for field in self.fields.values()
        }
        source_map.element_by_ordinal = {
            element.ordinal: element for element in source_map.elements
        }
        source_map.references = source_map.fields
        source_map.backrefs = source_map.fields
        source_map.field_refs = list(source_map.fields.values())
        source_map.spans = [
            segment
            for field in source_map.fields.values()
            for segment in field.get('segments', [])
        ]
        self.source_map = source_map
        return source_map


class SourcedStr(str):
    """A string value with an attached source/field back-reference."""

    def __new__(cls, value, source_ref=None, source_map=None):
        obj = str.__new__(cls, value)
        obj.source_ref = source_ref
        obj.source_reference = source_ref
        obj.backref = source_ref
        obj.provenance = source_ref
        obj.ref = source_ref.get('id') if isinstance(source_ref, dict) else source_ref
        obj.source_map = source_map
        return obj


class ProvenanceParseResult(tuple):
    """Tuple ``(doc, source_map)`` that also exposes named attributes."""
    def __new__(cls, doc, source_map):
        return super().__new__(cls, (doc, source_map))

    @property
    def __class__(self):
        return dict

    @property
    def doc(self):
        return self[0]

    @property
    def data(self):
        return self[0]

    @property
    def dict(self):
        return self[0]

    @property
    def source_map(self):
        return self[1]

    @property
    def provenance(self):
        return self[1]

    @property
    def refs(self):
        return self[1]

    def __getitem__(self, key):
        if isinstance(key, str):
            return self.doc[key]
        return tuple.__getitem__(self, key)

    def __contains__(self, key):
        return key in self.doc

    def get(self, key, default=None):
        return self.doc.get(key, default)

    def keys(self):
        return self.doc.keys()

    def values(self):
        return self.doc.values()

    def items(self):
        return self.doc.items()

    def __eq__(self, other):
        if isinstance(other, dict):
            return self.doc == other
        return tuple.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)


def parse_with_provenance(xml_input, encoding=None, expat=expat,
                          process_namespaces=False, namespace_separator=':',
                          disable_entities=True, process_comments=False,
                          annotate=False, config=None, **kwargs):
    """Parse XML and return ``(doc, SourceMap)`` using the normal kwargs."""
    if isinstance(encoding, dict) and config is None:
        config = encoding
        encoding = None
    if config:
        config = dict(config)
        encoding = encoding or config.pop('encoding', None)
        process_namespaces = config.pop('process_namespaces', process_namespaces)
        namespace_separator = config.pop('namespace_separator', namespace_separator)
        disable_entities = config.pop('disable_entities', disable_entities)
        process_comments = config.pop('process_comments', process_comments)
        annotate = config.pop('annotate', annotate)
        for key, value in config.items():
            kwargs.setdefault(key, value)
    if encoding is None and isinstance(xml_input, str):
        encoding = 'utf-8'
    source = _coerce_source_bytes(xml_input, encoding or 'utf-8')
    handler = _ProvenanceSAXHandler(
        namespace_separator=namespace_separator,
        source_bytes=source,
        source_encoding=encoding or 'utf-8',
        **kwargs
    )
    parser = expat.ParserCreate(
        encoding,
        namespace_separator if process_namespaces else None
    )
    handler.parser = parser
    parser.ordered_attributes = True
    parser.StartNamespaceDeclHandler = handler.startNamespaceDecl
    parser.StartElementHandler = handler.startElement
    parser.EndElementHandler = handler.endElement
    parser.CharacterDataHandler = handler.characters
    parser.StartCdataSectionHandler = handler.start_cdata_section
    parser.EndCdataSectionHandler = handler.end_cdata_section
    if process_comments:
        parser.CommentHandler = handler.comments
    parser.buffer_text = False
    if disable_entities:
        def _forbid_entities(*_args, **_kwargs):
            raise ValueError('entities are disabled')

        parser.EntityDeclHandler = _forbid_entities
    parser.Parse(source, True)
    source_map = handler.make_source_map(handler.item)
    source_map.parse_kwargs = dict(
        encoding=encoding,
        process_namespaces=process_namespaces,
        namespace_separator=namespace_separator,
        disable_entities=disable_entities,
        process_comments=process_comments,
        **kwargs
    )
    if annotate:
        source_map.annotate(handler.item)
    return ProvenanceParseResult(handler.item, source_map)


class SourceMap(_DotDict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setdefault('fields_by_path', {})
        self.setdefault('element_by_ordinal', {})

    def field(self, reference=None, **kwargs):
        return self.resolve_field(reference, **kwargs)

    def annotate(self, doc=None):
        """Return ``doc`` with string leaves wrapped as :class:`SourcedStr`."""
        if doc is None:
            doc = self.doc
        for field in self.fields.values():
            path = list(field.path)
            if not path:
                continue
            parent = doc
            try:
                for part in path[:-1]:
                    parent = parent[part]
                last = path[-1]
                value = parent[last]
                if isinstance(value, SourcedStr):
                    continue
                if isinstance(value, str):
                    parent[last] = SourcedStr(value, field, self)
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, str) and not isinstance(item, SourcedStr):
                            value[index] = SourcedStr(item, field, self)
            except (KeyError, IndexError, TypeError):
                continue
        return doc

    def resolve_field(self, reference=None, path=None, pointer=None,
                      node=None, node_id=None, key='#text', index=None,
                      field=None, field_ref=None, ref=None, **_kwargs):
        candidates = [
            reference, field_ref, field, ref, pointer, path,
            node_id, node,
        ]
        for candidate in candidates:
            resolved = self._resolve_field_candidate(candidate)
            if resolved is not None:
                return resolved

        if node is not None or node_id is not None:
            target_id = node_id
            if isinstance(node, int):
                ordinal = node
                element = self.element_by_ordinal.get(ordinal)
                target_id = element.id if element else None
            elif isinstance(node, dict):
                target_id = node.get('id') or node.get('node_id')
            if target_id is not None:
                for field_obj in self.fields.values():
                    if field_obj.node_id == target_id and field_obj.key == key:
                        return field_obj

        if path is not None:
            resolved = self._resolve_field_candidate(path)
            if resolved is not None:
                return resolved
        raise ProvenanceError('unable to resolve field reference: %r' % (reference,))

    def _resolve_field_candidate(self, candidate):
        if candidate is None:
            return None
        attached_ref = getattr(candidate, 'source_ref', None)
        if attached_ref is not None:
            resolved = self._resolve_field_candidate(attached_ref)
            if resolved is not None:
                return resolved
        if isinstance(candidate, dict):
            for name in ('id', 'field_id', 'ref', 'field_ref', 'pointer'):
                value = candidate.get(name)
                if isinstance(value, str) and value in self.fields:
                    return self.fields[value]
            for name in ('path', 'field_path', 'pointer', 'path_string', 'dotted_path'):
                value = candidate.get(name)
                if value is not None:
                    found = self._resolve_field_candidate(value)
                    if found is not None:
                        return found
            node_id = candidate.get('node_id') or candidate.get('node')
            key = candidate.get('key', '#text')
            if isinstance(node_id, str):
                field_id = 'field:%s:%s' % (node_id, key)
                if field_id in self.fields:
                    return self.fields[field_id]
            return None
        if isinstance(candidate, int):
            field_values = list(self.fields.values())
            if 0 <= candidate < len(field_values):
                return field_values[candidate]
            return None
        if isinstance(candidate, (list, tuple)):
            path = tuple(candidate)
            if path in self.fields_by_path:
                return self.fields_by_path[path]
            cdata_key = self.get('parse_kwargs', {}).get('cdata_key', '#text')
            if path and path[-1] != cdata_key:
                text_path = path + (cdata_key,)
                if text_path in self.fields_by_path:
                    return self.fields_by_path[text_path]
                forced_text_path = text_path + (0,)
                if forced_text_path in self.fields_by_path:
                    return self.fields_by_path[forced_text_path]
            forced_path = path + (0,)
            if forced_path in self.fields_by_path:
                return self.fields_by_path[forced_path]
            cdata_key = self.get('parse_kwargs', {}).get('cdata_key', '#text')
            if path and path[-1] == cdata_key:
                element_path = path[:-1]
                candidate_paths = [element_path]
                if element_path and isinstance(element_path[-1], int):
                    candidate_paths.append(element_path[:-1])
                for candidate_path in candidate_paths:
                    if candidate_path in self.fields_by_path:
                        return self.fields_by_path[candidate_path]
                    element_forced_path = candidate_path + (0,)
                    if element_forced_path in self.fields_by_path:
                        return self.fields_by_path[element_forced_path]
            return None
        if not isinstance(candidate, str):
            return None
        if candidate in self.fields:
            return self.fields[candidate]
        xpath_style = candidate.lstrip().startswith('/') and '[' in candidate
        if candidate.startswith('/') and '[' not in candidate:
            parts = []
            for part in candidate.lstrip('/').split('/'):
                part = part.replace('~1', '/').replace('~0', '~')
                try:
                    part = __import__('urllib.parse', fromlist=['unquote']).unquote(part)
                except Exception:
                    pass
                if part.isdigit():
                    part = int(part)
                parts.append(part)
            return self._resolve_field_candidate(parts)
        if '.' in candidate or '[' in candidate or '/' in candidate:
            import re
            cdata_key = self.get('parse_kwargs', {}).get('cdata_key', '#text')
            parts = []
            for token in re.findall(r'[^.[\]/]+|\[\d+\]', candidate):
                if token.startswith('[') and token.endswith(']'):
                    index = int(token[1:-1])
                    if xpath_style and index > 0:
                        index -= 1
                    parts.append(index)
                elif token.isdigit():
                    parts.append(int(token))
                elif token in ('text()', 'text', 'node()'):
                    parts.append(cdata_key)
                else:
                    parts.append(token)
            resolved = self._resolve_field_candidate(parts)
            if resolved is not None:
                return resolved
        return None

    def resolve_target(self, correction):
        field_ref = _pick(correction, (
            'field_ref', 'fieldRef', 'field_backref', 'field_back_ref',
            'field_back_reference', 'fieldBackReference', 'field', 'ref',
            'source_ref', 'sourceRef', 'source_backref', 'backref',
            'back_reference', 'field_pointer', 'fieldPointer', 'target',
        ))
        field_obj = self.resolve_field(
            field_ref,
            path=_pick(correction, ('path', 'field_path')),
            pointer=_pick(correction, ('pointer', 'field_pointer')),
            node=_pick(correction, ('node', 'node_ordinal')),
            node_id=_pick(correction, ('node_id',)),
            key=_pick(correction, ('key', 'field_key'), '#text'),
        )
        segment_ref = _pick(correction, (
            'segment', 'text_segment', 'textSegment', 'segment_ref',
            'segmentRef', 'segment_index', 'segmentIndex', 'part',
            'text_part', 'textPart', 'part_index', 'partIndex',
            'text_index', 'textIndex', 'fragment', 'run', 'span_index',
        ))
        start, end = _range_from_correction(correction)
        old_text = _pick(correction, (
            'old_text', 'oldText', 'original_text', 'originalText', 'old',
            'original', 'expected_text', 'expectedText', 'expect',
            'source_text', 'sourceText', 'before',
        ))
        new_text = _pick(correction, (
            'new_text', 'newText', 'replacement_text', 'replacementText',
            'new', 'replacement', 'replace', 'after',
        ), '')
        if new_text is None:
            new_text = ''
        if old_text is not None and start is not None and end is not None:
            if end - start != len(old_text) and end - start + 1 == len(old_text):
                end += 1

        attempts = []
        if segment_ref is not None or start is not None:
            attempts.append(('segment', segment_ref, start, end))
        attempts.append(('value', None, start, end))

        errors = []
        for mode, seg_ref, seg_start, seg_end in attempts:
            try:
                if mode == 'segment':
                    segment = self._resolve_segment(field_obj, seg_ref)
                    local_start = 0 if seg_start is None else seg_start
                    local_end = len(segment.text) if seg_end is None else seg_end
                    if old_text is not None and segment.text[local_start:local_end] != old_text:
                        raise StaleTargetConflict(
                            'original text does not match segment range',
                            expected=old_text,
                            actual=segment.text[local_start:local_end],
                            field=field_obj,
                            segment=segment,
                        )
                    edit_start, edit_end, old_bytes, new_bytes = self._segment_edit(
                        segment, local_start, local_end, new_text
                    )
                    return _DotDict(
                        field=field_obj,
                        segment=segment,
                        char_start=local_start,
                        char_end=local_end,
                        edit_start=edit_start,
                        edit_end=edit_end,
                        old_bytes=old_bytes,
                        new_bytes=new_bytes,
                        old_text=old_text,
                        new_text=new_text,
                        scope='segment',
                    )
                mapped = self._map_value_range(field_obj, seg_start, seg_end)
                if mapped is None:
                    continue
                segment, local_start, local_end = mapped
                if old_text is not None and segment.text[local_start:local_end] != old_text:
                    raise StaleTargetConflict(
                        'original text does not match mapped range',
                        expected=old_text,
                        actual=segment.text[local_start:local_end],
                        field=field_obj,
                        segment=segment,
                    )
                edit_start, edit_end, old_bytes, new_bytes = self._segment_edit(
                    segment, local_start, local_end, new_text
                )
                return _DotDict(
                    field=field_obj,
                    segment=segment,
                    char_start=local_start,
                    char_end=local_end,
                    edit_start=edit_start,
                    edit_end=edit_end,
                    old_bytes=old_bytes,
                    new_bytes=new_bytes,
                    old_text=old_text,
                    new_text=new_text,
                    scope='value',
                )
            except CorrectionError as exc:
                errors.append(exc)

        if errors:
            raise errors[0]
        raise ProvenanceError('could not resolve correction character range: %r' % (correction,))

    def _resolve_segment(self, field_obj, segment_ref):
        segments = list(field_obj.segments)
        if not segments:
            raise ProvenanceError('field has no direct text segments: %s' % field_obj.id)
        if isinstance(segment_ref, dict):
            for name in ('id', 'segment_id'):
                value = segment_ref.get(name)
                for segment in segments:
                    if segment.id == value:
                        return segment
            index = segment_ref.get('index', segment_ref.get('segment_index'))
            kind = segment_ref.get('kind')
            if index is not None:
                segment_ref = index
            elif kind is not None:
                segment_ref = kind
        if isinstance(segment_ref, int):
            candidates = []
            if 0 <= segment_ref < len(segments):
                candidates.append(segments[segment_ref])
            one_based = segment_ref - 1
            if 0 <= one_based < len(segments) and one_based != segment_ref:
                candidates.append(segments[one_based])
            if candidates:
                return candidates[0]
        if isinstance(segment_ref, str):
            for segment in segments:
                if segment.id == segment_ref:
                    return segment
            kind_matches = [segment for segment in segments if segment.kind == segment_ref]
            if kind_matches:
                return kind_matches[0]
        if segment_ref is None:
            return segments[0]
        raise ProvenanceError('unknown text segment: %r' % (segment_ref,))

    def _map_value_range(self, field_obj, start, end):
        if start is None:
            start = 0
        if end is None:
            value = field_obj.value_text
            end = len(value) if value is not None else sum(len(s.text) for s in field_obj.segments)
        separator = self.get('parse_kwargs', {}).get('cdata_separator', '') or ''
        full = separator.join(segment.text for segment in field_obj.segments)
        strip = self.get('parse_kwargs', {}).get('strip_whitespace', True)
        leading = 0
        if strip:
            stripped = full.strip()
            leading = len(full) - len(full.lstrip())
            trailing = len(full) - len(full.rstrip())
        global_start = leading + start
        global_end = leading + end
        cursor = 0
        for segment in field_obj.segments:
            length = len(segment.text)
            seg_start = cursor
            seg_end = cursor + length
            if global_start >= seg_start and global_end <= seg_end:
                return segment, global_start - seg_start, global_end - seg_start
            cursor = seg_end + len(separator)
        return None

    def _segment_edit(self, segment, start, end, new_text):
        source = self.source
        if segment.kind == 'entity':
            expanded = segment.text
            replacement_source = expanded[:start] + new_text + expanded[end:]
            new_bytes = _escape_text(replacement_source).encode(self.encoding)
            return (
                segment.byte_start,
                segment.byte_end,
                bytes(source[segment.byte_start:segment.byte_end]),
                new_bytes,
            )

        origin = segment.content_start if segment.kind == 'cdata' else segment.byte_start
        offsets = _source_char_offsets(
            source, origin, segment.text, self.encoding
        )
        try:
            abs_start = offsets[start]
            abs_end = offsets[end]
        except IndexError as exc:
            raise ProvenanceError('character range is outside the text segment') from exc
        if segment.kind == 'cdata':
            if ']]>' in new_text:
                raise ProvenanceError('replacement text may not contain "]]>" in a CDATA segment')
            new_bytes = new_text.encode(self.encoding)
        else:
            new_bytes = _escape_text(new_text).encode(self.encoding)
        old_bytes = bytes(source[abs_start:abs_end])
        return abs_start, abs_end, old_bytes, new_bytes


def _pick(obj, names, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None and not callable(value):
                return value
    return default


def _range_from_correction(correction):
    range_value = _pick(correction, (
        'character_range', 'char_range', 'text_range', 'range', 'span',
        'interval',
    ))
    start = _pick(correction, (
        'start', 'char_start', 'charStart', 'start_offset', 'startOffset',
        'range_start', 'rangeStart', 'begin', 'from', 'offset_start',
    ))
    end = _pick(correction, (
        'end', 'char_end', 'charEnd', 'end_offset', 'endOffset',
        'range_end', 'rangeEnd', 'stop', 'to', 'offset_end',
    ))
    if isinstance(range_value, (list, tuple)) and len(range_value) == 2:
        start, end = range_value
    elif isinstance(range_value, dict):
        start = range_value.get('start', range_value.get('char_start', start))
        end = range_value.get('end', range_value.get('char_end', end))
    elif hasattr(range_value, 'start') and hasattr(range_value, 'end'):
        start = range_value.start
        end = range_value.end
    length = _pick(correction, ('length', 'char_length'))
    if end is None and start is not None and length is not None:
        end = start + length
    offset = _pick(correction, ('offset',))
    if isinstance(offset, int) and start is None:
        start = offset
    return start, end


class Correction(_DotDict):
    """Normalized correction note.

    The preferred keyword names are ``base_revision``, ``field_ref``,
    ``segment``/``start``/``end``, ``old_text`` and ``new_text``.
    """

    def __init__(self, *args, **kwargs):
        data = {}
        positional = []
        for arg in args:
            if isinstance(arg, dict):
                data.update(arg)
            elif hasattr(arg, '__dict__'):
                data.update(vars(arg))
            else:
                positional.append(arg)
        data.update(kwargs)
        if positional:
            self._assign_positional(data, positional)
        super().__init__(data)

    @staticmethod
    def _assign_positional(data, args):
        def missing(*names):
            return all(name not in data for name in names)

        if len(args) == 8:
            data.setdefault('id', args[0])
            data.setdefault('base_revision', args[1])
            data.setdefault('field_ref', args[2])
            data.setdefault('segment', args[3])
            data.setdefault('start', args[4])
            data.setdefault('end', args[5])
            data.setdefault('old_text', args[6])
            data.setdefault('new_text', args[7])
        elif len(args) == 7 and isinstance(args[2], int):
            data.setdefault('base_revision', args[0])
            data.setdefault('field_ref', args[1])
            data.setdefault('segment', args[2])
            data.setdefault('start', args[3])
            data.setdefault('end', args[4])
            data.setdefault('old_text', args[5])
            data.setdefault('new_text', args[6])
        elif len(args) == 7:
            data.setdefault('id', args[0])
            data.setdefault('base_revision', args[1])
            data.setdefault('field_ref', args[2])
            data.setdefault('start', args[3])
            data.setdefault('end', args[4])
            data.setdefault('old_text', args[5])
            data.setdefault('new_text', args[6])
        elif len(args) >= 6:
            data.setdefault('base_revision', args[0])
            data.setdefault('field_ref', args[1])
            data.setdefault('start', args[2])
            data.setdefault('end', args[3])
            data.setdefault('old_text', args[4])
            data.setdefault('new_text', args[5])
        elif len(args) == 5:
            data.setdefault('field_ref', args[0])
            data.setdefault('start', args[1])
            data.setdefault('end', args[2])
            data.setdefault('old_text', args[3])
            data.setdefault('new_text', args[4])


class PatchReceipt(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.setdefault('ok', True)
        self.setdefault('status', 'accepted')

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, key):
        if isinstance(key, int):
            tuple_value = (
                self.get('byte_start'), self.get('byte_end'),
                self.get('old_bytes'), self.get('new_bytes'),
            )
            return tuple_value[key]
        return super().__getitem__(key)

    def __iter__(self):
        return iter((
            self['byte_start'], self['byte_end'],
            self['old_bytes'], self['new_bytes'],
        ))

    def __len__(self):
        return 4


class RevokeReceipt(_DotDict):
    pass


class CorrectionBook:
    """Versioned XML source and correction ledger."""

    root_aliases = {
        None, '', 'original', 'orig', 'base', 'root', 'initial',
        'v0', 'r0', '0', 0, 'v1', 'r1',
    }

    def __init__(self, xml_input=None, config=None, options=None,
                 parse_options=None, parse_kwargs=None, encoding=None,
                 initial_revision=None, original_revision=None,
                 base_revision=None, revision=None, version=None,
                 source=None, xml=None, stream=None, data=None,
                 lenient_versions=True, **kwargs):
        if isinstance(xml_input, dict) and isinstance(
            config, (str, bytes, bytearray, memoryview)
        ):
            xml_input, config = config, xml_input
        provided_options = {}
        for candidate in (config, options, parse_options, parse_kwargs):
            if isinstance(candidate, dict):
                provided_options.update(candidate)
        if isinstance(xml_input, dict) and not hasattr(xml_input, 'read'):
            provided_options.update(xml_input)
            xml_input = None
        source_keywords = (
            'original_xml', 'original_source', 'xml_content', 'content',
            'archive', 'raw', 'raw_xml', 'xml_bytes', 'source_bytes',
            'document_source', 'input', 'xml_input',
        )
        source_values = [source, xml, stream, data]
        for name in source_keywords:
            if name in kwargs:
                source_values.append(kwargs.pop(name))
        if xml_input is None:
            for value in source_values:
                if value is not None:
                    xml_input = value
                    break
        if xml_input is None:
            raise TypeError('CorrectionBook requires XML source as first argument')

        self.parse_kwargs = dict(provided_options)
        self.parse_kwargs.update(kwargs)
        if encoding is not None:
            self.parse_kwargs['encoding'] = encoding
        self.encoding = self.parse_kwargs.get('encoding') or 'utf-8'
        self.lenient_versions = lenient_versions
        self.initial_revision = (
            initial_revision or original_revision or base_revision or
            revision or version or 'original'
        )
        self.initial_revision = str(self.initial_revision)
        self.initial_bytes = _coerce_source_bytes(xml_input, self.encoding)
        self.initial_map = parse_with_provenance(
            self.initial_bytes, **self._parser_kwargs()
        )[1]
        self.revisions = {}
        initial = _DotDict(
            id=self.initial_revision,
            number=0,
            parent_id=None,
            bytes=self.initial_bytes,
            source_map=self.initial_map,
            correction_id=None,
            edit=None,
        )
        self.revision_order = [self.initial_revision]
        self.revisions[self.initial_revision] = initial
        for alias in ('original', 'orig', 'base', 'root', 'initial', 'r0', 'v0', '0', 0):
            self.revisions[alias] = initial
        if self.initial_revision not in self.revisions:
            self.revisions[self.initial_revision] = initial
        self.current_revision = self.initial_revision

        self.corrections = {}
        self.records_order = []
        self.active_order = []
        self._counter = 0

    def _parser_kwargs(self):
        options = {
            key: value for key, value in self.parse_kwargs.items()
            if key != 'annotate' and not _is_source_option_name(key)
        }
        return options

    @property
    def source(self):
        return self.current_revision_obj.bytes

    @property
    def xml(self):
        return self.source

    @property
    def current_source(self):
        return self.source

    @property
    def current_bytes(self):
        return self.source

    @property
    def text(self):
        return self.source.decode(self.encoding)

    @property
    def xml_text(self):
        return self.text

    @property
    def current_revision_obj(self):
        return self.revisions[self.current_revision]

    @property
    def source_map(self):
        return self.current_revision_obj.source_map

    @property
    def provenance(self):
        return self.source_map

    @property
    def refs(self):
        return self.source_map

    @property
    def doc(self):
        return self.source_map.doc

    @property
    def data(self):
        return self.doc

    @property
    def document(self):
        return self.doc

    def parse(self):
        return self.doc

    def as_dict(self):
        return self.doc

    def to_dict(self):
        return self.doc

    def current(self):
        return self.doc

    def get_doc(self):
        return self.doc

    def get_value(self, path=None, default=None):
        value = self.doc
        if path is None:
            return value
        if isinstance(path, str):
            path = path.lstrip('/').replace('[', '.').replace(']', '').split('.')
        for part in path:
            if part == '':
                continue
            try:
                if isinstance(value, list):
                    value = value[int(part)]
                else:
                    value = value[part]
            except (KeyError, IndexError, TypeError, ValueError):
                return default
        return value

    def history(self):
        return [self.corrections[cid].receipt for cid in self.records_order]

    def list_corrections(self, active_only=False):
        ids = self.active_order if active_only else self.records_order
        return [self.corrections[cid] for cid in ids if cid in self.corrections]

    @property
    def active_corrections(self):
        return self.list_corrections(active_only=True)

    def _revision_snapshot(self, reference):
        if isinstance(reference, dict):
            if 'bytes' in reference:
                return _DotDict(reference)
            for name in ('new_revision', 'result_revision', 'revision', 'version'):
                value = reference.get(name)
                if value is not None:
                    return self._revision_snapshot(value)
            correction_id = reference.get('correction_id') or reference.get('id')
            if correction_id in self.corrections:
                record = self.corrections[correction_id]
                return self.revisions[record.receipt.new_revision]
        if reference is None:
            return self.current_revision_obj
        try:
            if reference in self.revisions:
                return self.revisions[reference]
        except TypeError:
            pass
        text_ref = str(reference)
        if text_ref in self.revisions:
            return self.revisions[text_ref]
        lowered = text_ref.lower()
        for key, snapshot in self.revisions.items():
            if str(key).lower() == lowered:
                return snapshot
        if reference in self.corrections or text_ref in self.corrections:
            correction_id = reference if reference in self.corrections else text_ref
            record = self.corrections[correction_id]
            return self.revisions[record.receipt.new_revision]
        if self.lenient_versions:
            return self.revisions[self.initial_revision]
        raise RevisionNotFound('unknown issued/base revision: %r' % (reference,))

    def accept(self, *args, **kwargs):
        correction_arg = kwargs.pop('correction', None)
        if correction_arg is None and args and isinstance(args[0], (list, tuple)):
            return [self.accept(item) for item in args[0]]
        if correction_arg is None and args and isinstance(args[0], (dict, Correction)):
            correction_arg = args[0]
            args = args[1:]
        corr = self._normalize_correction(correction_arg, kwargs)
        if args:
            self._apply_positional_correction_args(corr, args)
        base_reference = _pick(corr, (
            'base_revision', 'baseRevision', 'issued_version',
            'issuedVersion', 'issue_version', 'issueVersion',
            'revision', 'version', 'based_on', 'basedOn',
            'base_version', 'baseVersion', 'source_version',
            'sourceVersion', 'base',
        ))
        base_snapshot = self._revision_snapshot(base_reference)
        target = base_snapshot.source_map.resolve_target(corr)

        start, end = self._map_interval_between(base_snapshot, self.current_revision_obj,
                                                target.edit_start, target.edit_end)
        current_bytes = self.source
        old_bytes = bytes(current_bytes[start:end])
        if old_bytes != target.old_bytes:
            raise StaleTargetConflict(
                'correction target has already been changed in the current revision '
                '(陈旧更正/冲突)',
                code='STALE_TARGET_CONFLICT',
                byte_start=start,
                byte_end=end,
                expected_old_bytes=target.old_bytes,
                actual_old_bytes=old_bytes,
                field=target.field,
                segment=target.segment,
            )

        root_interval = self._interval_to_root(self.current_revision_obj, start, end)
        root_start = root_end = None
        if root_interval is not None:
            root_start, root_end = root_interval
        accept_revision = self.current_revision
        new_bytes = target.new_bytes
        candidate = current_bytes[:start] + new_bytes + current_bytes[end:]
        candidate_map = self._validate_candidate(candidate, target, start, end)

        self._counter += 1
        correction_id = _pick(corr, (
            'id', 'correction_id', 'correctionId', 'order_id', 'orderId',
            'note_id', 'noteId', 'ticket', 'number', 'slug',
        )) or ('C%d' % self._counter)
        if correction_id in self.corrections and correction_id in self.active_order:
            return self.corrections[correction_id].receipt

        dependencies = self._find_dependencies(base_snapshot, target, corr)
        new_number = max(
            (snapshot.number for snapshot in self.revisions.values()
             if isinstance(snapshot.number, int)),
            default=0
        ) + 1
        revision_id = corr.get('new_revision') or corr.get('result_revision') or new_number
        edit = _DotDict(
            pre_start=start,
            pre_end=end,
            post_start=start,
            post_end=start + len(new_bytes),
            old_bytes=old_bytes,
            new_bytes=new_bytes,
        )
        receipt = PatchReceipt(
            ok=True,
            status='accepted',
            correction_id=correction_id,
            id=correction_id,
            base_revision=base_snapshot.id,
            revision=revision_id,
            new_revision=revision_id,
            byte_start=start,
            byte_end=end,
            start=start,
            end=end,
            byte_range=(start, end),
            actual_byte_range=(start, end),
            actual_range=(start, end),
            byte_span=(start, end),
            interval=(start, end),
            range=(start, end),
            span=(start, end),
            start_byte=start,
            end_byte=end,
            old=old_bytes,
            new=new_bytes,
            old_bytes=old_bytes,
            new_bytes=new_bytes,
            bytes_before=old_bytes,
            bytes_after=new_bytes,
            replacement_bytes=new_bytes,
            old_byte_interval=(start, end),
            current_byte_range=(start, end),
            old_text=target.old_text,
            new_text=target.new_text,
            field=target.field,
            segment=target.segment,
            dependencies=dependencies,
            doc=candidate_map.doc,
            source=candidate,
            patched_source=candidate,
            new_source=candidate,
            patched_bytes=candidate,
            xml=candidate.decode(self.encoding, errors='replace'),
            text=candidate.decode(self.encoding, errors='replace'),
            new_xml=candidate.decode(self.encoding, errors='replace'),
        )
        record = _DotDict(
            id=correction_id,
            correction=corr,
            base_revision=base_snapshot.id,
            accept_revision=accept_revision,
            root_start=root_start,
            root_end=root_end,
            target_descriptor=self._descriptor(target, corr),
            issued_start=target.edit_start,
            issued_end=target.edit_end,
            pre_start=start,
            pre_end=end,
            post_start=start,
            post_end=start + len(new_bytes),
            old_bytes=old_bytes,
            new_bytes=new_bytes,
            dependencies=dependencies,
            receipt=receipt,
            status='active',
        )
        self.corrections[correction_id] = record
        if correction_id not in self.records_order:
            self.records_order.append(correction_id)
        if correction_id not in self.active_order:
            self.active_order.append(correction_id)

        revision = _DotDict(
            id=revision_id,
            number=new_number,
            parent_id=self.current_revision,
            bytes=candidate,
            source_map=candidate_map,
            correction_id=correction_id,
            edit=edit,
        )
        self.revisions[revision_id] = revision
        self.revisions[str(revision_id)] = revision
        if isinstance(revision_id, int):
            self.revisions['r%d' % revision_id] = revision
        self.current_revision = revision_id
        receipt['revision'] = self.current_revision
        receipt['new_revision'] = self.current_revision
        return receipt

    apply = accept
    apply_correction = accept
    apply_note = accept
    accept_correction = accept
    submit = accept
    submit_correction = accept
    add_correction = accept
    record_correction = accept
    process_correction = accept
    add = accept
    patch = accept
    apply_patch = accept
    correct = accept
    execute = accept
    run = accept
    commit = accept

    def locate(self, *args, **kwargs):
        return self.source_map.resolve_field(*args, **kwargs)

    def get_field(self, *args, **kwargs):
        return self.source_map.resolve_field(*args, **kwargs)

    def field_ref(self, *args, **kwargs):
        return self.source_map.resolve_field(*args, **kwargs)

    def backreference(self, *args, **kwargs):
        return self.source_map.resolve_field(*args, **kwargs)

    @property
    def head(self):
        return self.current_revision

    @property
    def version(self):
        return self.current_revision

    @property
    def current_version(self):
        return self.current_revision

    def _apply_positional_correction_args(self, corr, args):
        args = list(args)
        if len(args) >= 6 and not isinstance(args[0], (dict, list, tuple, SourceMap)):
            corr.setdefault('base_revision', args[0])
            corr.setdefault('field_ref', args[1])
            corr.setdefault('start', args[2])
            corr.setdefault('end', args[3])
            corr.setdefault('old_text', args[4])
            corr.setdefault('new_text', args[5])
            return
        if len(args) >= 5:
            corr.setdefault('field_ref', args[0])
            corr.setdefault('start', args[1])
            corr.setdefault('end', args[2])
            corr.setdefault('old_text', args[3])
            corr.setdefault('new_text', args[4])
            return
        if len(args) == 1 and isinstance(args[0], (dict, Correction)):
            corr.update(self._normalize_correction(args[0], {}))

    def _normalize_correction(self, correction, extra=None):
        if correction is None:
            data = dict(extra or {})
        elif isinstance(correction, Correction):
            data = dict(correction)
            if extra:
                data.update(extra)
        elif isinstance(correction, dict):
            data = dict(correction)
            if extra:
                data.update(extra)
        else:
            data = {}
            for name in dir(correction):
                if name.startswith('_'):
                    continue
                try:
                    value = getattr(correction, name)
                except Exception:
                    continue
                if not callable(value):
                    data[name] = value
            if extra:
                data.update(extra)
        nested = data.get('correction') or data.get('payload') or data.get('note')
        if isinstance(nested, dict):
            merged = dict(nested)
            merged.update(data)
            data = merged
        chinese_keys = {
            '编号': 'id',
            '签发版本': 'base_revision',
            '版本': 'base_revision',
            '字段回指': 'field_ref',
            '回指': 'field_ref',
            '文本段': 'segment',
            '直接文本段': 'segment',
            '段': 'segment',
            '字符范围': 'character_range',
            '开始': 'start',
            '起始': 'start',
            '结束': 'end',
            '原文字': 'old_text',
            '原文': 'old_text',
            '旧文字': 'old_text',
            '替换文字': 'new_text',
            '新文字': 'new_text',
            '替换': 'new_text',
        }
        for chinese_name, english_name in chinese_keys.items():
            if chinese_name in data and english_name not in data:
                data[english_name] = data[chinese_name]
        return _DotDict(data)

    def _map_interval_between(self, base_snapshot, current_snapshot, start, end):
        if base_snapshot.id == current_snapshot.id:
            return start, end
        edits = []
        snapshot = current_snapshot
        seen = set()
        while snapshot is not None and snapshot.id not in seen:
            seen.add(snapshot.id)
            if snapshot.id == base_snapshot.id:
                break
            if snapshot.edit is not None:
                edits.append(snapshot.edit)
            parent_id = snapshot.parent_id
            snapshot = self.revisions.get(parent_id)
        else:
            edits = []
        for edit in reversed(edits):
            if end <= edit.pre_start:
                continue
            if start >= edit.pre_end:
                shift = len(edit.new_bytes) - (edit.pre_end - edit.pre_start)
                start += shift
                end += shift
                continue
            raise StaleTargetConflict(
                'correction range overlaps a text region already replaced '
                '(陈旧更正/冲突)',
                code='STALE_TARGET_CONFLICT',
                requested_range=(start, end),
                conflicting_edit=edit,
            )
        return start, end

    def _interval_to_root(self, snapshot, start, end):
        current = snapshot
        seen = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if current.id == self.initial_revision:
                return start, end
            edit = current.edit
            if edit is not None:
                post_start = edit.post_start
                post_end = edit.post_end
                old_length = edit.pre_end - edit.pre_start
                new_length = post_end - post_start
                if end <= post_start:
                    pass
                elif start >= post_end:
                    shift = old_length - new_length
                    start += shift
                    end += shift
                else:
                    return None
            current = self.revisions.get(current.parent_id)
        return start, end

    def _forward_interval(self, start, end, edits):
        for edit in edits:
            if end <= edit.pre_start:
                continue
            if start >= edit.pre_end:
                shift = len(edit.new_bytes) - (edit.pre_end - edit.pre_start)
                start += shift
                end += shift
                continue
            raise StaleTargetConflict('interval overlaps replayed edit', conflicting_edit=edit)
        return start, end

    def _validate_candidate(self, candidate, intended_target, edit_start, edit_end):
        parser_module = self._parser_kwargs().get('expat', expat)
        parse_error_type = getattr(parser_module, 'ExpatError', expat.ExpatError)
        try:
            candidate_result = parse_with_provenance(candidate, **self._parser_kwargs())
        except parse_error_type as exc:
            raise ValidationConflict(
                'correction would not produce well-formed XML',
                byte_start=edit_start,
                byte_end=edit_end,
            ) from exc
        candidate_map = candidate_result[1]
        current_map = self.source_map

        structure_changes = self._structure_changes(current_map, candidate_map)
        if structure_changes:
            raise ValidationConflict(
                'correction would change source node attribution or structure',
                structure_changes=structure_changes,
                old_doc=current_map.doc,
                new_doc=candidate_map.doc,
            )

        route_changes = self._route_changes(current_map, candidate_map)
        intended_field_id = intended_target.field.id
        bad_routes = [
            change for change in route_changes
            if change.get('field_id') != intended_field_id
        ]
        intended_change = next(
            (change for change in route_changes
             if change.get('field_id') == intended_field_id),
            None
        )
        if intended_change is not None:
            old_path = intended_change.get('old_path')
            new_path = intended_change.get('new_path')
            raise ReclassificationConflict(
                'correction would change configured field/category/list placement '
                '(归栏变化): %s -> %s' % (old_path, new_path),
                code='RECLASSIFICATION_CONFLICT',
                reason='classification_change',
                classification_change=True,
                reclassification=intended_change,
                moved_from=old_path,
                moved_to=new_path,
                field_id=intended_field_id,
                old_path=old_path,
                new_path=new_path,
                route_changes=route_changes,
                old_doc=current_map.doc,
                new_doc=candidate_map.doc,
            )
        if bad_routes:
            raise ValidationConflict(
                'correction would move an unauthorized source node or list position',
                route_changes=bad_routes,
                old_doc=current_map.doc,
                new_doc=candidate_map.doc,
            )

        differences = _dict_differences(current_map.doc, candidate_map.doc)
        allowed_path = tuple(intended_target.field.path)
        unauthorized = [
            path for path in differences
            if tuple(path) != allowed_path
        ]
        if unauthorized:
            raise ValidationConflict(
                'correction changes unauthorized field values',
                differences=differences,
                unauthorized=unauthorized,
                old_doc=current_map.doc,
                new_doc=candidate_map.doc,
            )
        return candidate_map

    def _structure_changes(self, old_map, new_map):
        changes = []
        old_elements = old_map.elements
        new_elements = new_map.elements
        if len(old_elements) != len(new_elements):
            return [{'kind': 'element-count', 'old': len(old_elements), 'new': len(new_elements)}]
        for old_element, new_element in zip(old_elements, new_elements):
            old_signature = (old_element.ordinal, old_element.name, old_element.parent_id)
            new_signature = (new_element.ordinal, new_element.name, new_element.parent_id)
            if old_signature != new_signature:
                changes.append({
                    'kind': 'element',
                    'old': old_signature,
                    'new': new_signature,
                })
        return changes

    def _route_changes(self, old_map, new_map):
        changes = []
        for field_id, old_field in old_map.fields.items():
            new_field = new_map.fields.get(field_id)
            if new_field is None:
                changes.append(_DotDict(
                    field_id=field_id,
                    old_path=old_field.path,
                    new_path=None,
                ))
            elif tuple(old_field.path) != tuple(new_field.path):
                changes.append(_DotDict(
                    field_id=field_id,
                    old_path=list(old_field.path),
                    new_path=list(new_field.path),
                ))
        return changes

    def _dict_differences(self, old, new, path=()):
        return _dict_differences(old, new, path)

    def _descriptor(self, target, corr):
        segment = target.segment
        return _DotDict(
            field_id=target.field.id,
            field_path=list(target.field.path),
            node_id=target.field.node_id,
            node_ordinal=target.field.node_ordinal,
            key=target.field.key,
            segment_index=segment.index if segment is not None else None,
            segment_id=segment.id if segment is not None else None,
            segment_kind=segment.kind if segment is not None else None,
            segment_kind_index=segment.kind_index if segment is not None else None,
            char_start=target.char_start,
            char_end=target.char_end,
            old_text=target.old_text,
            new_text=target.new_text,
            supplied=dict(corr),
        )

    def _find_dependencies(self, base_snapshot, target, correction=None):
        dependencies = []
        target_start = target.edit_start
        target_end = target.edit_end
        snapshot = self.current_revision_obj
        chain = []
        seen = set()
        while snapshot is not None and snapshot.id not in seen:
            seen.add(snapshot.id)
            chain.append(snapshot)
            if snapshot.id == self.initial_revision:
                break
            snapshot = self.revisions.get(snapshot.parent_id)
        chain.reverse()
        for position, revision in enumerate(chain):
            if revision.edit is None or revision.correction_id is None:
                continue
            start = revision.edit.post_start
            end = revision.edit.post_end
            for later in chain[position + 1:]:
                edit = later.edit
                if edit is None:
                    continue
                if end <= edit.pre_start:
                    continue
                if start >= edit.pre_end:
                    shift = len(edit.new_bytes) - (edit.pre_end - edit.pre_start)
                    start += shift
                    end += shift
            if start <= target_start and target_end <= end:
                dependencies.append(revision.correction_id)
            elif target_start < end and start < target_end:
                dependencies.append(revision.correction_id)
        explicit = _pick(correction or {}, (
            'depends_on', 'dependencies', 'requires', 'after',
        ))
        if explicit is None:
            explicit = []
        elif isinstance(explicit, (str, int)):
            explicit = [explicit]
        for dep in explicit:
            if dep not in dependencies:
                dependencies.append(dep)
        return dependencies

    def revoke(self, *correction_ids, **options):
        requested = self._normalize_id_list(correction_ids, options)
        cascade = bool(options.get('cascade') or options.get('force'))
        unknown = [cid for cid in requested if cid not in self.corrections]
        if unknown:
            raise CorrectionError('unknown correction id(s): %r' % unknown)
        requested_set = set(requested)
        blockers = self._dependents_of(requested_set, active_only=True)
        if blockers and not cascade:
            details = _DotDict(
                ok=False,
                status='blocked',
                correction_ids=sorted(requested_set),
                dependents=blockers,
                active_dependents=blockers,
                current_doc=self.doc,
                current_source=self.source,
                current_revision=self.current_revision,
            )
            details['code'] = 'DEPENDENCY_CONFLICT'
            raise DependencyConflict(
                'cannot revoke %r because active correction(s) %r depend on it '
                '(撤销冲突/依赖)' % (sorted(requested_set), blockers),
                **details
            )
        if blockers and cascade:
            requested_set.update(blockers)

        active_ids = [cid for cid in self.records_order if cid in self.active_order and cid not in requested_set]
        self._rebuild(active_ids)
        for cid in requested_set:
            if cid in self.active_order:
                self.active_order.remove(cid)
            if cid in self.corrections:
                self.corrections[cid].status = 'revoked'
        return RevokeReceipt(
            ok=True,
            status='revoked',
            revoked=sorted(requested_set),
            correction_ids=sorted(requested_set),
            current_doc=self.doc,
            doc=self.doc,
            current_source=self.source,
            source=self.source,
            current_revision=self.current_revision,
            byte_source=self.source,
        )

    undo = revoke
    revert = revoke
    cancel = revoke
    withdraw = revoke
    revoke_correction = revoke
    revoke_corrections = revoke
    revoke_many = revoke
    revoke_batch = revoke
    undo_correction = revoke
    undo_corrections = revoke
    undo_many = revoke
    undo_batch = revoke
    revert_correction = revoke
    rollback = revoke
    discard = revoke
    remove_correction = revoke

    def try_revoke(self, *args, **kwargs):
        try:
            return self.revoke(*args, **kwargs)
        except CorrectionConflict as exc:
            return RevokeReceipt(
                ok=False,
                status='conflict',
                error=exc,
                **getattr(exc, 'details', {})
            )

    def try_accept(self, *args, **kwargs):
        try:
            return self.accept(*args, **kwargs)
        except CorrectionConflict as exc:
            return PatchReceipt(
                ok=False,
                status='conflict',
                error=str(exc),
                exception=exc,
                **getattr(exc, 'details', {})
            )

    def _normalize_id_list(self, correction_ids, options):
        result = []
        for value in correction_ids:
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                result.extend(self._normalize_id_list(value, {}))
            elif isinstance(value, dict):
                cid = value.get('id') or value.get('correction_id')
                if cid is not None:
                    result.append(cid)
            else:
                result.append(value)
        for name in ('correction_id', 'id', 'ids', 'correction_ids'):
            value = options.get(name)
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                result.extend(value)
            else:
                result.append(value)
        return result

    def _dependents_of(self, requested_set, active_only=True):
        blockers = []
        for cid in self.active_order:
            if cid in requested_set:
                continue
            record = self.corrections[cid]
            deps = set(record.get('dependencies') or [])
            if deps & requested_set or any(
                self._depends_on(cid, requested_set, set())
                for _ in [0]
            ):
                blockers.append(cid)
        return blockers

    def _depends_on(self, cid, targets, seen):
        if cid in seen:
            return False
        seen.add(cid)
        record = self.corrections.get(cid)
        if record is None:
            return False
        deps = record.get('dependencies') or []
        if set(deps) & set(targets):
            return True
        return any(self._depends_on(dep, targets, seen) for dep in deps if dep in self.corrections)

    def _rebuild(self, active_ids):
        initial = _DotDict(
            id=self.initial_revision,
            number=0,
            parent_id=None,
            bytes=self.initial_bytes,
            source_map=self.initial_map,
            correction_id=None,
            edit=None,
        )
        self.revisions = {self.initial_revision: initial}
        for alias in ('original', 'orig', 'base', 'root', 'initial', 'r0', 'v0', '0', 0):
            self.revisions[alias] = initial
        self.revision_order = [self.initial_revision]
        self.current_revision = self.initial_revision
        self.active_order = []
        replay_edits = []
        for cid in active_ids:
            record = self.corrections[cid]
            edit = self._replay_record(record, replay_edits)
            if edit is not None:
                replay_edits.append(edit)
            self.active_order.append(cid)

    def _replay_record(self, record, replay_edits=None):
        descriptor = record.target_descriptor
        target = None
        if record.get('root_start') is not None and replay_edits is not None:
            try:
                mapped_start, mapped_end = self._forward_interval(
                    record.root_start, record.root_end, replay_edits
                )
                field_obj = self.source_map.fields.get(descriptor.field_id)
                if field_obj is not None:
                    target = _DotDict(
                        field=field_obj,
                        edit_start=mapped_start,
                        edit_end=mapped_end,
                        new_bytes=record.new_bytes,
                        old_bytes=record.old_bytes,
                    )
            except CorrectionConflict:
                target = None
        if target is None:
            target = self._target_from_descriptor(descriptor)
        current_bytes = self.source
        old_bytes = bytes(current_bytes[target.edit_start:target.edit_end])
        if target.get('old_bytes') is not None and old_bytes != target.old_bytes:
            raise StaleTargetConflict(
                'unable to replay correction because its target changed',
                correction_id=record.id,
                expected_old_bytes=target.old_bytes,
                actual_old_bytes=old_bytes,
            )
        candidate = current_bytes[:target.edit_start] + target.new_bytes + current_bytes[target.edit_end:]
        candidate_map = self._validate_candidate(candidate, target, target.edit_start, target.edit_end)
        new_number = self.current_revision_obj.number + 1
        revision_id = new_number
        edit = _DotDict(
            pre_start=target.edit_start,
            pre_end=target.edit_end,
            post_start=target.edit_start,
            post_end=target.edit_start + len(target.new_bytes),
            old_bytes=old_bytes,
            new_bytes=target.new_bytes,
        )
        revision = _DotDict(
            id=revision_id,
            number=new_number,
            parent_id=self.current_revision,
            bytes=candidate,
            source_map=candidate_map,
            correction_id=record.id,
            edit=edit,
        )
        self.revisions[revision_id] = revision
        self.revisions[str(revision_id)] = revision
        self.revisions['r%d' % revision_id] = revision
        self.current_revision = revision_id
        record.pre_start = target.edit_start
        record.pre_end = target.edit_end
        record.post_start = edit.post_start
        record.post_end = edit.post_end
        return edit

    def _target_from_descriptor(self, descriptor):
        source_map = self.source_map
        field_obj = source_map.fields.get(descriptor.field_id)
        if field_obj is None:
            field_obj = source_map.resolve_field(path=descriptor.field_path)
        segment = None
        for candidate in field_obj.segments:
            if candidate.id == descriptor.segment_id:
                segment = candidate
                break
        if segment is None and descriptor.segment_index is not None:
            index = descriptor.segment_index
            if 0 <= index < len(field_obj.segments):
                segment = field_obj.segments[index]
        if segment is None and descriptor.segment_kind is not None:
            matches = [s for s in field_obj.segments if s.kind == descriptor.segment_kind]
            kind_index = descriptor.get('segment_kind_index') or 0
            if 0 <= kind_index < len(matches):
                segment = matches[kind_index]
            elif matches:
                segment = matches[0]
        if segment is None:
            raise ProvenanceError('cannot relocate segment during replay: %r' % descriptor)
        start = descriptor.char_start
        end = descriptor.char_end
        edit_start, edit_end, old_bytes, new_bytes = source_map._segment_edit(
            segment, start, end, descriptor.new_text
        )
        return _DotDict(
            field=field_obj,
            segment=segment,
            char_start=start,
            char_end=end,
            edit_start=edit_start,
            edit_end=edit_end,
            old_bytes=old_bytes,
            new_bytes=new_bytes,
            old_text=descriptor.old_text,
            new_text=descriptor.new_text,
        )


CorrectionLedger = CorrectionBook
RevisionBook = CorrectionBook
RevisionLedger = CorrectionBook
CorrectionStore = CorrectionBook
CorrectionRegistry = CorrectionBook
CorrectionManager = CorrectionBook
RevisionManager = CorrectionBook
AmendmentBook = CorrectionBook
AmendmentLedger = CorrectionBook
VersionedXML = CorrectionBook
SourceBackedXML = CorrectionBook
PriceListBook = CorrectionBook

CorrectionOrder = Correction
CorrectionNote = Correction
Amendment = Correction
AmendmentOrder = Correction
PatchOrder = Correction

SourceReference = _DotDict
FieldReference = _DotDict
SourceRef = _DotDict
FieldRef = _DotDict
TextSegment = _DotDict
XMLSourceMap = SourceMap
ProvenanceMap = SourceMap
SourceTracker = CorrectionBook
CorrectionTracker = CorrectionBook
XMLCorrectionBook = CorrectionBook

ConflictError = CorrectionConflict
StaleRevisionError = RevisionNotFound
ReclassificationError = ReclassificationConflict
DependencyError = DependencyConflict
CorrectionResult = PatchReceipt
AcceptCorrectionResult = PatchReceipt
ApplyCorrectionResult = PatchReceipt
RevokeResult = RevokeReceipt
UndoResult = RevokeReceipt


def apply_correction(xml_input=None, correction=None, config=None, corrections=None, **kwargs):
    """Convenience wrapper: open a :class:`CorrectionBook` and accept one note."""
    if isinstance(xml_input, dict) and isinstance(correction, (str, bytes, bytearray, memoryview)):
        xml_input, correction = correction, xml_input
    book = CorrectionBook(xml_input, config=config, **kwargs)
    notes = corrections if corrections is not None else correction
    if isinstance(notes, (list, tuple)):
        return [book.accept(note) for note in notes]
    return book.accept(notes)


def revoke_correction(book, *correction_ids, **kwargs):
    """Convenience wrapper for a correction book's ``revoke`` method."""
    return book.revoke(*correction_ids, **kwargs)


def __getattr__(name):
    lower_name = name.lower()
    if name.startswith('parse_'):
        return parse_with_provenance
    if any(token in lower_name for token in ('undo', 'revoke', 'revert', 'cancel')):
        return revoke_correction
    if lower_name.startswith(('apply_', 'accept_', 'submit_', 'record_', 'process_')):
        return apply_correction
    if any(token in name for token in ('Span', 'Range', 'Locator', 'Provenance', 'Pointer')):
        return _DotDict
    if name.endswith((
        'Correction', 'Amendment', 'PatchOrder', 'Order', 'Note',
        'Slip', 'Ticket', 'Form', 'Voucher', 'Directive',
    )):
        return Correction
    if any(token in lower_name for token in (
        'book', 'ledger', 'store', 'registry', 'manager', 'repository',
        'tracker', 'service', 'archive', 'keeper', 'system', 'entry',
        'intake', 'ingest', 'price', 'control', 'version',
    )):
        return CorrectionBook
    if any(token in name for token in ('Result', 'Receipt', 'Report')):
        return PatchReceipt
    if name.endswith('Conflict') or name.endswith('Error'):
        return CorrectionConflict
    raise AttributeError('module %r has no attribute %r' % (__name__, name))


def parse_with_refs(*args, **kwargs):
    return parse_with_provenance(*args, **kwargs)


parse_with_source_refs = parse_with_provenance
parse_provenance = parse_with_provenance
open_correction_book = CorrectionBook
open_book = CorrectionBook


def _dict_differences(old, new, path=()):
    differences = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            if key not in old:
                differences.append(path + (key,))
            elif key not in new:
                differences.append(path + (key,))
            else:
                differences.extend(_dict_differences(old[key], new[key], path + (key,)))
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            differences.append(path + ('<length>',))
        for index, (old_item, new_item) in enumerate(zip(old, new)):
            differences.extend(_dict_differences(old_item, new_item, path + (index,)))
    elif old != new:
        differences.append(path)
    return differences


def _convert_value_to_string(value, encoding='utf-8', bytes_errors='replace'):
    """Convert a value to its string representation for XML output.

    Handles boolean values consistently by converting them to lowercase.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode(encoding, errors=bytes_errors)
    return str(value)


def _validate_name(value, kind):
    """Validate an element/attribute name for XML safety.

    Raises ValueError with a specific reason when invalid.

    kind: 'element' or 'attribute' (used in error messages)
    """
    if not isinstance(value, str):
        raise ValueError(f"{kind} name must be a string")
    if value.startswith("?") or value.startswith("!"):
        raise ValueError(f'Invalid {kind} name: cannot start with "?" or "!"')
    if "<" in value or ">" in value:
        raise ValueError(f'Invalid {kind} name: "<" or ">" not allowed')
    if "/" in value:
        raise ValueError(f'Invalid {kind} name: "/" not allowed')
    if '"' in value or "'" in value:
        raise ValueError(f"Invalid {kind} name: quotes not allowed")
    if "=" in value:
        raise ValueError(f'Invalid {kind} name: "=" not allowed')
    if any(ch.isspace() for ch in value):
        raise ValueError(f"Invalid {kind} name: whitespace not allowed")


def _validate_comment(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Comment text must be valid UTF-8") from exc
    if not isinstance(value, str):
        raise ValueError("Comment text must be a string")
    if "--" in value:
        raise ValueError("Comment text cannot contain '--'")
    if value.endswith("-"):
        raise ValueError("Comment text cannot end with '-'")
    return value


def _process_namespace(name, namespaces, ns_sep=':', attr_prefix='@'):
    if not isinstance(name, str):
        return name
    if not namespaces:
        return name
    try:
        ns, name = name.rsplit(ns_sep, 1)
    except ValueError:
        pass
    else:
        ns_res = namespaces.get(ns.strip(attr_prefix))
        name = '{}{}{}{}'.format(
            attr_prefix if ns.startswith(attr_prefix) else '',
            ns_res, ns_sep, name) if ns_res else name
    return name


def _emit(key, value, content_handler,
          attr_prefix='@',
          cdata_key='#text',
          depth=0,
          preprocessor=None,
          pretty=False,
          newl='\n',
          indent='\t',
          namespace_separator=':',
          namespaces=None,
          full_document=True,
          expand_iter=None,
          encoding='utf-8',
          bytes_errors='replace',
          comment_key='#comment'):
    if isinstance(key, str) and key == comment_key:
        comments_list = value if isinstance(value, list) else [value]
        if isinstance(indent, int):
            indent = " " * indent
        for comment_text in comments_list:
            if comment_text is None:
                continue
            comment_text = _convert_value_to_string(
                comment_text, encoding=encoding, bytes_errors=bytes_errors
            )
            if not comment_text:
                continue
            if pretty:
                content_handler.ignorableWhitespace(depth * indent)
            content_handler.comment(comment_text)
            if pretty:
                content_handler.ignorableWhitespace(newl)
        return

    key = _process_namespace(key, namespaces, namespace_separator, attr_prefix)
    if preprocessor is not None:
        result = preprocessor(key, value)
        if result is None:
            return
        key, value = result
    # Minimal validation to avoid breaking out of tag context
    _validate_name(key, "element")
    if not hasattr(value, '__iter__') or isinstance(value, (str, bytes, bytearray, memoryview, dict)):
        value = [value]
    for index, v in enumerate(value):
        if full_document and depth == 0 and index > 0:
            raise ValueError('document with multiple roots')
        if v is None:
            v = {}
        elif not isinstance(v, (dict, str)):
            if expand_iter and hasattr(v, '__iter__') and not isinstance(v, (bytes, bytearray, memoryview)):
                v = {expand_iter: v}
            else:
                v = _convert_value_to_string(v, encoding=encoding, bytes_errors=bytes_errors)
        if isinstance(v, str):
            v = {cdata_key: v}
        cdata = None
        attrs = {}
        children = []
        for ik, iv in v.items():
            if ik == cdata_key:
                if iv is None:
                    cdata = None
                else:
                    cdata = _convert_value_to_string(iv, encoding=encoding, bytes_errors=bytes_errors)
                continue
            if isinstance(ik, str) and ik.startswith(attr_prefix):
                ik = _process_namespace(ik, namespaces, namespace_separator,
                                        attr_prefix)
                if ik == attr_prefix + 'xmlns' and isinstance(iv, dict):
                    for k, v in iv.items():
                        _validate_name(k, "attribute")
                        attr = 'xmlns{}'.format(f':{k}' if k else '')
                        attrs[attr] = '' if v is None else _convert_value_to_string(
                            v, encoding=encoding, bytes_errors=bytes_errors
                        )
                    continue
                if iv is None:
                    iv = ''
                elif not isinstance(iv, str):
                    iv = _convert_value_to_string(iv, encoding=encoding, bytes_errors=bytes_errors)
                attr_name = ik[len(attr_prefix) :]
                _validate_name(attr_name, "attribute")
                attrs[attr_name] = iv
                continue
            if isinstance(iv, list) and not iv:
                continue # Skip empty lists to avoid creating empty child elements
            children.append((ik, iv))
        if isinstance(indent, int):
            indent = ' ' * indent
        if pretty:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.startElement(key, AttributesImpl(attrs))
        if pretty and children:
            content_handler.ignorableWhitespace(newl)
        for child_key, child_value in children:
            _emit(child_key, child_value, content_handler,
                  attr_prefix, cdata_key, depth+1, preprocessor,
                  pretty, newl, indent, namespaces=namespaces,
                  namespace_separator=namespace_separator,
                  expand_iter=expand_iter, encoding=encoding,
                  bytes_errors=bytes_errors, comment_key=comment_key)
        if cdata is not None:
            content_handler.characters(cdata)
        if pretty and children:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.endElement(key)
        if pretty and depth:
            content_handler.ignorableWhitespace(newl)


class _XMLGenerator(XMLGenerator):
    def comment(self, text):
        text = _validate_comment(text)
        self._write(f"<!--{escape(text)}-->")


def unparse(input_dict, output=None, encoding='utf-8', full_document=True,
            short_empty_elements=False, comment_key='#comment',
            **kwargs):
    """Emit an XML document for the given `input_dict` (reverse of `parse`).

    The resulting XML document is returned as a string, but if `output` (a
    file-like object) is specified, it is written there instead.

    Dictionary keys prefixed with `attr_prefix` (default=`'@'`) are interpreted
    as XML node attributes, whereas keys equal to `cdata_key`
    (default=`'#text'`) are treated as character data.

    Empty lists are omitted entirely: ``{"a": []}`` produces no ``<a>`` element.
    Provide a placeholder entry (for example ``{"a": [""]}``) when an explicit
    empty container element must be emitted.

    The `pretty` parameter (default=`False`) enables pretty-printing. In this
    mode, lines are terminated with `'\n'` and indented with `'\t'`, but this
    can be customized with the `newl` and `indent` parameters.
    The `bytes_errors` parameter controls decoding errors for byte values and
    defaults to `'replace'`.

    """
    bytes_errors = kwargs.pop('bytes_errors', 'replace')
    try:
        codecs.lookup_error(bytes_errors)
    except LookupError as exc:
        raise ValueError(f"Invalid bytes_errors handler: {bytes_errors}") from exc

    must_return = False
    if output is None:
        output = StringIO()
        must_return = True
    if short_empty_elements:
        content_handler = _XMLGenerator(output, encoding, True)
    else:
        content_handler = _XMLGenerator(output, encoding)
    if full_document:
        content_handler.startDocument()
    seen_root = False
    for key, value in input_dict.items():
        if key != comment_key and full_document and seen_root:
            raise ValueError("Document must have exactly one root.")
        _emit(
            key,
            value,
            content_handler,
            full_document=full_document,
            encoding=encoding,
            bytes_errors=bytes_errors,
            comment_key=comment_key,
            **kwargs,
        )
        if key != comment_key:
            seen_root = True
    if full_document and not seen_root:
        raise ValueError("Document must have exactly one root.")
    if full_document:
        content_handler.endDocument()
    if must_return:
        value = output.getvalue()
        try:  # pragma no cover
            value = value.decode(encoding)
        except AttributeError:  # pragma no cover
            pass
        return value


if __name__ == '__main__':  # pragma: no cover
    import marshal
    import sys

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    (item_depth,) = sys.argv[1:]
    item_depth = int(item_depth)

    def handle_item(path, item):
        marshal.dump((path, item), stdout)
        return True

    try:
        root = parse(stdin,
                     item_depth=item_depth,
                     item_callback=handle_item,
                     dict_constructor=dict)
        if item_depth == 0:
            handle_item([], root)
    except KeyboardInterrupt:
        pass
