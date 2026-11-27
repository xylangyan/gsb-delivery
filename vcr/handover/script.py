"""Offline request scripts for handover tests and dry runs.

A script walks only the finite set of declared page turns and retry paths from
:class:`~vcr.handover.config.Permissions`; it never opens a socket and never
imports an HTTP client, so a handover flow can be exercised fully offline.
"""

from .config import HandoverConfigError


class OfflineScript:
    """A finite, declarative sequence of fetch actions.

    Each action is a ``(page_turn, request)`` tuple; ``page_turn`` must be one
    of the permissions' declared page turns. A retry is an action whose
    ``page_turn`` was declared as a retry path.
    """

    def __init__(self, config, actions):
        self.config = config
        permissions = config.permissions
        if not permissions.page_turns:
            raise HandoverConfigError(
                "an offline script needs a finite, declared set of page turns; "
                "set page_turns on the handover permissions",
            )
        for page_turn, _request in actions:
            if page_turn in permissions.retries:
                permissions.validate_retry(page_turn)
            else:
                permissions.validate_page_turn(page_turn)
        self._actions = list(actions)
        self._index = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._index >= len(self._actions):
            raise StopIteration
        page_turn, request = self._actions[self._index]
        self._index += 1
        return page_turn, request

    def run(self, cassette):
        """Play each action against a cassette, fully offline.

        Returns the list of ``(page_turn, response)`` results. Requests the
        cassette can't play are recorded through the cassette's normal
        append path, which in handover mode writes only the sidecar book.
        """
        results = []
        for page_turn, request in self:
            if cassette.can_play_response_for(request):
                response = cassette.play_response(request)
            else:
                if cassette.write_protected:
                    raise HandoverConfigError(
                        f"offline script page turn {page_turn!r} has no recorded response and the "
                        "cassette is write-protected; the script cannot reach an external service",
                    )
                response = self._canned_response(page_turn, request)
                cassette.append(request, response)
            results.append((page_turn, response))
        return results

    @staticmethod
    def _canned_response(page_turn, request):
        return {
            "status": {"code": 200, "message": "OK"},
            "headers": {"X-Page-Turn": [page_turn]},
            "body": {"string": f"offline response for {request.uri}".encode("utf-8")},
        }
