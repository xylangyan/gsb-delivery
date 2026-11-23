from __future__ import print_function
import random
import socket
import threading
import time

import pytest

from pysyncobj import SyncObj, SyncObjConf, replicated, FAIL_REASON, SyncObjException
from pysyncobj.node import Node
from pysyncobj.syncobj import AsyncResult
from pysyncobj.transport import Transport

from test_syncobj import doTicks, getNextAddr


class CollectionObj(SyncObj):
    """Models the collection service: a replicated partition manifest and
    ingestion cursor. The matching log boundary is delivered alongside each
    consistent read result (ReadIndexResult.logBoundary)."""

    def __init__(self, selfNode, otherNodes, transport, compaction=False, readTimeout=5.0):
        cfg = SyncObjConf(autoTick=False, appendEntriesUseBatch=False,
                          linearizableReadTimeout=readTimeout)
        cfg.appendEntriesPeriod = 0.05
        cfg.raftMinTimeout = 0.2
        cfg.raftMaxTimeout = 0.4
        cfg.connectionTimeout = 1.0
        cfg.leaderFallbackTimeout = 1.0
        if compaction:
            cfg.logCompactionMinEntries = 40
            cfg.logCompactionMinTime = 0.1
            cfg.appendEntriesUseBatch = True
        super(CollectionObj, self).__init__(selfNode, otherNodes, cfg, transport=transport)
        self.manifest = ()
        self.cursor = 0
        self.archiveSeq = 0

    @replicated
    def archiveCommit(self, partition, cursor):
        self.archiveSeq += 1
        self.manifest = tuple(sorted(set(self.manifest) | {partition}))
        self.cursor = cursor
        return self.archiveSeq

    def _buildReadSnapshot(self):
        return {
            'manifest': self.manifest,
            'cursor': self.cursor,
            'archiveSeq': self.archiveSeq,
        }


class InMemoryNetwork(object):
    """Shared, in-process model of the cluster network with controllable links."""

    def __init__(self):
        self.transports = {}
        self.blockedLinks = set()  # frozenset({nodeIdA, nodeIdB})

    def register(self, nodeId, transport):
        self.transports[nodeId] = transport

    def block(self, nodeIdA, nodeIdB):
        self.blockedLinks.add(frozenset((nodeIdA, nodeIdB)))

    def unblock(self, nodeIdA, nodeIdB):
        self.blockedLinks.discard(frozenset((nodeIdA, nodeIdB)))

    def canDeliver(self, srcId, dstId):
        return frozenset((srcId, dstId)) not in self.blockedLinks


class InMemoryTransport(Transport):
    """Deterministic transport: messages are queued in the peer's inbox and
    delivered when the peer is ticked. No sockets, so the full raft logic can
    be exercised in environments where binding ports is forbidden."""

    def __init__(self, syncObj, selfNode, otherNodes, network):
        super(InMemoryTransport, self).__init__(syncObj, selfNode, otherNodes)
        self._network = network
        self._selfNode = selfNode
        self._nodes = set(otherNodes)
        self._connectedNodes = set()
        self._inbox = []
        self._lock = threading.Lock()
        self._ready = True
        if selfNode is not None:
            network.register(selfNode.id, self)

    @property
    def ready(self):
        return self._ready

    def tryGetReady(self):
        pass

    def waitReady(self):
        pass

    def addNode(self, node):
        self._nodes.add(node)

    def dropNode(self, node):
        self._nodes.discard(node)
        self._connectedNodes.discard(node)

    def bringUp(self):
        for node in self._nodes:
            if node not in self._connectedNodes:
                self._connectedNodes.add(node)
                self._onNodeConnected(node)

    def disconnectPeer(self, node):
        if node in self._connectedNodes:
            self._connectedNodes.discard(node)
            self._onNodeDisconnected(node)

    def send(self, node, message):
        if self._selfNode is None:
            srcId = None
        else:
            srcId = self._selfNode.id
        if srcId is not None and not self._network.canDeliver(srcId, node.id):
            return False
        peer = self._network.transports.get(node.id)
        if peer is None:
            return False
        with peer._lock:
            peer._inbox.append((self._selfNode, message))
        return True

    def deliverInbox(self):
        with self._lock:
            messages = self._inbox
            self._inbox = []
        for node, message in messages:
            self._onMessageReceived(node, message)

    def destroy(self):
        self._ready = False
        self._connectedNodes.clear()


