"""LDAP protocol server, which acts as a proxy which
forwards the requests to multiple LDAP servers and
merges the results.
Only Bind and Search requests are supported.

Plain searches keep the historical merge behaviour.  A search that
carries the RFC 2696 paged-results control is served by the versioned
roster engine in :mod:`ldaptor.protocols.ldap.roster`: every directory is
read in ascending personnel-number order, pages are only released once
all directories have moved past them, and continuation state belongs to
the query and the client connection that created it.
"""

from twisted.internet import reactor, defer
from twisted.python import log

from ldaptor.protocols.ldap import ldapclient, ldapconnector
from ldaptor.protocols.ldap import ldapserver, roster
from ldaptor.protocols.ldap import ldaperrors
from ldaptor.protocols import pureldap
from ldaptor.config import LDAPConfig


class MergedLDAPServer(ldapserver.BaseLDAPServer):
    protocol = ldapclient.LDAPClient

    def __init__(
        self,
        configs,
        use_tls,
        source_names=None,
        primary_source=None,
        number_attribute=roster.DEFAULT_NUMBER_ATTRIBUTE,
        pager=None,
    ):
        ldapserver.BaseLDAPServer.__init__(self)
        self.clients = []
        self.configs = configs
        self.use_tls = use_tls
        self.all_connected = False
        # merge_map[id] = {"expected": N, "responses": [...]}
        # expected is the number of backends still owed a response for this
        # request; it is decremented on each response AND on each backend
        # disconnect, so a dead backend can no longer hang the search (#231).
        self.merge_map = {}
        self.waitingConnect = []
        self.unbound = False

        if source_names is None:
            source_names = ["source-%d" % index for index in range(len(configs))]
        self.source_names = list(source_names)
        self.primary_source = (
            primary_source
            if primary_source is not None
            else (self.source_names[0] if self.source_names else None)
        )
        if self.primary_source not in self.source_names:
            raise ValueError("primary_source must be one of source_names")
        self.number_attribute = roster.number_text(number_attribute)
        self._pager = pager
        self._client_sources = {}
        self._lost_sources = set()

    @property
    def pager(self):
        """Roster query state, shared by every connection to one factory.

        The protocol instance represents one client connection, but a
        roster query must survive that connection so the client can
        resume on the same version.  When a factory is present, its
        ``roster_pager`` attribute holds the shared state; a standalone
        protocol (as used in unit tests) falls back to its own pager.
        """
        if self._pager is not None:
            return self._pager
        factory = getattr(self, "factory", None)
        if factory is not None:
            pager = getattr(factory, "roster_pager", None)
            if pager is None:
                pager = roster.RosterPager()
                factory.roster_pager = pager
            self._pager = pager
            return pager
        self._pager = roster.RosterPager()
        return self._pager

    def _whenConnected(self, fn, *a, **kw):
        if not self.all_connected:
            d = defer.Deferred()
            self.waitingConnect.append((d, fn, a, kw))
            return d
        else:
            return defer.maybeDeferred(fn, *a, **kw)

    def _failConnection(self, reason):
        self.transport.loseConnection()
        raise ldaperrors.LDAPOther(f"Cannot connect to server.{reason}")

    def _cbConnectionMade(self, proto, source=None):
        self.clients.append(proto)
        if source is not None:
            self._client_sources[proto] = source
            self._lost_sources.discard(source)
        proto.notifyOnDisconnect().addBoth(self._cbClientLost, proto)

        if len(self.clients) == len(self.configs):
            self.all_connected = True

        # Only call once when all clients are connected.
        if self.all_connected:
            while self.waitingConnect:
                d, fn, a, kw = self.waitingConnect.pop(0)
                d2 = defer.maybeDeferred(fn, *a, **kw)
                d2.chainDeferred(d)

    def _cbClientLost(self, reason, proto):
        """A backend went away; stop expecting responses from it."""
        source = self._client_sources.pop(proto, None)
        if proto in self.clients:
            self.clients.remove(proto)
        if source is not None and not self.unbound:
            # A teardown we initiated (client EOF/unbind) is not proof
            # that the directory lost a snapshot.  A reconnect can still
            # resume the same version; the directory itself decides
            # whether that version is still available.
            self._lost_sources.add(source)
            self._roster_source_lost(source, reason="directory connection lost")
        for id_ in list(self.merge_map.keys()):
            self.merge_map[id_]["expected"] -= 1
            self._checkComplete(id_)

    def _roster_source_lost(self, source, reason):
        for query in list(self.pager.queries.values()):
            query.mark_source_lost(source, reason=reason)

    def _clientQueue(self, request, controls, reply):
        # Controls are ignored.
        for c in list(self.clients):
            if not c.connected:
                continue
            try:
                if request.needs_answer:
                    d = c.send_multiResponse(request, self._gotResponse, reply)
                    d.addErrback(defer.logError)
                else:
                    c.send_noResponse(request)
            except ldapclient.LDAPClientConnectionLostException:
                # Client died between the .connected check and the send;
                # the notifyOnDisconnect callback will prune it.
                continue

    def _checkComplete(self, id):
        entry = self.merge_map.get(id)
        if entry is None:
            return
        if len(entry["responses"]) < entry["expected"]:
            return
        del self.merge_map[id]
        if entry["responses"]:
            # Send success, if at least one success; otherwise the last.
            op = entry["responses"][-1]
            for r in entry["responses"]:
                if r.resultCode == ldaperrors.Success.resultCode:
                    op = r
                    break
        else:
            # All backends disconnected before any response arrived.
            op = pureldap.LDAPSearchResultDone(
                resultCode=ldaperrors.LDAPOther.resultCode,
                errorMessage=b"All backend LDAP connections lost",
            )
        ldapserver.BaseLDAPServer.queue(self, id, op)

    def queue(self, id, op, controls=None):
        if controls is not None:
            # Roster pages carry their own controls and are already a
            # complete, final answer for the request.  Do not fold them
            # into the legacy per-backend merge accounting.
            ldapserver.BaseLDAPServer.queue(self, id, op, controls)
            return
        if isinstance(op, (pureldap.LDAPSearchResultDone, pureldap.LDAPBindResponse)):
            if id not in self.merge_map:
                # Size to the currently live backend count so a dead
                # backend never blocks completion.
                live = sum(1 for c in self.clients if c.connected)
                self.merge_map[id] = {"expected": live, "responses": []}
            self.merge_map[id]["responses"].append(op)
            self._checkComplete(id)
        else:
            ldapserver.BaseLDAPServer.queue(self, id, op)

    def connectionMade(self):
        clientCreator = ldapconnector.LDAPClientCreator(reactor, self.protocol)
        for index, (c, tls) in enumerate(zip(self.configs, self.use_tls)):
            source = self.source_names[index]
            d = clientCreator.connect(dn="", overrides=c.getServiceLocationOverrides())
            if tls:
                d.addCallback(lambda x: x.startTLS())
            d.addCallback(self._cbConnectionMade, source=source)
            d.addErrback(self._failConnection)

        ldapserver.BaseLDAPServer.connectionMade(self)

    def connectionLost(self, reason):
        self.pager.connection_lost(id(self))
        # Iterate a snapshot: _cbClientLost mutates self.clients.
        for c in list(self.clients):
            assert c is not None
            if c.connected:
                if not self.unbound:
                    c.unbind()
                else:
                    c.transport.loseConnection()

        self.clients = []
        self.merge_map = {}
        self.unbound = True
        ldapserver.BaseLDAPServer.connectionLost(self, reason)

    def _gotResponse(self, response, reply):
        reply(response)

        # TODO this is ugly
        return isinstance(
            response,
            (
                pureldap.LDAPSearchResultDone,
                pureldap.LDAPBindResponse,
            ),
        )

    def _handleUnknown(self, request, controls, reply):
        self._whenConnected(self._clientQueue, request, controls, reply)
        return None

    def handleUnknown(self, request, controls, reply):
        d = defer.succeed(request)
        d.addCallback(self._handleUnknown, controls, reply)
        return d

    def handle_LDAPBindRequest(self, request, controls, reply):
        return self.handleUnknown(request, controls, reply)

    def handle_LDAPSearchRequest(self, request, controls, reply):
        paged = roster.decode_paged_results_control(controls)
        if paged is None:
            return self.handleUnknown(request, controls, reply)
        return self._handle_roster_search(request, controls, reply, paged)

    # -- paged roster searches ------------------------------------------

    def _handle_roster_search(self, request, controls, reply, paged):
        page_size, cookie = paged
        if page_size < 1:
            raise ldaperrors.LDAPProtocolError(
                "roster paging requires a positive page size"
            )

        resume = roster.decode_resume_control(controls)
        key = roster.make_query_key(request)
        connection_id = id(self)

        if cookie:
            query = self.pager.consume_token(cookie, connection_id)
            if query.key != key:
                raise ldaperrors.LDAPProtocolError(
                    "continuation cookie names a different search"
                )
        elif resume is not None:
            query_id, version, confirmed_after = resume
            try:
                query = self.pager.get(query_id)
            except roster.InvalidContinuationToken:
                query = None
            if query is not None and query.key != key:
                raise ldaperrors.LDAPProtocolError(
                    "roster resume control names a different search"
                )
            if query is None:
                query = self.pager.find_resumable(key, version)
            if query is not None:
                query.prepare_resume()
                query.rebind_sources()
            elif confirmed_after is not None:
                # Resume was requested but the pinned query is gone.
                # Starting a fresh version here would silently mix
                # roster versions, so reject it instead.
                raise ldaperrors.LDAPProtocolError(
                    "roster query %r is no longer available; it cannot be "
                    "resumed at %r"
                    % (query_id, confirmed_after)
                )
            else:
                query = self.pager.new_query(
                    key=key,
                    page_size=page_size,
                    source_names=self.source_names,
                    primary_source=self.primary_source,
                    version=version,
                )
        else:
            query = self.pager.new_query(
                key=key,
                page_size=page_size,
                source_names=self.source_names,
                primary_source=self.primary_source,
            )
        query.page_size = page_size

        def _done(result):
            self._send_roster_page(request, query, result, reply)
            query.mark_served(result.entries)
            return None

        d = self._drive_roster_query(request, query)
        d.addCallback(_done)
        d.addErrback(self._roster_error, reply)
        return None

    def _drive_roster_query(self, request, query):
        """Alternate page building and per-source fetching.

        Bounded so a misbehaving directory cannot keep the client waiting
        forever; a source that makes no progress is marked lost by the
        engine and surfaced as an unsettled range.
        """

        def _loop(remaining):
            result = query.build_page()
            if not result.need or remaining <= 0:
                return result
            d = self._fetch_roster_batches(request, query, result.need)
            d.addCallback(lambda _: _loop(remaining - 1))
            return d

        return defer.maybeDeferred(_loop, 1024)

    def _fetch_roster_batches(self, request, query, sources):
        deferreds = []
        for source in sources:
            state = query.source_state(source)
            if state.lost:
                continue
            d = self._fetch_roster_batch(request, query, state)
            d.addErrback(self._roster_batch_failed, query, source)
            deferreds.append(d)
        return defer.DeferredList(deferreds, consumeErrors=True)

    def _roster_batch_failed(self, failure, query, source):
        query.mark_source_lost(
            source, reason="directory request failed: %s" % failure.getErrorMessage()
        )
        return None

    def _source_client(self, source):
        for client in self.clients:
            if self._client_sources.get(client) == source and client.connected:
                return client
        return None

    def _fetch_roster_batch(self, request, query, state):
        client = self._source_client(state.name)
        if client is None:
            query.mark_source_lost(
                state.name, reason="directory connection lost"
            )
            return defer.succeed(None)

        mode = state.mode
        after = None if mode == "native" else state.cursor
        filter_object = self._roster_filter(request, after)
        attributes = [self.number_attribute]
        for attr in request.attributes:
            attr = roster.to_bytes(attr)
            if attr not in attributes and attr not in (b"*", b"1.1"):
                attributes.append(attr)

        # Fetch a little beyond the client page so a full response is
        # an unambiguous "more may follow" signal for directories
        # without a watermark extension.
        limit = max(query.page_size + 1, 64)
        version = state.version or query.version
        if mode == "native":
            # RFC 2696: the filter is fixed for the life of the search
            # and the directory's own cookie advances the cursor.
            controls = [roster.encode_paged_results_control(limit, state.cookie)]
        elif mode == "plain":
            # A directory that knows neither extension can only be read
            # with a fresh filter, so the cursor is exclusive.
            controls = []
        else:
            # Unknown or roster-aware: negotiate with this module's
            # source control as well, so the directory can report the
            # next lower bound and its page end explicitly.
            controls = [
                roster.encode_paged_results_control(limit, b""),
                roster.encode_source_request_control(version, after, limit),
            ]

        op = pureldap.LDAPSearchRequest(
            baseObject=request.baseObject,
            scope=request.scope,
            derefAliases=request.derefAliases,
            sizeLimit=0,
            timeLimit=request.timeLimit,
            typesOnly=request.typesOnly,
            filter=filter_object,
            attributes=attributes,
        )

        records = []
        collected = {}
        finished = []
        finished_d = defer.Deferred()

        def _finish():
            if finished:
                return
            finished.append(True)
            if "done" not in collected:
                query.mark_source_lost(
                    state.name, reason="directory ended without a result"
                )
            else:
                page = self._roster_page(
                    state,
                    records,
                    collected.get("controls"),
                    limit,
                    after,
                )
                query.feed(page)
            if not finished_d.called:
                finished_d.callback(None)

        def _handler(msg, response_controls):
            if isinstance(msg, pureldap.LDAPSearchResultEntry):
                records.append(self._roster_record(state.name, msg))
                return False
            if isinstance(msg, pureldap.LDAPSearchResultDone):
                collected["done"] = msg
                collected["controls"] = response_controls
                if msg.resultCode != ldaperrors.Success.resultCode:
                    raise ldaperrors.get(msg.resultCode, msg.errorMessage)
                _finish()
                return True
            return False

        d = client.send_multiResponse_ex(op, controls, _handler)

        def _cb(_):
            _finish()
            return None

        def _eb(failure):
            if not finished_d.called:
                finished_d.errback(failure)
            return None

        d.addCallbacks(_cb, _eb)
        return finished_d

    def _roster_record(self, source, msg):
        attributes = []
        number = None
        for name, values in msg.attributes:
            name = roster.to_bytes(name)
            values = tuple(roster.to_bytes(value) for value in values)
            attributes.append((name, values))
            if (
                number is None
                and name.lower() == self.number_attribute.lower()
                and values
            ):
                number = values[0]
        if number is None:
            raise roster.RosterProtocolError(
                "directory %s returned an entry without %s"
                % (
                    source,
                    self.number_attribute.decode("ascii", "replace"),
                )
            )
        return roster.RosterRecord(
            number=roster.number_text(number),
            object_name=roster.to_bytes(msg.objectName),
            attributes=tuple(attributes),
            source=source,
        )

    def _roster_page(self, state, records, response_controls, limit, after):
        parsed = roster.decode_source_response_control(response_controls)
        paged = roster.decode_paged_results_control(response_controls)

        if parsed is not None:
            version, next_number, ended = parsed
        else:
            version = state.version
            if paged is not None:
                # RFC 2696: the directory's own cookie advances pages,
                # and an empty cookie marks the last page.
                _size, state.cookie = paged
                state.mode = "native"
                ended = not state.cookie
            elif state.mode == "plain" or (
                state.mode is None and len(records) < limit
            ):
                # A directory that knows neither extension can only be
                # read by filtering above its last number.  A short page
                # therefore ends the range.
                state.mode = "plain"
                ended = len(records) < limit
            else:
                state.mode = "plain"

            if records:
                next_number = records[-1].number
            else:
                next_number = after or state.cursor

        if parsed is not None:
            if state.mode is None:
                state.mode = "custom"
            elif state.mode not in ("custom", "plain"):
                raise roster.RosterProtocolError(
                    "directory %s changed its paging protocol" % state.name
                )
            state.mode = "custom"
            if (
                version is not None
                and state.version is not None
                and roster.to_bytes(version) != state.version
            ):
                raise roster.SnapshotLost(
                    "directory %s no longer has version %r (got %r)"
                    % (
                        state.name,
                        state.version.decode("utf-8", "replace"),
                        roster.to_bytes(version).decode("utf-8", "replace"),
                    )
                )

        if version is not None:
            version = roster.to_bytes(version)
            if state.version is None:
                state.version = version
            elif state.version != version:
                raise roster.SnapshotLost(
                    "directory %s no longer has version %r"
                    % (state.name, state.version)
                )

        return roster.SourcePage(
            source=state.name,
            records=tuple(records),
            next_number=None if ended else next_number,
            ended=ended,
            version=version,
        )

    def _roster_filter(self, request, after):
        if after is None:
            return request.filter
        bound = pureldap.LDAPFilter_greaterOrEqual(
            attributeDesc=pureldap.LDAPAttributeDescription(
                self.number_attribute
            ),
            assertionValue=pureldap.LDAPAssertionValue(
                roster.number_text(after)
            ),
        )
        return pureldap.LDAPFilter_and([request.filter, bound])

    def _roster_error(self, failure, reply):
        failure.printTraceback()
        reply(
            pureldap.LDAPSearchResultDone(
                resultCode=ldaperrors.LDAPOther.resultCode,
                errorMessage=b"roster paging failed",
            )
        )
        return None

    def _send_roster_page(self, request, query, result, reply):
        requested = [roster.to_bytes(attr) for attr in request.attributes]
        for entry in result.entries:
            attributes = []
            for name, values in entry.attributes:
                if (
                    not requested
                    or b"*" in requested
                    or name in requested
                    or name in roster.RESERVED_ATTRIBUTES
                ):
                    attributes.append((name, list(values)))
            reply(
                pureldap.LDAPSearchResultEntry(
                    objectName=entry.object_name,
                    attributes=attributes,
                )
            )

        token = b""
        if not result.complete and not result.blocked:
            token = self.pager.issue_token(query, id(self))
        controls = [
            roster.encode_paged_results_control(0, token),
            roster.encode_state_control(query),
        ]
        if result.unsettled:
            controls.append(roster.encode_unsettled_control(result.unsettled))
        reply(
            ldapserver.ResponseWithControls(
                pureldap.LDAPSearchResultDone(
                    resultCode=ldaperrors.Success.resultCode
                ),
                controls,
            )
        )

    def handle_LDAPUnbindRequest(self, request, controls, reply):
        self.unbound = True
        self.handleUnknown(request, controls, reply)

    fail_LDAPDelRequest = pureldap.LDAPDelResponse

    def handle_LDAPDelRequest(self, request, controls, reply):
        raise ldaperrors.LDAPUnwillingToPerform()

    fail_LDAPAddRequest = pureldap.LDAPAddResponse

    def handle_LDAPAddRequest(self, request, controls, reply):
        raise ldaperrors.LDAPUnwillingToPerform()

    fail_LDAPModifyDNRequest = pureldap.LDAPModifyDNResponse

    def handle_LDAPModifyDNRequest(self, request, controls, reply):
        raise ldaperrors.LDAPUnwillingToPerform()

    fail_LDAPModifyRequest = pureldap.LDAPModifyResponse

    def handle_LDAPModifyRequest(self, request, controls, reply):
        raise ldaperrors.LDAPUnwillingToPerform()

    fail_LDAPExtendedRequest = pureldap.LDAPExtendedResponse

    def handle_LDAPExtendedRequest(self, request, controls, reply):
        raise ldaperrors.LDAPUnwillingToPerform()


if __name__ == "__main__":
    from twisted.internet import protocol
    from twisted.python import log
    import sys

    log.startLogging(sys.stderr)

    configs = [
        LDAPConfig(serviceLocationOverrides={"": ("localhost", 38942)}),
        LDAPConfig(serviceLocationOverrides={"": ("localhost", 8080)}),
    ]
    use_tls = [True, False]
    factory = protocol.ServerFactory()
    factory.protocol = lambda: MergedLDAPServer(configs, use_tls)
    reactor.listenTCP(10389, factory)
    reactor.run()
