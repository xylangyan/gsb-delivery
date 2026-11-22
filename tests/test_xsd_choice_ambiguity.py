"""Tests for backtracking resolution of ambiguous ``xsd:choice`` branches.

These cover the case where several choice branches share an element prefix
and a same-named required field follows the choice.  The complete element
sequence must decide which branch actually applies; a prefix that still
matches several branches keeps every candidate instead of greedily choosing
one.
"""

from lxml import etree

from tests.utils import assert_nodes_equal, load_xml, render_node
from zeep import Settings, xsd
from zeep.xsd.const import Nil
from zeep.xsd.elements.ambiguity import AMBIGUOUS_KEY

NS = "http://tests.python-zeep.org/"


def _schema(xsd_body):
    return xsd.Schema(
        load_xml(
            """
            <xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                    xmlns:tns="%s"
                    elementFormDefault="qualified"
                    targetNamespace="%s">
              %s
            </xsd:schema>
            """
            % (NS, NS, xsd_body)
        )
    )


# Two branches share the item_1 prefix; branch two additionally consumes an
# item_2, while a *required* item_2 sibling follows the choice.
PREFIXED_CHOICE = """
  <xsd:element name="container">
    <xsd:complexType>
      <xsd:sequence>
        <xsd:choice>
          <xsd:sequence>
            <xsd:element name="item_1" type="xsd:string"/>
          </xsd:sequence>
          <xsd:sequence>
            <xsd:element name="item_1" type="xsd:string"/>
            <xsd:element name="item_2" type="xsd:string"/>
          </xsd:sequence>
        </xsd:choice>
        <xsd:element name="item_2" type="xsd:string"/>
      </xsd:sequence>
    </xsd:complexType>
  </xsd:element>
"""


def test_trailing_required_element_disambiguates_choice():
    schema = _schema(PREFIXED_CHOICE)
    element = schema.get_element("ns0:container")

    node = load_xml(
        """
        <ns0:container xmlns:ns0="%s">
          <ns0:item_1>a</ns0:item_1>
          <ns0:item_2>b</ns0:item_2>
        </ns0:container>
        """
        % NS
    )
    value = element.parse(node, schema)

    # The trailing required item_2 must be the sibling (item_2__1), so the
    # short branch wins even though both branches accept the first node.
    assert value.item_1 == "a"
    assert value.item_2 is None
    assert value.item_2__1 == "b"
    assert AMBIGUOUS_KEY not in value.__values__


def test_longer_branch_selected_when_its_extra_node_is_present():
    from collections import deque

    from zeep.xsd.elements.ambiguity import SequenceResolver

    schema = _schema(PREFIXED_CHOICE)
    sequence = schema.get_element("ns0:container").type._element

    node = load_xml(
        """
        <ns0:container xmlns:ns0="%s">
          <ns0:item_1>a</ns0:item_1>
          <ns0:item_2>b</ns0:item_2>
          <ns0:item_2>c</ns0:item_2>
        </ns0:container>
        """
        % NS
    )
    consumed, result, candidates, leftover = SequenceResolver(
        sequence, schema, None, sequence
    ).resolve(deque(node.iterchildren()))

    # The whole sequence is consumed; the first item_2 belongs to the long
    # branch and the second to the required sibling.
    assert consumed == 3
    assert not leftover
    assert result == {"item_1": "a", "item_2": "b", "item_2__1": "c"}
    assert len(candidates) == 1


def test_prefix_does_not_consume_trailing_required_sibling():
    from collections import deque

    from zeep.xsd.elements.ambiguity import SequenceResolver

    schema = _schema(PREFIXED_CHOICE)
    sequence = schema.get_element("ns0:container").type._element

    node = load_xml(
        """
        <ns0:container xmlns:ns0="%s">
          <ns0:item_1>a</ns0:item_1>
        </ns0:container>
        """
        % NS
    )
    consumed, result, candidates, leftover = SequenceResolver(
        sequence, schema, None, sequence
    ).resolve(deque(node.iterchildren()))

    # The short branch explains the prefix without leaving a required sibling
    # unaccounted for, so it is the single best parse; the long branch is
    # penalised for still expecting its own item_2.
    assert consumed == 1
    assert result == {"item_1": "a"}
    assert len(candidates) == 1
    assert not leftover


def test_ambiguous_full_bundle_returns_candidates():
    schema = _schema(
        """
        <xsd:element name="container">
          <xsd:complexType>
            <xsd:sequence>
              <xsd:choice>
                <xsd:element name="item" type="xsd:string"/>
                <xsd:element name="item" type="xsd:integer"/>
              </xsd:choice>
            </xsd:sequence>
          </xsd:complexType>
        </xsd:element>
        """
    )
    element = schema.get_element("ns0:container")
    value = element.parse(
        load_xml(
            """
            <ns0:container xmlns:ns0="%s">
              <ns0:item>1</ns0:item>
            </ns0:container>
            """
            % NS
        ),
        schema,
    )

    candidates = value.__values__[AMBIGUOUS_KEY]
    assert len(candidates) == 2
    assert {"item": "1"} in candidates
    assert {"item__1": 1} in candidates