def _buildCluster(numNodes=3, compaction=False, readTimeout=5.0):
    network = InMemoryNetwork()
    nodes = [Node('node%d' % i) for i in range(numNodes)]
    transports = {}
    objs = []
    for i, node in enumerate(nodes):
        others = [n for n in nodes if n != node]
        transport = InMemoryTransport(None, node, others, network)
        transports[node.id] = transport
        objs.append(CollectionObj(node, others, transport,
                                  compaction=compaction, readTimeout=readTimeout))
    for transport in transports.values():
        transport.bringUp()
    return network, nodes, objs


def tick(objs, duration, stopFunc=None, interval=0.01):
    """Drive every node's raft thread manually, delivering inboxes first."""
    deadline = time.time() + duration
    while time.time() < deadline:
        for obj in objs:
            obj._SyncObj__transport.deliverInbox()
            obj._onTick(interval)
        if stopFunc is not None and stopFunc():
            return
        time.sleep(interval)


def _leader(objs):
    leaderAddr = objs[0]._getLeader()
    return next(o for o in objs if o._SyncObj__selfNode == leaderAddr)


def _nodeById(objs, nodeId):
    return next(o for o in objs if o._SyncObj__selfNode.id == nodeId)


def _destroyAll(objs):
    for obj in objs:
        try:
            obj._doDestroy()
        except Exception:
            pass


def _waitRead(obj, timeout=5.0):
    box = {}
    event = threading.Event()
    obj.consistentRead(callback=lambda res, err: (box.update(res=res, err=err), event.set()),
                       timeout=timeout)
    return box, event


def test_consistentReadReturnsAppliedStateOnEveryNode():
    random.seed(101)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        assert all(o._isReady() for o in objs)
        leader = _leader(objs)
        leader.archiveCommit('p1', 10)
        tick(objs, 5.0, stopFunc=lambda: all(o.archiveSeq == 1 for o in objs))

        for node in objs:
            box, event = _waitRead(node)
            tick(objs, 5.0, stopFunc=event.is_set)
            assert event.is_set()
            assert box['err'] is None
            result = box['res']
            assert result.payload['archiveSeq'] == 1
            assert result.payload['manifest'] == ('p1',)
            assert result.payload['cursor'] == 10
            assert result.readIndex == node.raftLastApplied
            assert result.logBoundary[0] == node.raftLastApplied
            assert isinstance(result.logBoundary[1], int)
    finally:
        _destroyAll(objs)


def test_consistentReadAppendsNoLogEntry():
    random.seed(102)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        leader = _leader(objs)
        leader.archiveCommit('p1', 1)
        tick(objs, 5.0, stopFunc=lambda: all(o.archiveSeq == 1 for o in objs))

        sizes = [o._getRaftLogSize() for o in objs]
        for _ in range(5):
            for node in objs:
                box, event = _waitRead(node)
                tick(objs, 5.0, stopFunc=event.is_set)
                assert event.is_set() and box['err'] is None
                assert box['res'].payload['archiveSeq'] == 1
        tick(objs, 1.0)
        assert [o._getRaftLogSize() for o in objs] == sizes
    finally:
        _destroyAll(objs)


