from twisted.internet import error
from ldaptor import config, testutil
from ldaptor.protocols.ldap import ldaperrors, ldapserver
from ldaptor.protocols.pureldap import (
    LDAPBindResponse,
    LDAPBindRequest,
    LDAPUnbindRequest,
    LDAPSearchResultEntry,
    LDAPMessage,
    LDAPSearchRequest,
    LDAPSearchResultDone,
    LDAPAddRequest,
    LDAPAddResponse,
    LDAPDelRequest,
    LDAPDelResponse,
    LDAPModifyRequest,
    LDAPModifyResponse,
    LDAPModifyDNRequest,
    LDAPModifyDNResponse,
    LDAPExtendedRequest,
    LDAPExtendedResponse,
)
from twisted.test import proto_helpers
from twisted.trial import unittest

from ldaptor.protocols.ldap.merger import MergedLDAPServer
from ldaptor.protocols.ldap import roster
from ldaptor.protocols import pureber


class MergedLDAPServerTest(unittest.TestCase):
    def createMergedServer(self, *responses):
        """
        Create an MergedLDAP server for testing. Initialize with
        len(responses) clients.
        :param responses: The responses to initialize the `LDAPClientTestDrives`.
        :type responses: args of lists of lists
        :return a deferred, fires when server finished connecting
        """

        def createClient(factory):
            factory.doStart()
            proto = factory.buildProtocol(addr=None)
            proto.connectionMade()

        clients = []
        for r in responses:
            clients.append(testutil.LDAPClientTestDriver(*r))

        conf = config.LDAPConfig(serviceLocationOverrides={"": createClient})
        server = MergedLDAPServer([conf for _ in clients], [False for _ in clients])
        self.clients = clients * 1
        server.protocol = lambda: clients.pop()
        server.transport = proto_helpers.StringTransport()
        server.connectionMade()

        d = server._whenConnected(lambda: server)
        return d

    def createRosterServer(self, *responses, source_names=None):
        clients = [testutil.LDAPClientTestDriver(*r) for r in responses]

        def createClient(factory):
            factory.doStart()
            proto = factory.buildProtocol(addr=None)
            proto.connectionMade()

        conf = config.LDAPConfig(serviceLocationOverrides={"": createClient})
        server = MergedLDAPServer(
            [conf for _ in clients],
            [False for _ in clients],
            source_names=source_names,
            primary_source=source_names[0] if source_names else None,
        )
        pending = list(clients)
        server.protocol = lambda: pending.pop(0)
        server.transport = proto_helpers.StringTransport()
        server.connectionMade()
        return server._whenConnected(lambda: server)

    def decodeMessages(self, data):
        return pureber.berDecodeMultiple(
            data, ldapserver.BaseLDAPServer.berdecoder
        )

    def test_roster_paged_search_is_ordered_and_merged(self):
        d = self.createRosterServer(
            [
                [
                    LDAPSearchResultEntry(
                        "uid=001,primary",
                        [
                            ("employeeNumber", ["001"]),
                            ("cn", ["primary-name"]),
                        ],
                    ),
                    LDAPSearchResultEntry(
                        "uid=002,primary",
                        [("employeeNumber", ["002"]), ("cn", ["two"])],
                    ),
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
            [
                [
                    LDAPSearchResultEntry(
                        "uid=001,secondary",
                        [
                            ("employeeNumber", ["001"]),
                            ("cn", ["secondary-name"]),
                        ],
                    ),
                    LDAPSearchResultEntry(
                        "uid=003,secondary",
                        [("employeeNumber", ["003"]), ("cn", ["three"])],
                    ),
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
            source_names=["primary", "secondary"],
        )

        def test_f(server):
            controls = [roster.encode_paged_results_control(10)]
            server.dataReceived(
                LDAPMessage(
                    LDAPSearchRequest(baseObject="dc=example,dc=com"),
                    id=3,
                    controls=controls,
                ).toWire()
            )

            def deliver(messages):
                entries = [
                    message
                    for message in messages
                    if isinstance(message.value, LDAPSearchResultEntry)
                ]
                self.assertEqual(
                    [message.value.objectName for message in entries],
                    [
                        b"uid=001,primary",
                        b"uid=002,primary",
                        b"uid=003,secondary",
                    ],
                )
                merged = dict(entries[0].value.attributes)
                self.assertEqual(merged[b"cn"], [b"primary-name"])
                self.assertEqual(
                    merged[b"rosterSource"], [b"primary", b"secondary"]
                )

                done = [
                    message
                    for message in messages
                    if isinstance(message.value, LDAPSearchResultDone)
                ]
                self.assertEqual(len(done), 1)
                self.assertEqual(
                    done[0].value.resultCode,
                    ldaperrors.Success.resultCode,
                )

            def check():
                try:
                    deliver(self.decodeMessages(server.transport.value()))
                except pureber.BERExceptionInsufficientData:
                    return False
                return True

            from twisted.internet import task

            return task.deferLater(
                __import__("twisted.internet.reactor", fromlist=["reactor"]),
                0,
                check,
            )

        d.addCallback(test_f)
        return d

    def test_plain_search_still_uses_legacy_merge(self):
        d = self.createRosterServer(
            [
                [
                    LDAPSearchResultEntry("cn=a,dc=example", [("a", ["b"])]),
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
            [
                [
                    LDAPSearchResultEntry("cn=b,dc=example", [("b", ["c"])]),
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
            source_names=["primary", "secondary"],
        )

        def test_f(server):
            server.dataReceived(
                LDAPMessage(
                    LDAPSearchRequest(baseObject="dc=example,dc=com"), id=4
                ).toWire()
            )
            messages = self.decodeMessages(server.transport.value())
            self.assertEqual(
                [
                    message.value.objectName
                    for message in messages
                    if isinstance(message.value, LDAPSearchResultEntry)
                ],
                [b"cn=a,dc=example", b"cn=b,dc=example"],
            )
            self.assertEqual(
                len(
                    [
                        message
                        for message in messages
                        if isinstance(message.value, LDAPSearchResultDone)
                    ]
                ),
                1,
            )

        d.addCallback(test_f)
        return d

    def test_roster_page_size_limits_each_page(self):
        def source_done(next_number, ended=False):
            return (
                LDAPSearchResultDone(ldaperrors.Success.resultCode),
                [
                    roster.encode_source_response_control(
                        b"v1",
                        next_number,
                        ended,
                    )
                ],
            )

        d = self.createRosterServer(
            [
                [
                    LDAPSearchResultEntry(
                        "uid=001,p", [("employeeNumber", ["001"])]
                    ),
                    LDAPSearchResultEntry(
                        "uid=002,p", [("employeeNumber", ["002"])]
                    ),
                    source_done(b"002"),
                ],
                [
                    LDAPSearchResultEntry(
                        "uid=002,p", [("employeeNumber", ["002"])]
                    ),
                    source_done(b"003"),
                ],
            ],
            [
                [
                    LDAPSearchResultEntry(
                        "uid=003,s", [("employeeNumber", ["003"])]
                    ),
                    source_done(b"003"),
                ],
                [
                    LDAPSearchResultEntry(
                        "uid=003,s", [("employeeNumber", ["003"])]
                    ),
                    source_done(b"004"),
                ],
            ],
            source_names=["primary", "secondary"],
        )

        def test_f(server):
            server.dataReceived(
                LDAPMessage(
                    LDAPSearchRequest(baseObject="dc=example,dc=com"),
                    id=7,
                    controls=[roster.encode_paged_results_control(1)],
                ).toWire()
            )

            def parse():
                messages = self.decodeMessages(server.transport.value())
                entries = [
                    message
                    for message in messages
                    if isinstance(message.value, LDAPSearchResultEntry)
                ]
                done = [
                    message
                    for message in messages
                    if isinstance(message.value, LDAPSearchResultDone)
                ]
                return entries, done

            def check():
                entries, done = parse()
                if not done:
                    return False
                self.assertEqual(
                    [entry.value.objectName for entry in entries],
                    [b"uid=001,p"],
                )
                size, cookie = roster.decode_paged_results_control(
                    done[0].controls
                )
                self.assertEqual(size, 0)
                self.assertTrue(cookie)

                server.transport.clear()
                server.dataReceived(
                    LDAPMessage(
                        LDAPSearchRequest(baseObject="dc=example,dc=com"),
                        id=8,
                        controls=[
                            roster.encode_paged_results_control(1, cookie)
                        ],
                    ).toWire()
                )
                entries, done = parse()
                self.assertEqual(
                    [entry.value.objectName for entry in entries],
                    [b"uid=002,p"],
                )
                self.assertEqual(len(done), 1)
                return True

            from twisted.internet import task, reactor

            return task.deferLater(reactor, 0, check)

        d.addCallback(test_f)
        return d

    def test_roster_reconnect_resumes_after_confirmed_number(self):
        def source_done(next_number):
            return (
                LDAPSearchResultDone(ldaperrors.Success.resultCode),
                [
                    roster.encode_source_response_control(
                        b"v1",
                        next_number,
                        False,
                    )
                ],
            )

        d = self.createRosterServer(
            [
                [
                    LDAPSearchResultEntry(
                        "uid=001,p", [("employeeNumber", ["001"])]
                    ),
                    LDAPSearchResultEntry(
                        "uid=002,p", [("employeeNumber", ["002"])]
                    ),
                    source_done(b"002"),
                ],
                [
                    LDAPSearchResultEntry(
                        "uid=002,p", [("employeeNumber", ["002"])]
                    ),
                    source_done(b"003"),
                ],
            ],
            [
                [
                    LDAPSearchResultEntry(
                        "uid=001,s", [("employeeNumber", ["001"])]
                    ),
                    source_done(b"001"),
                ],
                [
                    LDAPSearchResultEntry(
                        "uid=003,s", [("employeeNumber", ["003"])]
                    ),
                    source_done(b"004"),
                ],
            ],
            source_names=["primary", "secondary"],
        )

        def test_f(server):
            # Pin the query and receive two pages of one entry each.
            for request_id, expected in ((11, b"uid=001,p"), (12, b"uid=002,p")):
                server.transport.clear()
                controls = [roster.encode_paged_results_control(1)]
                if request_id == 12:
                    controls[0] = roster.encode_paged_results_control(
                        1, cookie
                    )
                server.dataReceived(
                    LDAPMessage(
                        LDAPSearchRequest(baseObject="dc=example,dc=com"),
                        id=request_id,
                        controls=controls,
                    ).toWire()
                )
                messages = self.decodeMessages(server.transport.value())
                entries = [
                    message.value.objectName
                    for message in messages
                    if isinstance(message.value, LDAPSearchResultEntry)
                ]
                self.assertEqual(entries, [expected])
                done = [
                    message
                    for message in messages
                    if isinstance(message.value, LDAPSearchResultDone)
                ][0]
                query_id, version, confirmed_after = (
                    roster.decode_state_control(done.controls)
                )
                _size, cookie = roster.decode_paged_results_control(
                    done.controls
                )

            # Page 2 was requested, so page 1 is confirmed and page 2 is
            # still pending.  Drop the connection: the cookie dies, the
            # query does not.
            old_cookie = cookie
            old_query_id = query_id
            old_version = version
            server.connectionLost(error.ConnectionDone)
            self.assertRaises(
                roster.InvalidContinuationToken,
                server.pager.consume_token,
                old_cookie,
                id(server),
            )

            primary, secondary = server.primary_source, "secondary"
            pending = [
                testutil.LDAPClientTestDriver(
                    [
                        LDAPSearchResultEntry(
                            "uid=002,p", [("employeeNumber", ["002"])]
                        ),
                        LDAPSearchResultDone(ldaperrors.Success.resultCode),
                    ]
                ),
                testutil.LDAPClientTestDriver(
                    [
                        LDAPSearchResultDone(ldaperrors.Success.resultCode),
                    ]
                ),
            ]

            def createClient(factory):
                factory.doStart()
                proto = factory.buildProtocol(addr=None)
                proto.connectionMade()

            conf = config.LDAPConfig(
                serviceLocationOverrides={"": createClient}
            )
            resumed = MergedLDAPServer(
                [conf, conf],
                [False, False],
                source_names=[primary, secondary],
                primary_source=primary,
                pager=server.pager,
            )
            resumed.protocol = lambda: pending.pop(0)
            resumed.transport = proto_helpers.StringTransport()
            resumed.connectionMade()

            def after_connect(connected):
                connected.dataReceived(
                    LDAPMessage(
                        LDAPSearchRequest(baseObject="dc=example,dc=com"),
                        id=13,
                        controls=[
                            roster.encode_paged_results_control(1),
                            roster.encode_resume_control(
                                old_query_id,
                                old_version,
                                confirmed_after,
                            ),
                        ],
                    ).toWire()
                )
                messages = self.decodeMessages(connected.transport.value())
                entries = [
                    message.value.objectName
                    for message in messages
                    if isinstance(message.value, LDAPSearchResultEntry)
                ]
                # The pending page (002) is rebuilt and served again.
                self.assertEqual(entries, [b"uid=002,p"])
                self.assertNotEqual(
                    roster.decode_paged_results_control(
                        [
                            message.controls
                            for message in messages
                            if isinstance(
                                message.value, LDAPSearchResultDone
                            )
                        ][0]
                    )[1],
                    old_cookie,
                )

            return resumed._whenConnected(lambda: after_connect(resumed))

        d.addCallback(test_f)
        return d

    def test_bind_both_success(self):
        d = self.createMergedServer(
            [[LDAPBindResponse(resultCode=0)]], [[LDAPBindResponse(resultCode=0)]]
        )

        def test_f(server):
            server.dataReceived(LDAPMessage(LDAPBindRequest(), id=4).toWire())

            self.assertEqual(
                server.transport.value(),
                LDAPMessage(LDAPBindResponse(resultCode=0), id=4).toWire(),
            )

        d.addCallback(test_f)

        return d

    def test_bind_one_invalid(self):
        d = self.createMergedServer(
            [
                [
                    LDAPBindResponse(
                        resultCode=ldaperrors.LDAPInvalidCredentials.resultCode
                    )
                ]
            ],
            [[LDAPBindResponse(resultCode=0)]],
        )

        def test_f(server):
            server.dataReceived(LDAPMessage(LDAPBindRequest(), id=4).toWire())
            self.assertEqual(
                server.transport.value(),
                LDAPMessage(LDAPBindResponse(resultCode=0), id=4).toWire(),
            )

        d.addCallback(test_f)
        return d

    def test_bind_both_invalid(self):
        d = self.createMergedServer(
            [
                [
                    LDAPBindResponse(
                        resultCode=ldaperrors.LDAPInvalidCredentials.resultCode
                    )
                ]
            ],
            [
                [
                    LDAPBindResponse(
                        resultCode=ldaperrors.LDAPInvalidCredentials.resultCode
                    )
                ]
            ],
        )

        def test_f(server):
            server.dataReceived(LDAPMessage(LDAPBindRequest(), id=4).toWire())
            self.assertEqual(
                server.transport.value(),
                LDAPMessage(
                    LDAPBindResponse(
                        resultCode=ldaperrors.LDAPInvalidCredentials.resultCode
                    ),
                    id=4,
                ).toWire(),
            )

        d.addCallback(test_f)
        return d

    def test_search_merged(self):
        d = self.createMergedServer(
            [
                [
                    LDAPSearchResultEntry("cn=foo,dc=example,dc=com", [("a", ["b"])]),
                    LDAPSearchResultEntry("cn=bar,dc=example,dc=com", [("b", ["c"])]),
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
            [
                [
                    LDAPSearchResultEntry("cn=foo,dc=example,dc=com", [("a", ["b"])]),
                    LDAPSearchResultEntry("cn=bar2,dc=example,dc=com", [("b", ["c"])]),
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
        )

        def test_f(server):
            server.dataReceived(LDAPMessage(LDAPSearchRequest(), id=3).toWire())
            self.assertEqual(
                server.transport.value(),
                LDAPMessage(
                    LDAPSearchResultEntry("cn=foo,dc=example,dc=com", [("a", ["b"])]),
                    id=3,
                ).toWire()
                + LDAPMessage(
                    LDAPSearchResultEntry("cn=bar2,dc=example,dc=com", [("b", ["c"])]),
                    id=3,
                ).toWire()
                + LDAPMessage(
                    LDAPSearchResultEntry("cn=foo,dc=example,dc=com", [("a", ["b"])]),
                    id=3,
                ).toWire()
                + LDAPMessage(
                    LDAPSearchResultEntry("cn=bar,dc=example,dc=com", [("b", ["c"])]),
                    id=3,
                ).toWire()
                + LDAPMessage(
                    LDAPSearchResultDone(ldaperrors.Success.resultCode), id=3
                ).toWire(),
            )

        d.addCallback(test_f)

        return d

    def test_search_one_invalid(self):
        d = self.createMergedServer(
            [
                [
                    LDAPSearchResultDone(
                        ldaperrors.LDAPInappropriateAuthentication.resultCode
                    )
                ]
            ],
            [
                [
                    LDAPSearchResultEntry("cn=foo,dc=example,dc=com", [("a", ["b"])]),
                    LDAPSearchResultEntry("cn=bar,dc=example,dc=com", [("b", ["c"])]),
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
        )

        def test_f(server):
            server.dataReceived(LDAPMessage(LDAPSearchRequest(), id=3).toWire())
            self.assertEqual(
                server.transport.value(),
                LDAPMessage(
                    LDAPSearchResultEntry("cn=foo,dc=example,dc=com", [("a", ["b"])]),
                    id=3,
                ).toWire()
                + LDAPMessage(
                    LDAPSearchResultEntry("cn=bar,dc=example,dc=com", [("b", ["c"])]),
                    id=3,
                ).toWire()
                + LDAPMessage(
                    LDAPSearchResultDone(ldaperrors.Success.resultCode), id=3
                ).toWire(),
            )

        d.addCallback(test_f)

        return d

    def test_unbind_clientUnbinds(self):
        d = self.createMergedServer([[]], [[]])

        def test_f(server):
            server.dataReceived(LDAPMessage(LDAPUnbindRequest(), id=3).toWire())
            server.connectionLost(error.ConnectionDone)
            for c in self.clients:
                c.assertSent(LDAPUnbindRequest())
            self.assertEqual(server.transport.value(), b"")

        d.addCallback(test_f)

        return d

    def test_unbind_clientEOF(self):
        """
        No connection is done when client has nothing to say.
        """
        d = self.createMergedServer([[]], [[]])

        def test_f(server):
            server.connectionLost(error.ConnectionDone)

            self.assertEqual([], server.clients, "A connection should not be done.")
            self.assertEqual(server.transport.value(), b"")

        d.addCallback(test_f)

        return d

    def test_search_backend_disconnected_before_response(self):
        """
        If one backend disconnects before it can send its search-done, the
        merger must still complete the search using the surviving
        backend's response. Previously the merger sized its response queue
        to len(self.clients) and hung waiting for the dead backend (#231).
        """
        d = self.createMergedServer(
            [
                [
                    LDAPSearchResultEntry("cn=foo,dc=example,dc=com", [("a", ["b"])]),
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
            [
                [
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                ]
            ],
        )

        def test_f(server):
            # Kill the first backend before dispatching the search; the
            # other one will do all the work.
            server.clients[0].responses = []
            server.clients[0].connectionLost(error.ConnectionDone)

            server.dataReceived(
                LDAPMessage(
                    LDAPSearchRequest(baseObject="dc=example,dc=com"),
                    id=3,
                ).toWire()
            )

            self.assertEqual(
                server.transport.value(),
                LDAPMessage(
                    LDAPSearchResultEntry("cn=foo,dc=example,dc=com", [("a", ["b"])]),
                    id=3,
                ).toWire()
                + LDAPMessage(
                    LDAPSearchResultDone(ldaperrors.Success.resultCode), id=3
                ).toWire(),
            )

        d.addCallback(test_f)
        return d

    def test_search_backend_disconnects_mid_flight(self):
        """
        A backend that has already received a request but dies before it
        can send its LDAPSearchResultDone must not hang the search. The
        disconnect callback shrinks the expected-response count so the
        surviving backend's response completes the merge (#231).

        LDAPClientTestDriver replays queued responses synchronously on
        each send, so we drive the merger's ``queue`` directly to mimic
        "one Done in, then the peer dies".
        """
        d = self.createMergedServer([[]], [[]])

        def test_f(server):
            # One backend has already produced its Done -- pretend it did.
            server.queue(3, LDAPSearchResultDone(ldaperrors.Success.resultCode))
            self.assertIn(3, server.merge_map)
            self.assertEqual(server.merge_map[3]["expected"], 2)
            self.assertEqual(len(server.merge_map[3]["responses"]), 1)

            # Now the other backend dies without responding.
            server.clients[1].responses = []
            server.clients[1].connectionLost(error.ConnectionDone)

            self.assertNotIn(
                3, server.merge_map, "disconnect should have completed the merge"
            )
            self.assertEqual(
                server.transport.value(),
                LDAPMessage(
                    LDAPSearchResultDone(ldaperrors.Success.resultCode),
                    id=3,
                ).toWire(),
            )

        d.addCallback(test_f)
        return d

    def test_search_all_backends_disconnect(self):
        """
        If every backend disconnects with a merge still outstanding, the
        merger emits a synthetic ``LDAPSearchResultDone`` with
        ``LDAPOther`` rather than hanging (#231).
        """
        d = self.createMergedServer([[]], [[]])

        def test_f(server):
            # A search is in flight against both backends, no responses
            # yet arrived.
            server.merge_map[7] = {"expected": 2, "responses": []}

            for c in list(server.clients):
                c.responses = []
                c.connectionLost(error.ConnectionDone)

            self.assertNotIn(7, server.merge_map)
            self.assertEqual(
                server.transport.value(),
                LDAPMessage(
                    LDAPSearchResultDone(
                        resultCode=ldaperrors.LDAPOther.resultCode,
                        errorMessage=b"All backend LDAP connections lost",
                    ),
                    id=7,
                ).toWire(),
            )

        d.addCallback(test_f)
        return d

    def test_disconnect_paths_are_defensive(self):
        """
        Cover the remaining defensive branches in the merger's disconnect
        + dispatch paths so future regressions surface (#231):

        * ``_cbClientLost`` on a proto that was already pruned.
        * ``_checkComplete`` on an id that has already been finalized.
        * ``_clientQueue`` skipping a client whose ``connected`` flag was
          flipped without a ``notifyOnDisconnect`` firing.
        * ``_clientQueue`` swallowing ``LDAPClientConnectionLostException``.
        """
        d = self.createMergedServer([[]], [[]])

        def test_f(server):
            # 1. Re-fire disconnect for a client that's already been
            #    pruned -- proto not in self.clients branch.
            already_gone = server.clients[0]
            server._cbClientLost(None, already_gone)
            server._cbClientLost(None, already_gone)

            # 2. _checkComplete on an unknown id -- entry is None branch.
            server._checkComplete(9999)

            # 3. _clientQueue skips a client whose .connected is falsy
            #    even though it hasn't been pruned.
            stale = server.clients[0]
            stale.connected = 0
            calls = list(stale.sent)
            server._clientQueue(
                LDAPSearchRequest(baseObject="dc=example,dc=com"),
                None,
                lambda *_: None,
            )
            self.assertEqual(
                stale.sent, calls, "disconnected client should not have been sent to"
            )

            # 4. _clientQueue swallows LDAPClientConnectionLostException.
            from ldaptor.protocols.ldap.ldapclient import (
                LDAPClientConnectionLostException,
            )

            def _raise(*_a, **_kw):
                raise LDAPClientConnectionLostException()

            stale.connected = 1  # pass the guard, force the raise instead
            stale.send_multiResponse = _raise
            server._clientQueue(
                LDAPSearchRequest(baseObject="dc=example,dc=com"),
                None,
                lambda *_: None,
            )
            # No crash means the except caught it.

        d.addCallback(test_f)
        return d

    def test_unwilling_to_perform(self):
        d = self.createMergedServer([[]], [[]])

        def test_f(server):
            server.dataReceived(
                LDAPMessage(LDAPAddRequest(entry="", attributes=[]), id=3).toWire()
            )
            server.dataReceived(LDAPMessage(LDAPDelRequest(entry=""), id=4).toWire())
            server.dataReceived(
                LDAPMessage(
                    LDAPModifyRequest(object="", modification=[]), id=5
                ).toWire()
            )
            server.dataReceived(
                LDAPMessage(
                    LDAPModifyDNRequest(entry="", newrdn="", deleteoldrdn=0), id=6
                ).toWire()
            )
            server.dataReceived(
                LDAPMessage(LDAPExtendedRequest(requestName=""), id=7).toWire()
            )
            for c in server.clients:
                c.assertNothingSent()

            self.assertEqual(
                server.transport.value(),
                LDAPMessage(
                    LDAPAddResponse(
                        resultCode=ldaperrors.LDAPUnwillingToPerform.resultCode
                    ),
                    id=3,
                ).toWire()
                + LDAPMessage(
                    LDAPDelResponse(
                        resultCode=ldaperrors.LDAPUnwillingToPerform.resultCode
                    ),
                    id=4,
                ).toWire()
                + LDAPMessage(
                    LDAPModifyResponse(
                        resultCode=ldaperrors.LDAPUnwillingToPerform.resultCode
                    ),
                    id=5,
                ).toWire()
                + LDAPMessage(
                    LDAPModifyDNResponse(
                        resultCode=ldaperrors.LDAPUnwillingToPerform.resultCode
                    ),
                    id=6,
                ).toWire()
                + LDAPMessage(
                    LDAPExtendedResponse(
                        resultCode=ldaperrors.LDAPUnwillingToPerform.resultCode
                    ),
                    id=7,
                ).toWire(),
            )

        d.addCallback(test_f)

        return d
