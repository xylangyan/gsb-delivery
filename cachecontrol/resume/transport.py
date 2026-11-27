# SPDX-FileCopyrightText: 2026
#
# SPDX-License-Identifier: Apache-2.0

"""Transport boundary for resumable downloads.

The resumable cache never touches :mod:`http.client` or a socket itself. It
talks to a narrow :class:`Transport` protocol. Two implementations ship here:

* :class:`InProcessTransport` -- a fully local stand-in that dispatches to
  in-memory handlers. It is what the test suite uses to interleave two
  distribution zones, two ``Vary`` declarations, delayed validators and
  interrupted bodies without binding a port.
* :class:`RequestsTransport` -- a thin bridge onto a real :mod:`requests`
  session for production use.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import urlsplit

from requests.structures import CaseInsensitiveDict

from cachecontrol.resume.http import HttpResponse


class TransportError(Exception):
    """Base class for failures reported by a :class:`Transport`."""


class IncompleteTransfer(TransportError):
    """Raised when a response body ends before ``Content-Length`` is met.

    This models a half-closed connection: the headers (and possibly some body
    bytes) were observed, but the transfer was cut short. A resumable cache
    must treat the trailing, unterminated segment as not covered.
    """

    def __init__(self, message: str, *, partial: bytes = b"") -> None:
        super().__init__(message)
        self.partial = partial


class Interrupt(Exception):
    """A stand-in handler may raise this to simulate a dropped connection."""

    def __init__(self, message: str = "connection reset", *, partial: bytes = b""):
        super().__init__(message)
        self.partial = partial


class IncomingRequest:
    """The normalized request handed to an in-process handler."""

    __slots__ = ("method", "url", "path", "headers")

    def __init__(self, method: str, url: str, headers: CaseInsensitiveDict[str]):
        self.method = method
        self.url = url
        split = urlsplit(url)
        self.path = split.path + (f"?{split.query}" if split.query else "")
        self.headers = headers

    def header(self, name: str) -> str | None:
        return self.headers.get(name)


Handler = Callable[[IncomingRequest], HttpResponse]


@runtime_checkable
class Transport(Protocol):
    """The minimal surface the resumable cache depends on."""

    def send(
        self, method: str, url: str, headers: dict[str, str] | None = ...
    ) -> HttpResponse: ...

    def close(self) -> None: ...


class InProcessTransport:
    """An in-memory :class:`Transport` backed by registered handlers.

    Handlers are keyed by the URL's ``path?query``. Every request is appended
    to :attr:`log` before dispatch, so a test can reconstruct exactly which
    request produced each stored byte range and which conditional validators
    are still in force.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self.log: list[IncomingRequest] = []

    def handle(self, path: str, handler: Handler) -> None:
        """Register (or replace) the handler for ``path``."""
        self._handlers[path] = handler

    def send(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        request = IncomingRequest(
            method.upper(), url, CaseInsensitiveDict(headers or {})
        )
        self.log.append(request)
        handler = self._handlers.get(request.path)
        if handler is None:
            return HttpResponse(404, {}, b"")
        try:
            response = handler(request)
        except Interrupt as interrupted:
            raise IncompleteTransfer(
                str(interrupted), partial=interrupted.partial
            ) from interrupted
        return response

    def close(self) -> None:
        self._handlers.clear()

    # -- introspection helpers used by the warehouse/auditor ----------------

    def requests_for(self, path: str) -> list[IncomingRequest]:
        return [request for request in self.log if request.path == path]


class RequestsTransport:
    """Drive a real :mod:`requests` session through the same protocol.

    The whole-body, non-resumable code path in :mod:`cachecontrol.adapter`
    is unchanged; this bridge exists so the resumable cache can run over real
    HTTP when an application explicitly opts in.
    """

    def __init__(self, session: Any | None = None) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    def send(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        try:
            response = self._session.request(
                method, url, headers=headers or {}, stream=True
            )
        except Exception as exc:  # pragma: no cover - exercised over real I/O
            raise TransportError(str(exc)) from exc

        try:
            body = response.content
        except Exception as exc:  # pragma: no cover - exercised over real I/O
            raise IncompleteTransfer(str(exc)) from exc

        return HttpResponse(
            response.status_code,
            [(key, value) for key, value in response.headers.items()],
            body,
        )

    def close(self) -> None:
        self._session.close()