def test_stalePartitionedLeaderCannotServeOldState():
    random.seed(103)
    network, nodes, objs = _buildCluster()
    oldLeader = None
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        oldLeader = _leader(objs)
        oldLeaderId = oldLeader._SyncObj__selfNode.id
        survivors = [o for o in objs if o is not oldLeader]

        oldLeader.archiveCommit('before-split', 1)
        tick(objs, 5.0, stopFunc=lambda: all(o.archiveSeq == 1 for o in objs))

        # Put the old leader alone on one side and drop its connections.
        for survivor in survivors:
            sid = survivor._SyncObj__selfNode.id
            network.block(oldLeaderId, sid)
            oldLeader._SyncObj__transport.disconnectPeer(survivor._SyncObj__selfNode)
            survivor._SyncObj__transport.disconnectPeer(oldLeader._SyncObj__selfNode)

        tick(survivors, 5.0,
             stopFunc=lambda: survivors[0]._getLeader() is not None and
                              survivors[0]._getLeader() == survivors[1]._getLeader() and
                              survivors[0]._getLeader() != oldLeader._SyncObj__selfNode)
        newLeader = _leader(survivors)

        # An archive is committed only after the new leader took over.
        newLeader.archiveCommit('after-split', 2)
        tick(survivors, 5.0, stopFunc=lambda: all(o.archiveSeq == 2 for o in survivors))
        requiredIndex = newLeader.raftCommitIndex

        # Read against the stale (isolated) leader first. It must never return
        # the old state: no read-index quorum is reachable, so it times out or
        # reports it can no longer prove leadership.
        box, event = _waitRead(oldLeader, timeout=1.0)
        tick([oldLeader], 2.0)
        assert event.wait(2.0)
        assert box['res'] is None
        assert box['err'] in (FAIL_REASON.TIMEOUT, FAIL_REASON.NOT_LEADER,
                              FAIL_REASON.MISSING_LEADER, FAIL_REASON.LEADER_CHANGED)
        assert oldLeader.archiveSeq == 1  # old state was never handed back

        # The catching-up node returns only state containing the new commit.
        lagging = next(o for o in survivors if o is not newLeader)
        box, event = _waitRead(lagging)
        tick(survivors, 5.0, stopFunc=event.is_set)
        assert event.is_set() and box['err'] is None
        result = box['res']
        assert result.payload['archiveSeq'] >= 2
        assert 'after-split' in result.payload['manifest']
        assert result.readIndex >= requiredIndex
        assert lagging.raftLastApplied >= requiredIndex
    finally:
        _destroyAll(objs)


def test_readWaitsForLaggingNodeToApplyConfirmedIndex():
    random.seed(104)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        leader = _leader(objs)
        follower = next(o for o in objs if o is not leader)
        followerId = follower._SyncObj__selfNode.id
        leaderId = leader._SyncObj__selfNode.id

        # Isolate one follower and commit work without it.
        network.block(leaderId, followerId)
        for node in objs:
            if node is not follower:
                node._SyncObj__transport.disconnectPeer(follower._SyncObj__selfNode)
        follower._SyncObj__transport.disconnectPeer(leader._SyncObj__selfNode)

        ticking = [o for o in objs if o is not follower]
        leader.archiveCommit('a', 1)
        leader.archiveCommit('b', 2)
        tick(ticking, 5.0, stopFunc=lambda: all(o.archiveSeq == 2 for o in ticking))
        requiredIndex = leader.raftCommitIndex
        assert follower.raftLastApplied < requiredIndex

        # Start the read while the follower still holds an older state.
        box, event = _waitRead(follower, timeout=8.0)
        assert not event.is_set()

        # Reconnect and let it catch up; the read may only complete afterwards.
        network.unblock(leaderId, followerId)
        for transport in network.transports.values():
            transport.bringUp()
        tick(objs, 8.0, stopFunc=event.is_set)
        assert event.wait(2.0)
        assert box['err'] is None
        result = box['res']
        assert result.readIndex >= requiredIndex
        assert result.payload['archiveSeq'] == 2
        assert follower.raftLastApplied == result.readIndex
    finally:
        _destroyAll(objs)


def test_concurrentArchiveYieldsSelfConsistentSnapshots():
    random.seed(105)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        leader = _leader(objs)
        leader.archiveCommit('p0', 0)
        tick(objs, 5.0, stopFunc=lambda: all(o.archiveSeq == 1 for o in objs))

        results = []
        errors = []

        def reader():
            for _ in range(8):
                box = {}
                done = threading.Event()
                leader.consistentRead(
                    callback=lambda res, err: (box.update(res=res, err=err), done.set()),
                    timeout=5.0)
                if not done.wait(5.0):
                    errors.append('timeout')
                    continue
                if box['err'] is not None:
                    errors.append(box['err'])
                else:
                    results.append(box['res'])

        readerThread = threading.Thread(target=reader)
        readerThread.start()
        for i in range(1, 6):
            leader.archiveCommit('p%d' % i, i)
        tick(objs, 10.0, stopFunc=lambda: leader.archiveSeq == 6 and not readerThread.is_alive())
        readerThread.join(5.0)

        assert errors == []
        assert results
        boundaryToPayload = {}
        prevIndex = 0
        for result in results:
            payload = (result.payload['manifest'], result.payload['cursor'],
                       result.payload['archiveSeq'])
            if result.logBoundary in boundaryToPayload:
                assert boundaryToPayload[result.logBoundary] == payload
            else:
                boundaryToPayload[result.logBoundary] = payload
            assert result.readIndex >= prevIndex
            prevIndex = result.readIndex
        assert results[-1].payload['archiveSeq'] == 6
    finally:
        _destroyAll(objs)


