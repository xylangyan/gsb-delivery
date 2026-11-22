from zeep.settings import Settings


class XmlParserContext:
    """Parser context when parsing XML elements"""

    def __init__(self, settings=None, message_pack=None):
        self.schemas = []
        self.settings = settings or Settings()
        # Optional LazyMessagePack used to resolve xop:Include references
        # lazily during deserialization of an MTOM response.
        self.message_pack = message_pack

    def resolve_xop(self, xmlelement):
        """Resolve a (possibly XOP referencing) element to a lazy binary.

        Returns ``None`` when the element does not contain an ``xop:Include``
        node or when no message pack is attached to the context.
        """
        if self.message_pack is None:
            return None

        from zeep.wsdl.lazy import find_xop_includes

        includes = find_xop_includes(xmlelement)
        if not includes:
            return None

        href = includes[0].get("href")
        return self.message_pack.open(href)