def test_two_locked_xsd_versions_selected_by_message_evidence():
    v1 = _schema(PREFIXED_CHOICE)
    v2 = _schema(
        """
        <xsd:element name="container">
          <xsd:complexType>
            <xsd:sequence>
              <xsd:choice>
                <xsd:sequence>
                  <xsd:element name="item_1" type="xsd:string"/>
                </xsd:sequence>
                <xsd:sequence>
                  <xsd:element name="item_1" type="xsd:string"/>
                  <xsd:element name="item_2" type="xsd:string"/>
                </xsd:sequence>
              </xsd:choice>
              <xsd:element name="item_3" type="xsd:string"/>
            </xsd:sequence>
          </xsd:complexType>
        </xsd:element>
        """
    )

    # The item_3 element only exists in version 2, selecting that schema.
    node = load_xml(
        """
        <ns0:container xmlns:ns0="%s">
          <ns0:item_1>a</ns0:item_1>
          <ns0:item_3>c</ns0:item_3>
        </ns0:container>
        """
        % NS
    )
    value = v2.get_element("ns0:container").parse(node, v2)
    assert value.item_1 == "a"
    assert value.item_3 == "c"


def test_rewrite_preserves_order_missing_and_nil():
    schema = _schema(
        """
        <xsd:element name="doc">
          <xsd:complexType>
            <xsd:sequence>
              <xsd:element name="a" type="xsd:string"/>
              <xsd:element name="b" type="xsd:string" minOccurs="0"/>
              <xsd:element name="c" type="xsd:string" nillable="true"/>
              <xsd:choice>
                <xsd:element name="x" type="xsd:string" minOccurs="0"/>
                <xsd:element name="y" type="xsd:string" minOccurs="0"/>
              </xsd:choice>
            </xsd:sequence>
          </xsd:complexType>
        </xsd:element>
        """
    )
    element = schema.get_element("ns0:doc")
    incoming = load_xml(
        """
        <ns0:doc xmlns:ns0="%s">
          <ns0:a>1</ns0:a>
          <ns0:c xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>
          <ns0:y>9</ns0:y>
        </ns0:doc>
        """
        % NS
    )
    value = element.parse(incoming, schema)
    node = render_node(element, value)

    # Order is retained, the missing optional b is omitted and the explicit
    # xsi:nil on c is preserved verbatim.
    assert_nodes_equal(
        node,
        """
        <document>
          <ns0:doc xmlns:ns0="%s">
            <ns0:a>1</ns0:a>
            <ns0:c xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>
            <ns0:y>9</ns0:y>
          </ns0:doc>
        </document>
        """
        % NS,
    )


def test_non_strict_mode_keeps_unparsed_extension():
    schema = xsd.Schema(
        load_xml(
            """
            <xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                    xmlns:tns="%s"
                    elementFormDefault="qualified"
                    targetNamespace="%s">
              %s
            </xsd:schema>
            """
            % (NS, NS, PREFIXED_CHOICE)
        ),
        settings=Settings(strict=False),
    )
    element = schema.get_element("ns0:container")

    value = element.parse(
        load_xml(
            """
            <ns0:container xmlns:ns0="%s">
              <ns0:item_1>a</ns0:item_1>
              <ns0:item_2>b</ns0:item_2>
              <ns0:extra>z</ns0:extra>
            </ns0:container>
            """
            % NS
        ),
        schema,
    )

    raw = value.__values__["_raw_elements"]
    assert etree.QName(raw[0].tag).localname == "extra"


def test_optional_choice_is_skipped_when_no_branch_matches():
    schema = _schema(
        """
        <xsd:element name="container">
          <xsd:complexType>
            <xsd:sequence>
              <xsd:choice minOccurs="0">
                <xsd:element name="item_1" type="xsd:string"/>
                <xsd:element name="item_2" type="xsd:string"/>
              </xsd:choice>
              <xsd:element name="item_4" type="xsd:string"/>
            </xsd:sequence>
          </xsd:complexType>
        </xsd:element>
        """
    )
    element = schema.get_element("ns0:container")

    skipped = element.parse(
        load_xml(
            """
            <ns0:container xmlns:ns0="%s">
              <ns0:item_4>foo</ns0:item_4>
            </ns0:container>
            """
            % NS
        ),
        schema,
    )
    assert skipped.item_4 == "foo"

    matched = element.parse(
        load_xml(
            """
            <ns0:container xmlns:ns0="%s">
              <ns0:item_2>x</ns0:item_2>
              <ns0:item_4>y</ns0:item_4>
            </ns0:container>
            """
            % NS
        ),
        schema,
    )
    assert matched.item_2 == "x"
    assert matched.item_4 == "y"


def test_render_explicit_nil_value_object():
    schema = _schema(
        """
        <xsd:element name="doc">
          <xsd:complexType>
            <xsd:sequence>
              <xsd:element name="a" type="xsd:string"/>
              <xsd:element name="b" type="xsd:string" nillable="true"/>
            </xsd:sequence>
          </xsd:complexType>
        </xsd:element>
        """
    )
    element = schema.get_element("ns0:doc")
    value = element(a="1", b=Nil)
    node = render_node(element, value)
    assert_nodes_equal(
        node,
        """
        <document>
          <ns0:doc xmlns:ns0="%s">
            <ns0:a>1</ns0:a>
            <ns0:b xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>
          </ns0:doc>
        </document>
        """
        % NS,
    )