def test_cancelLinearizableRead():
    random.seed(106)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        oldLeader = _leader(objs)
        oldLeaderId = oldLeader._SyncObj__selfNode.id
        survivors = [o for o in objs if o is not oldLeader]
        for survivor in survivors:
            sid = survivor._SyncObj__selfNode.id
            network.block(oldLeaderId, sid)
            oldLeader._SyncObj__transport.disconnectPeer(survivor._SyncObj__selfNode)
            survivor._SyncObj__transport.disconnectPeer(oldLeader._SyncObj__selfNode)

        box = {}
        event = threading.Event()
        requestID = oldLeader.consistentRead(
            callback=lambda res, err: (box.update(err=err), event.set()), timeout=30.0)
        assert requestID is not None
        assert oldLeader.cancelLinearizableRead(requestID)
        tick([oldLeader], 2.0)
        assert event.wait(2.0)
        assert box['err'] == FAIL_REASON.CANCELLED
    finally:
        _destroyAll(objs)


def test_consistentReadWithoutQuorumTimesOut():
    random.seed(107)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        lone = objs[0]
        loneId = lone._SyncObj__selfNode.id
        for other in objs[1:]:
            network.block(loneId, other._SyncObj__selfNode.id)
            lone._SyncObj__transport.disconnectPeer(other._SyncObj__selfNode)
            other._SyncObj__transport.disconnectPeer(lone._SyncObj__selfNode)

        box, event = _waitRead(lone, timeout=1.0)
        tick([lone], 2.0)
        assert event.wait(2.0)
        assert box['res'] is None
        assert box['err'] in (FAIL_REASON.TIMEOUT, FAIL_REASON.MISSING_LEADER,
                              FAIL_REASON.NOT_LEADER, FAIL_REASON.LEADER_CHANGED)
    finally:
        _destroyAll(objs)


def test_boundaryUniquenessAcrossArchives():
    random.seed(108)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        leader = _leader(objs)
        seenBoundaries = set()
        for i in range(5):
            leader.archiveCommit('part-%d' % i, i)
            tick(objs, 5.0, stopFunc=lambda i=i: all(o.archiveSeq == i + 1 for o in objs))
            for node in objs:
                box, event = _waitRead(node)
                tick(objs, 5.0, stopFunc=event.is_set)
                assert event.is_set() and box['err'] is None
                assert box['res'].payload['archiveSeq'] == i + 1
                seenBoundaries.add(box['res'].logBoundary)
        assert len(seenBoundaries) == 5
        indices = sorted(boundary[0] for boundary in seenBoundaries)
        assert indices == sorted(set(indices))
    finally:
        _destroyAll(objs)


def test_consistentReadAfterLogCompaction():
    random.seed(109)
    network, nodes, objs = _buildCluster(compaction=True)
    try:
        tick(objs, 8.0, stopFunc=lambda: all(o._isReady() for o in objs))
        leader = _leader(objs)
        for i in range(120):
            leader.archiveCommit('p%d' % i, i)
        tick(objs, 15.0, stopFunc=lambda: all(o.archiveSeq == 120 for o in objs))
        for obj in objs:
            obj.forceLogCompaction()
        tick(objs, 15.0, stopFunc=lambda: all(o._getRaftLogSize() <= 60 for o in objs))

        for node in objs:
            box, event = _waitRead(node)
            tick(objs, 5.0, stopFunc=event.is_set)
            assert event.is_set() and box['err'] is None
            result = box['res']
            assert result.payload['archiveSeq'] == 120
            assert result.readIndex == node.raftLastApplied
            assert isinstance(result.logBoundary[1], int)
    finally:
        _destroyAll(objs)


