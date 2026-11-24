===========
LDAP Merger
===========

A merger forwards search requests to multiple LDAP Servers, and returns
the result entries of each successful response.

-------
Usecase
-------

You have multiple LDAP Servers, and you want to combine the search results
of all of them. This can be the case if you have an application
which needs extra users which are not inside the LDAP directory of your enterprise server,
and it is not desired to store them in it. In this case you could use an internal
LDAP server on the local filesystem (you can also do this with ldaptor, please
look at the LDAP Servers section, File-System LDAP DIT),
and combine this server with your general LDAP server.

-------
Caveats
-------

Be aware that it the merger is a read-only implementation: only BIND and SEARCH
operations are supported. Beyond that, notice that when binding only the servers
where the bind has been successful are delivering search results. So in order to retrieve
results on all servers, the bind user must be available on all LDAP servers. 

-----
Usage
-----

''''
Code
''''

Store the python code in a file called ldap-merger.tac:

.. code-block:: python

    #! /usr/bin/env python

    from twisted.application import service, internet
    from twisted.internet import protocol
    from ldaptor.config import LDAPConfig
    from ldaptor.protocols.ldap.merger import MergedLDAPServer

    application = service.Application("LDAP Merger")

    configs = [LDAPConfig(serviceLocationOverrides={"": ('external', 389)}),
               LDAPConfig(serviceLocationOverrides={"": ('localhost', 38942)})]
    use_tls = [True, False]
    factory = protocol.ServerFactory()
    factory.protocol = lambda: MergedLDAPServer(configs, use_tls)
    mergeService = internet.TCPServer(389, factory)
    mergeService.setServiceParent(application)


''''''''''
Discussion
''''''''''
We use two ldap servers: one listening on the host ''external'' on the
default port 389, and the other is a server running on localhost with port 38942.
TLS is used for the connection to the external server. The merger itself listens
on port 389.

Run it with
    $ twistd -y ldap-merger.tac

''''''''''''''''''''''''
Paged roster searches
''''''''''''''''''''''''

A plain search keeps the historical behaviour: every directory is queried
and the entries are merged as they arrive.  A client that needs a stable,
ascending, page-by-page view of a merged roster negotiates the standard
RFC 2696 paged-results control instead::

    from ldaptor.protocols import pureber

    value = pureber.BERSequence(
        [pureber.BERInteger(100), pureber.BEROctetString(b"")]
    )
    controls = [(b"1.2.840.113556.1.4.319", None, value.toWire())]

The nodes of the merged DIT must answer in ascending ``employeeNumber``
order (configurable with the ``number_attribute`` argument).  Each
request returns at most one page.  The response ``LDAPSearchResultDone``
carries:

* the paged-results control whose cookie is the continuation marker;
* ``1.3.6.1.4.1.22024.1.2.5`` with the query id, the pinned roster
  version and the last confirmed personnel number;
* ``1.3.6.1.4.1.22024.1.2.1`` only when a directory lost the pinned
  version.  Its value names the query id and the first personnel number
  whose range can no longer be settled.

Confirmation and reconnects
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The continuation cookie is valid only on the connection that received
it.  A client that disconnects presents the state control
(``1.3.6.1.4.1.22024.1.2.2``) with the same search on the new connection;
the merger confirms the last page that was actually served and continues
after the last confirmed personnel number, on the same version.  A page
that was served but never followed by another request is replayed rather
than skipped.

A page is only released once every live directory has moved strictly
past each personnel number in it.  This is what makes the ``084`` case
safe: a batch may end at ``084`` and report the next number as ``084``,
because another registration for the same person may still follow.  The
number is not confirmed until every directory reports a larger bound or
ends.

Duplicates and provenance
~~~~~~~~~~~~~~~~~~~~~~~~~

A personnel number may occur in several directories.  For a field that
the primary directory (the first source by default) carries, its value
wins.  Fields the primary does not carry are filled from the other
directories in configured order.  Every response entry also carries:

* ``rosterSource``: the directories in which this person actually
  appeared;
* ``rosterOccurrence``: ``source:count`` showing how many original
  registrations each directory supplied, so duplicate originals can be
  verified rather than collapsed silently;
* ``rosterPrimary``: ``TRUE`` when the primary directory contributed.

If a directory really loses the pinned version, its earlier numbers stay
confirmable, the query's later range is reported as unsettled, and other
queries (including other shifts) keep paging normally.

