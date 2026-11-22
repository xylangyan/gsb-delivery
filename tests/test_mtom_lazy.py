from io import StringIO

import pytest
import requests_mock
from pretend import stub

from zeep import Client
from zeep.exceptions import IncompleteMessage, LazyAttachmentError
from zeep.settings import Settings
from zeep.wsdl.lazy import (
    AttachmentPart,
    LazyBinary,
    LazyMessagePack,
    content_id_from_href,
)
from zeep.wsdl.messages import xop

WSDL = """
<?xml version="1.0"?>
<wsdl:definitions
  xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:tns="http://tests.python-zeep.org/mtom-lazy"
  xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
  targetNamespace="http://tests.python-zeep.org/mtom-lazy">
  <wsdl:types>
    <xsd:schema targetNamespace="http://tests.python-zeep.org/mtom-lazy"
                xmlns:tns="http://tests.python-zeep.org/mtom-lazy">
      <xsd:complexType name="ExportResult">
        <xsd:sequence>
          <xsd:element name="name" type="xsd:string"/>
          <xsd:element name="thumb" type="xsd:base64Binary" minOccurs="0"/>
          <xsd:element name="document" type="xsd:base64Binary" minOccurs="0"/>
        </xsd:sequence>
      </xsd:complexType>
      <xsd:complexType name="BinaryDataType">
        <xsd:simpleContent>
          <xsd:extension base="xsd:base64Binary">
            <xsd:anyAttribute namespace="##other" processContents="lax"/>
          </xsd:extension>
        </xsd:simpleContent>
      </xsd:complexType>
      <xsd:complexType name="ComplexExportResult">
        <xsd:sequence>
          <xsd:element name="BinaryData" type="tns:BinaryDataType"/>
        </xsd:sequence>
      </xsd:complexType>
      <xsd:element name="input" type="xsd:string"/>
      <xsd:element name="result" type="tns:ExportResult"/>
      <xsd:element name="resultComplex" type="tns:ComplexExportResult"/>
    </xsd:schema>
  </wsdl:types>

  <wsdl:message name="dummyRequest">
    <wsdl:part name="response" element="tns:input"/>
  </wsdl:message>
  <wsdl:message name="dummyResponse">
    <wsdl:part name="response" element="tns:result"/>
  </wsdl:message>
  <wsdl:message name="dummyResponseComplex">
    <wsdl:part name="response" element="tns:resultComplex"/>
  </wsdl:message>

  <wsdl:portType name="TestPortType">
    <wsdl:operation name="Export">
      <wsdl:input message="tns:dummyRequest"/>
      <wsdl:output message="tns:dummyResponse"/>
    </wsdl:operation>
    <wsdl:operation name="ExportComplex">
      <wsdl:input message="tns:dummyRequest"/>
      <wsdl:output message="tns:dummyResponseComplex"/>
    </wsdl:operation>
  </wsdl:portType>

  <wsdl:binding name="TestBinding" type="tns:TestPortType">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <wsdl:operation name="Export">
      <soap:operation soapAction="urn:dummyRequest"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input>
      <wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation>
    <wsdl:operation name="ExportComplex">
      <soap:operation soapAction="urn:dummyRequest"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input>
      <wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation>
  </wsdl:binding>
  <wsdl:service name="TestService">
    <wsdl:port name="TestPortType" binding="tns:TestBinding">
      <soap:address location="http://tests.python-zeep.org/test"/>
    </wsdl:port>
  </wsdl:service>
</wsdl:definitions>
"""

CONTENT_TYPE = (
    'multipart/related; boundary="boundary"; type="application/xop+xml"; '
    'start="<soap:Envelope>"; start-info="application/soap+xml; charset=utf-8"'
)


def make_response(parts):
    """Build a multipart/related response body from (headers, body) parts."""
    chunks = []
    for headers, body in parts:
        chunks.append("--boundary\r\n")
        chunks.append("\r\n".join(headers))
        chunks.append("\r\n\r\n")
        chunks.append(body)
        chunks.append("\r\n")
    chunks.append("--boundary--\r\n")
    data = "".join(chunks).encode("utf-8")
    return stub(
        status_code=200,
        content=data,
        encoding="utf-8",
        headers={"Content-Type": CONTENT_TYPE},
    )


