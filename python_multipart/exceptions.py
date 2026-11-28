class FormParserError(ValueError):
    """Base error class for our form parser."""


class ParseError(FormParserError):
    """This exception (or a subclass) is raised when there is an error while
    parsing something.
    """

    def __init__(self, message: str, *, offset: int = -1) -> None:
        super().__init__(message)
        self.offset = offset


class MultipartParseError(ParseError):
    """This is a specific error that is raised when the MultipartParser detects
    an error while parsing.
    """


class QuerystringParseError(ParseError):
    """This is a specific error that is raised when the QuerystringParser
    detects an error while parsing.
    """


class DecodeError(ParseError):
    """This exception is raised when there is a decoding error - for example
    with the Base64Decoder or QuotedPrintableDecoder.
    """


class ProvenanceError(FormParserError):
    """Raised when a provenance run cannot be settled - for example when the
    outer multipart message never closes and provisional parts therefore
    cannot be signed off.
    """


class ProvenanceDecodeError(DecodeError):
    """Raised by the strict provenance-aware decoders when an encoded layer
    contains bytes that violate its encoding rules.
    """


class FileError(FormParserError, OSError):
    """Exception class for problems with the File class."""