def test_writeCallbacksAndLocalReadsKeepWorking():
    random.seed(110)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        leader = _leader(objs)
        asyncResult = AsyncResult()
        leader.archiveCommit('p1', 1, callback=asyncResult.onResult)
        tick(objs, 5.0, stopFunc=lambda: asyncResult.event.is_set())
        assert asyncResult.event.wait(2.0)
        assert asyncResult.error == FAIL_REASON.SUCCESS
        assert leader.cursor == 1
        assert any(o.archiveSeq == 1 for o in objs)
    finally:
        _destroyAll(objs)


def test_leaderChangeDuringReadFailsUnconfirmedReadButRetrySucceeds():
    random.seed(111)
    network, nodes, objs = _buildCluster()
    try:
        tick(objs, 5.0, stopFunc=lambda: all(o._isReady() for o in objs))
        oldLeader = _leader(objs)
        oldLeaderId = oldLeader._SyncObj__selfNode.id
        survivors = [o for o in objs if o is not oldLeader]
        follower = survivors[0]

        oldLeader.archiveCommit('p1', 1)
        tick(objs, 5.0, stopFunc=lambda: all(o.archiveSeq == 1 for o in objs))

        # Isolate the old leader first, so a read forwarded to it can never be
        # confirmed; the follower still believes the old leader is in charge.
        for survivor in survivors:
            sid = survivor._SyncObj__selfNode.id
            network.block(oldLeaderId, sid)
            oldLeader._SyncObj__transport.disconnectPeer(survivor._SyncObj__selfNode)
            survivor._SyncObj__transport.disconnectPeer(oldLeader._SyncObj__selfNode)

        box, event = _waitRead(follower, timeout=30.0)
        tick([follower], 0.2)
        assert not event.is_set()  # still unconfirmed: the leader is unreachable

        tick(survivors, 5.0,
             stopFunc=lambda: survivors[0]._getLeader() is not None and
                              survivors[0]._getLeader() == survivors[1]._getLeader() and
                              survivors[0]._getLeader() != oldLeader._SyncObj__selfNode)
        tick([follower], 2.0)
        assert event.wait(2.0)
        assert box['res'] is None
        assert box['err'] in (FAIL_REASON.LEADER_CHANGED, FAIL_REASON.NOT_LEADER,
                              FAIL_REASON.TIMEOUT)

        # Retrying against the new quorum yields a consistent state.
        tick(survivors, 3.0, stopFunc=lambda: any(o._isLeader() for o in survivors))
        newLeader = next(o for o in survivors if o._isLeader())
        newLeader.archiveCommit('p2', 2)
        tick(survivors, 5.0, stopFunc=lambda: all(o.archiveSeq == 2 for o in survivors))
        box, event = _waitRead(follower)
        tick(survivors, 5.0, stopFunc=event.is_set)
        assert event.wait(2.0)
        assert box['err'] is None
        assert box['res'].payload['archiveSeq'] >= 2

        # The old leader's local read still shows its own (stale) state untouched.
        assert oldLeader.archiveSeq == 1
    finally:
        _destroyAll(objs)


def _canBindLoopback():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', 0))
    except (OSError, socket.error):
        return False
    finally:
        sock.close()
    return True


@pytest.mark.skipif(not _canBindLoopback(), reason='loopback socket binding is forbidden in this environment')
def test_tcpSmokeConsistentRead():
    from test_syncobj import TestObj
    random.seed(111)
    addrs = [getNextAddr(), getNextAddr(), getNextAddr()]
    objs = [TestObj(addrs[0], [addrs[1], addrs[2]]),
            TestObj(addrs[1], [addrs[2], addrs[0]]),
            TestObj(addrs[2], [addrs[0], addrs[1]])]
    try:
        doTicks(objs, 15.0, stopFunc=lambda: all(o._isReady() for o in objs))
        leader = _leader(objs)
        leader.addValue(7)
        doTicks(objs, 10.0, stopFunc=lambda: all(o.getCounter() == 7 for o in objs))
        for node in objs:
            box, event = {}, threading.Event()
            node.consistentRead(
                readFunc=lambda n=node: {'counter': n.getCounter()},
                callback=lambda res, err: (box.update(res=res, err=err), event.set()),
                timeout=5.0)
            doTicks(objs, 8.0, stopFunc=event.is_set)
            assert event.wait(2.0)
            assert box['err'] is None
            assert box['res'].payload['counter'] == 7
    finally:
        for obj in objs:
            obj._destroy()