def root_part(body):
    return (
        [
            "Content-Type: application/xop+xml; charset=utf-8; "
            'type="application/soap+xml"',
            "Content-Transfer-Encoding: binary",
            "Content-ID: <soap:Envelope>",
        ],
        body,
    )


def attachment_part(content_id, body, content_type="application/binary"):
    return (
        [
            "Content-Type: %s" % content_type,
            "Content-Transfer-Encoding: binary",
            "Content-ID: <%s>" % content_id,
        ],
        body,
    )


ENVELOPE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope
    xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:xop="http://www.w3.org/2004/08/xop/include"
    xmlns:test="http://tests.python-zeep.org/mtom-lazy">
  <soap:Body>
    <test:result>
      <test:name>{name}</test:name>
      <test:thumb>{thumb}</test:thumb>
      <test:document>{document}</test:document>
    </test:result>
  </soap:Body>
</soap:Envelope>
"""


def xop_include(cid):
    return '<xop:Include href="cid:%s"/>' % cid


def get_binding(settings=None):
    client = Client(StringIO(WSDL.strip()), settings=settings or Settings())
    return client, client.service._binding, client.service._binding.get("Export")


class CountingPart:
    """Mimics a toolbelt BodyPart while counting raw content accesses."""

    def __init__(self, content_id, payload):
        self.encoding = "utf-8"
        self.headers = {
            b"Content-Type": b"application/binary",
            b"Content-Transfer-Encoding": b"binary",
            b"Content-ID": ("<%s>" % content_id).encode("utf-8"),
        }
        self._payload = payload
        self.read_count = 0

    @property
    def content(self):
        self.read_count += 1
        return self._payload


def test_content_id_from_href():
    assert content_id_from_href("cid:id4") == "<id4>"
    assert content_id_from_href("<id4>") == "<id4>"
    assert content_id_from_href("cid:a%20b") == "<a b>"


def test_lazy_end_to_end_small_field_and_lazy_binary():
    settings = Settings(mtom_lazy=True)
    client, binding, operation = get_binding(settings)

    envelope = ENVELOPE_TEMPLATE.format(
        name="report", thumb=xop_include("big"), document=xop_include("big")
    )
    response = make_response(
        [root_part(envelope), attachment_part("big", "BINARYDATA")]
    )

    pack = binding.process_reply(client, operation, response)

    assert isinstance(pack, LazyMessagePack)
    # The small field is materialized immediately and available without
    # touching any attachment.
    assert pack.root.name == "report"
    # The big fields stay lazy until they are read.
    assert isinstance(pack.root.thumb, LazyBinary)
    assert isinstance(pack.root.document, LazyBinary)

    assert bytes(pack.root.thumb) == b"BINARYDATA"
    assert pack.root.document.read() == b"BINARYDATA"


def test_eager_path_unchanged_when_lazy_disabled():
    client, binding, operation = get_binding(Settings(mtom_lazy=False))

    envelope = ENVELOPE_TEMPLATE.format(
        name="report", thumb=xop_include("big"), document=xop_include("big")
    )
    response = make_response(
        [root_part(envelope), attachment_part("big", "BINARYDATA")]
    )

    result = binding.process_reply(client, operation, response)
    # Default behaviour inlines the attachment eagerly into plain bytes.
    assert not isinstance(result, LazyMessagePack)
    assert result.thumb == b"BINARYDATA"
    assert result.document == b"BINARYDATA"


def test_shared_part_is_read_only_once():
    part = CountingPart("shared", b"X" * 1024)
    pack = LazyMessagePack(parts=[part])

    first = pack.open("cid:shared")
    second = pack.open("cid:shared")

    assert part.read_count == 0
    assert first.read(10) == b"X" * 10
    assert part.read_count == 1
    assert bytes(second) == b"X" * 1024
    assert first.read() == b"X" * 1014
    # Both views share one physical read of the part.
    assert part.read_count == 1


def test_parallel_packs_are_isolated_by_content_id():
    part_a = CountingPart("shared", b"from-pack-a")
    part_b = CountingPart("shared", b"from-pack-b")

    pack_a = LazyMessagePack(parts=[part_a])
    pack_b = LazyMessagePack(parts=[part_b])

    assert bytes(pack_a.open("cid:shared")) == b"from-pack-a"
    assert bytes(pack_b.open("cid:shared")) == b"from-pack-b"
    # Cancelling pack B must not disturb pack A.
    pack_b.close()
    assert bytes(pack_a.open("cid:shared")) == b"from-pack-a"


def test_missing_attachment_is_distinguishable():
    part = CountingPart("present", b"")
    pack = LazyMessagePack(parts=[part])

    # A present, genuinely empty part yields real empty bytes, not None.
    present = pack.open("cid:present")
    assert present.read() == b""
    assert present.read() is not None

    # A missing Content-ID yields a view which raises on read instead of
    # silently returning None.
    with pytest.raises(IncompleteMessage):
        pack.open("cid:missing").read()


def test_missing_attachment_raises_during_field_access():
    settings = Settings(mtom_lazy=True)
    client, binding, operation = get_binding(settings)

    envelope = ENVELOPE_TEMPLATE.format(
        name="report", thumb=xop_include("missing"), document=""
    )
    response = make_response(
        [root_part(envelope), attachment_part("other", "DATA")]
    )

    pack = binding.process_reply(client, operation, response)
    with pytest.raises(IncompleteMessage):
        pack.root.thumb.read()


def test_duplicate_content_id_with_different_content_fails():
    with pytest.raises(LazyAttachmentError):
        LazyMessagePack(
            parts=[
                CountingPart("dup", b"first"),
                CountingPart("dup", b"second"),
            ]
        )


def test_duplicate_content_id_with_identical_content_is_accepted():
    pack = LazyMessagePack(
        parts=[
            CountingPart("dup", b"same"),
            CountingPart("dup", b"same"),
        ]
    )
    assert bytes(pack.open("cid:dup")) == b"same"


def test_independent_read_cursors():
    pack = LazyMessagePack(parts=[CountingPart("data", b"abcdef")])
    first = pack.open("cid:data")
    second = pack.open("cid:data")

    assert first.read(2) == b"ab"
    assert second.read(3) == b"abc"
    assert first.read() == b"cdef"
    assert second.read() == b"def"
    third = pack.open("cid:data")
    assert list(third.iter_content(2)) == [b"ab", b"cd", b"ef"]


def test_pack_close_does_not_harm_open_shared_stream():
    part = CountingPart("shared", b"payload-bytes")
    pack = LazyMessagePack(parts=[part])
    binary = pack.open("cid:shared")

    assert binary.read(4) == b"payl"
    pack.close()

    # The still-open view keeps the shared bytes alive.
    assert binary.read() == b"oad-bytes"

    # Once every view is closed the pack no longer hands out new binaries.
    binary.close()
    with pytest.raises(LazyAttachmentError):
        pack.open("cid:shared")


def test_closed_binary_view_cannot_be_read():
    pack = LazyMessagePack(parts=[CountingPart("data", b"abc")])
    binary = pack.open("cid:data")
    binary.close()
    with pytest.raises(LazyAttachmentError):
        binary.read()


def test_completed_field_can_be_verified_against_original_part():
    part = CountingPart("big", b"verifiable")
    pack = LazyMessagePack(parts=[part])
    binary = pack.open("cid:big")

    assert isinstance(binary.part, AttachmentPart)
    assert binary.content_id == "<big>"
    assert binary.part is pack.attachment_for("<big>")
    assert bytes(binary) == pack.attachment_for("<big>").read()


def test_base64_transfer_encoding_is_decoded_once():
    import base64

    raw = b"decoded-bytes"
    part = CountingPart("b64", base64.b64encode(raw))
    part.headers[b"Content-Transfer-Encoding"] = b"base64"
    pack = LazyMessagePack(parts=[part])

    assert bytes(pack.open("cid:b64")) == raw
    assert part.read_count == 1


def test_process_xop_still_rejects_missing_part_eagerly():
    envelope = ENVELOPE_TEMPLATE.format(
        name="report", thumb=xop_include("missing"), document=""
    )
    response = make_response(
        [root_part(envelope), attachment_part("other", "DATA")]
    )

    from requests_toolbelt.multipart.decoder import MultipartDecoder

    from zeep.wsdl.attachments import MessagePack

    decoder = MultipartDecoder(
        response.content, response.headers["Content-Type"], "utf-8"
    )
    from lxml import etree

    document = etree.fromstring(decoder.parts[0].content)
    pack = MessagePack(parts=decoder.parts[1:])
    with pytest.raises(ValueError):
        xop.process_xop(document, pack)


def test_acceptance_choreography_small_field_first_then_shared_part():
    """Two parallel exports: consume a small field from pack A, cancel pack B,
    then gradually read the attachment referenced twice by pack A."""
    settings = Settings(mtom_lazy=True)

    def build_pack(marker):
        client, binding, operation = get_binding(settings)
        envelope = ENVELOPE_TEMPLATE.format(
            name=marker,
            thumb=xop_include("shared"),
            document=xop_include("shared"),
        )
        response = make_response(
            [root_part(envelope), attachment_part("shared", "0123456789")]
        )
        return binding.process_reply(client, operation, response)

    pack_a = build_pack("a")
    pack_b = build_pack("b")

    # 1. Small field of the first response is readable straight away and the
    #    binary fields stay lazy.
    assert pack_a.root.name == "a"
    assert isinstance(pack_a.root.thumb, LazyBinary)

    # 2. The other response is cancelled; it must not leak into pack A.
    pack_b.close()

    # 3. Pack A's shared attachment (referenced twice) is then read gradually.
    thumb = pack_a.root.thumb
    document = pack_a.root.document
    assert thumb.read(4) == b"0123"
    assert document.read(6) == b"012345"
    assert thumb.read() == b"456789"
    assert bytes(document) == b"0123456789"
    # Both fields resolve through the single pack-scoped part.
    assert thumb.part is document.part
    assert pack_a.attachment_for("<shared>").read() == b"0123456789"


def test_lazy_simple_content_extension_field():
    """An xsd:base64Binary extended through simpleContent stays lazy too."""
    settings = Settings(mtom_lazy=True)
    client, binding, operation = get_binding(settings)

    envelope = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope
    xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:xop="http://www.w3.org/2004/08/xop/include"
    xmlns:test="http://tests.python-zeep.org/mtom-lazy"
    xmlns:xm="http://www.w3.org/2005/05/xmlmime">
  <soap:Body>
    <test:resultComplex>
      <test:BinaryData xm:content-type="application/binary">
        <xop:Include href="cid:complex"/>
      </test:BinaryData>
    </test:resultComplex>
  </soap:Body>
</soap:Envelope>
"""
    response = make_response(
        [root_part(envelope), attachment_part("complex", "COMPLEXDATA")]
    )
    pack = binding.process_reply(
        client, binding.get("ExportComplex"), response
    )

    value = pack.root["_value_1"]
    assert isinstance(value, LazyBinary)
    assert bytes(value) == b"COMPLEXDATA"


