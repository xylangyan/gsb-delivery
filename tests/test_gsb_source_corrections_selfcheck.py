import pytest

import xmltodict


XML = (
    '<prices xmlns:a="urn:a" xmlns:b="urn:b">'
    '<a:price> &#49;<unit/><![CDATA[2]]> </a:price>'
    '<b:price> &#49;<unit/><![CDATA[2]]> </b:price>'
    '</prices>'
)


def postprocessor(path, key, value):
    if key == 'price':
        text = value.get('#text') if isinstance(value, dict) else value
        try:
            amount = float(text)
        except (TypeError, ValueError):
            return key, value
        if amount > 105:
            return 'hold', value
    return key, value


def config():
    return dict(
        process_namespaces=True,
        namespaces={'urn:a': '', 'urn:b': ''},
        force_list=('price',),
        strip_whitespace=True,
        postprocessor=postprocessor,
    )


def first_price_ref(source_map):
    return source_map.resolve_field(path=['prices', 'price', 0, '#text'])


def test_optional_provenance_parse_keeps_normal_dict_equal():
    result = xmltodict.parse(XML, source_refs=True, **config())
    ordinary = xmltodict.parse(XML, **config())

    assert result == ordinary
    assert result['prices']['price'][0]['#text'] == '12'
    assert result[0] == ordinary
    assert result[1].source == XML.encode('utf-8')


def test_source_map_records_entity_and_cdata_byte_spans():
    _, source_map = xmltodict.parse_with_provenance(XML.encode('utf-8'), **config())
    field = first_price_ref(source_map)
    kinds = [(segment.kind, segment.text) for segment in field.segments]

    assert kinds == [('text', ' '), ('entity', '1'), ('cdata', '2'), ('text', ' ')]
    entity = field.segments[1]
    cdata = field.segments[2]
    assert source_map.source[entity.byte_start:entity.byte_end] == b'&#49;'
    assert source_map.source[cdata.content_start:cdata.content_end] == b'2'


def test_late_correction_is_rejected_when_postprocessor_reclassifies():
    book = xmltodict.CorrectionBook(XML, **config())
    field = first_price_ref(book.source_map)

    first = book.accept(
        id='C1', base_revision='original', field_ref=field,
        start=0, end=1, old_text='1', new_text='10',
    )
    assert (first.byte_start, first.byte_end) == (50, 55)
    assert first.old_bytes == b'&#49;'
    assert first.new_bytes == b'10'
    assert book.doc['prices']['price'][0]['#text'] == '102'

    with pytest.raises(xmltodict.ReclassificationConflict, match='归栏|hold|price'):
        book.accept(
            id='C2-9', base_revision='original', field_ref=field,
            start=1, end=2, old_text='2', new_text='9',
        )

    assert book.doc['prices']['price'][0]['#text'] == '102'
    assert book.doc['prices']['price'][1]['#text'] == '12'
    assert b'&#49;' not in book.source.split(b'</a:price>')[0]
    assert book.source.count(b'&#49;') == 1


def test_accepted_late_correction_and_dependent_undo():
    book = xmltodict.CorrectionBook(XML, **config())
    field = first_price_ref(book.source_map)

    book.accept(
        id='C1', base_revision='original', field_ref=field,
        start=0, end=1, old_text='1', new_text='10',
    )
    second = book.accept(
        id='C2', base_revision='original', field_ref=field,
        start=1, end=2, old_text='2', new_text='5',
    )
    assert second.old_bytes == b'2'
    assert second.new_bytes == b'5'
    assert book.doc['prices']['price'][0]['#text'] == '105'

    current_field = first_price_ref(book.source_map)
    third = book.accept(
        id='C3', base_revision=second.revision, field_ref=current_field,
        segment=0, start=1, end=2, old_text='1', new_text='0',
    )
    assert third.old_bytes == b'1'
    assert third.new_bytes == b'0'
    assert book.doc['prices']['price'][0]['#text'] == '005'

    with pytest.raises(xmltodict.DependencyConflict, match='C3') as exc_info:
        book.revoke('C1')
    assert exc_info.value.dependents == ['C3']
    assert book.doc['prices']['price'][0]['#text'] == '005'

    book.revoke('C1', 'C3')
    first_item = book.doc['prices']['price'][0]
    assert first_item['#text'] == '15'
    assert book.doc['prices']['price'][1]['#text'] == '12'
    assert b'&#49;<unit/><![CDATA[5]]>' in book.source


def test_plain_undo_of_first_correction_restores_entity():
    book = xmltodict.CorrectionBook(XML, **config())
    field = first_price_ref(book.source_map)
    book.accept(
        id='C1', base_revision='original', field_ref=field,
        start=0, end=1, old_text='1', new_text='10',
    )
    book.accept(
        id='C2', base_revision='original', field_ref=field,
        start=1, end=2, old_text='2', new_text='5',
    )

    book.revoke('C1')

    assert book.doc['prices']['price'][0]['#text'] == '15'
    assert b'&#49;<unit/><![CDATA[5]]>' in book.source


def test_stale_selection_conflicts_even_if_same_text_exists_elsewhere():
    book = xmltodict.CorrectionBook(XML, **config())
    field = first_price_ref(book.source_map)
    book.accept(
        id='C1', base_revision='original', field_ref=field,
        start=0, end=1, old_text='1', new_text='10',
    )

    with pytest.raises(xmltodict.StaleTargetConflict):
        book.accept(
            id='stale', base_revision='original', field_ref=field,
            start=0, end=1, old_text='1', new_text='7',
        )
    assert book.doc['prices']['price'][0]['#text'] == '102'


def test_arbitrary_single_byte_chunks_preserve_byte_offsets():
    raw = XML.encode('utf-8')
    chunks = (raw[index:index + 1] for index in range(len(raw)))
    book = xmltodict.CorrectionBook(chunks, **config())
    field = first_price_ref(book.source_map)

    receipt = book.accept(
        id='C1', base_revision='original', field_ref=field,
        start=0, end=1, old_text='1', new_text='10',
    )

    assert raw[receipt.byte_start:receipt.byte_end] == b'&#49;'
    assert receipt.old_bytes == b'&#49;'
    assert book.doc['prices']['price'][0]['#text'] == '102'


def test_multibyte_offsets_are_byte_based():
    xml = '<root><item>é49</item><item>x</item></root>'
    book = xmltodict.CorrectionBook(iter(xml.encode('utf-8')[i:i + 1] for i in range(len(xml.encode()))))
    field = book.source_map.resolve_field(path=['root', 'item', 0])
    receipt = book.accept(
        base_revision='original', field_ref=field,
        start=1, end=3, old_text='49', new_text='5',
    )

    assert receipt.old_bytes == b'49'
    assert xml.encode('utf-8')[receipt.byte_start:receipt.byte_end] == b'49'
    assert book.doc['root']['item'] == ['é5', 'x']


def test_annotated_string_carries_source_ref():
    result = xmltodict.parse(XML, source_ref=True, **config())
    value = result['prices']['price'][0]['#text']

    assert value == '12'
    assert isinstance(value, xmltodict.SourcedStr)
    assert value.source_ref.path[-1] == '#text'
    assert value.source_map is result.source_map