def test_non_xop_multipart_falls_back_to_message_pack_under_lazy_setting():
    settings = Settings(mtom_lazy=True)
    client, binding, operation = get_binding(settings)

    plain_envelope = ENVELOPE_TEMPLATE.format(name="plain", thumb="", document="")

    from zeep.wsdl.attachments import MessagePack

    response = make_response(
        [root_part(plain_envelope), attachment_part("swa", "SWADATA")]
    )
    result = binding.process_reply(client, operation, response)

    # No xop:Include in the envelope -> classic MessagePack attachment handling.
    assert isinstance(result, MessagePack)
    assert result.attachments[0].content == b"SWADATA"


def test_public_client_service_returns_lazy_pack():
    client = Client(StringIO(WSDL.strip()), settings=Settings(mtom_lazy=True))
    service = client.create_service(
        "{http://tests.python-zeep.org/mtom-lazy}TestBinding",
        "http://tests.python-zeep.org/test",
    )

    envelope = ENVELOPE_TEMPLATE.format(
        name="report", thumb=xop_include("big"), document=""
    )
    body = make_response(
        [root_part(envelope), attachment_part("big", "BINARYDATA")]
    ).content

    with requests_mock.mock() as mock:
        mock.post(
            "http://tests.python-zeep.org/test",
            content=body,
            headers={"Content-Type": CONTENT_TYPE},
        )
        pack = service.Export("")

    assert isinstance(pack, LazyMessagePack)
    assert pack.root.name == "report"
    assert isinstance(pack.root.thumb, LazyBinary)
    assert bytes(pack.root.thumb) == b"BINARYDATA"
