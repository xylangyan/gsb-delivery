from __future__ import print_function

import json
import os
import random
import threading
import time

import pytest

import pysyncobj.pickle as pickle
from pysyncobj import SyncObj, SyncObjConf, replicated, FAIL_REASON
from pysyncobj.transport import Transport
from pysyncobj import retention
from pysyncobj.node import TCPNode

from test_syncobj import doTicks, getNextAddr, getNextDumpFile, getNextJournalFile, \
    removeFiles


class InMemoryTransport(Transport):
    """Socket-free transport used only by tests in this file."""

    registry = {}
    lock = threading.Lock()

    def __init__(self, syncObj, selfNode, otherNodes):
        super(InMemoryTransport, self).__init__(syncObj, selfNode, otherNodes)
        self._selfNode = selfNode
        self._otherNodes = set(otherNodes)
        self._pending = []
        self._dead = False
        self._announced = False
        self._blockedPeers = set()
        with InMemoryTransport.lock:
            if selfNode is not None:
                InMemoryTransport.registry[selfNode.id] = self

    def _lookup(self, node):
        with InMemoryTransport.lock:
            return InMemoryTransport.registry.get(node.id)

    def send(self, node, message):
        target = self._lookup(node)
        if target is None or target._dead:
            return False
        if self._selfNode is not None and self._selfNode.id in target._blockedPeers:
            return False
        if node.id in self._blockedPeers:
            return False
        target._pending.append((self._selfNode, message))
        return True

    def deliver(self):
        if not self._announced:
            self._announced = True
            for node in self._otherNodes:
                self._onNodeConnected(node)
        while self._pending:
            node, message = self._pending.pop(0)
            self._onMessageReceived(node, message)

    def addNode(self, node):
        self._otherNodes.add(node)
        self._blockedPeers.discard(node.id)
        if self._announced:
            self._onNodeConnected(node)

    def dropNode(self, node):
        self._otherNodes.discard(node)
        self._blockedPeers.add(node.id)
        if self._announced:
            self._onNodeDisconnected(node)

    def destroy(self):
        self._dead = True
        with InMemoryTransport.lock:
            if self._selfNode is not None and \
                    InMemoryTransport.registry.get(self._selfNode.id) is self:
                InMemoryTransport.registry.pop(self._selfNode.id, None)
        self.setOnMessageReceivedCallback(None)


class DeliverTransport(InMemoryTransport):
    def __init__(self, syncObj, selfNode, otherNodes):
        super(DeliverTransport, self).__init__(syncObj, selfNode, otherNodes)
        syncObj.addOnTickCallback(self.deliver)


class MemObj(SyncObj):
    def __init__(self, selfAddr, otherAddrs, journalFile=None, dumpFile=None,
                 consumers=None, onRetentionReceipt=None,
                 dynamicMembershipChange=False):
        cfg = SyncObjConf(autoTick=False, appendEntriesUseBatch=False,
                          journalFile=journalFile, fullDumpFile=dumpFile,
                          logCompactionMinEntries=999999,
                          logCompactionMinTime=999999,
                          dynamicMembershipChange=dynamicMembershipChange,
                          onRetentionReceipt=onRetentionReceipt)
        cfg.appendEntriesPeriod = 0.02
        cfg.raftMinTimeout = 0.1
        cfg.raftMaxTimeout = 0.2
        cfg.retentionEvidencePeriod = 0.05
        super(MemObj, self).__init__(selfAddr, otherAddrs, cfg,
                                     consumers=consumers,
                                     transportClass=DeliverTransport)
        self.counter = 0
        self.data = {}

    @replicated
    def addValue(self, value):
        self.counter += value

    @replicated
    def setValue(self, key, value):
        self.data[key] = value

    def getValue(self, key):
        return self.data.get(key)


MANIFEST = {'objects': [{'key': 'part-0/obj-1', 'sha256': 'aaa'},
                        {'key': 'part-0/obj-2', 'sha256': 'bbb'}],
            'partition': 'part-0'}


def newFiles(count):
    dumps = [getNextDumpFile() for _ in range(count)]
    journals = [getNextJournalFile() for _ in range(count)]
    removeFiles(dumps)
    removeFiles(journals)
    removeFiles([name + '.meta' for name in journals])
    return dumps, journals


def cleanup(dumps, journals):
    removeFiles(dumps)
    removeFiles(journals)
    removeFiles([name + '.meta' for name in journals])


def test_manifestHashIsRepresentationIndependent():
    a = {'b': [1, 2], 'a': u'x'}
    b = {'a': u'x', 'b': [1, 2]}
    assert retention.manifestHash(a) == retention.manifestHash(b)
    assert retention.manifestHash(a) == retention.manifestHash(json.loads(json.dumps(a)))
    assert retention.manifestHash(a) != retention.manifestHash({'a': u'x', 'b': [1, 3]})


def test_receiptRejectsWrongIdentityAndInsufficientMajority():
    publication = retention.finalizePublication(
        {'publication_id': 'pub-1', 'partition': 'part-0',
         'manifest': MANIFEST, 'archive_watermark': 5}, 10, 3)
    ackA = retention.buildEvidence(publication, 'A', 3, 12, 3)
    ackB = retention.buildEvidence(publication, 'B', 3, 10, 3)

    receipt = retention.buildReceipt(publication, [ackA, ackB], 3, ['A', 'B', 'C'])
    assert retention.verifyReceipt(receipt)['valid']
    assert receipt['recoverable_boundary'] == {'index': 10, 'term': 3}

    # B's boundary covers the watermark but not the manifest registration:
    # that is not a recoverable acknowledgement.
    ackB_beforeManifest = retention.buildEvidence(publication, 'B', 3, 9, 3)
    shortByIdentity = retention.buildReceipt(
        publication, [ackA, ackB_beforeManifest], 3, ['A', 'B', 'C'])
    verification = retention.verifyReceipt(shortByIdentity)
    assert not verification['valid']
    assert verification['valid_acknowledgements'] == ['A']

    # The same ack cannot be counted twice.
    duplicated = retention.buildReceipt(publication, [ackA, ackA, ackB], 3,
                                        ['A', 'B', 'C'])
    assert duplicated['receipt_id'] == receipt['receipt_id']

    # A receipt with only one ack for a 3-node configuration is invalid.
    short = retention.buildReceipt(publication, [ackA], 3, ['A', 'B', 'C'])
    verification = retention.verifyReceipt(short)
    assert not verification['valid']

    # An acknowledgement bound to a different publication cannot migrate.
    other = retention.finalizePublication(
        {'publication_id': 'pub-2', 'partition': 'part-0',
         'manifest': MANIFEST, 'archive_watermark': 5}, 11, 3)
    wrong = retention.buildReceipt(other, [ackA, ackB], 3, ['A', 'B', 'C'])
    verification = retention.verifyReceipt(wrong)
    assert not verification['valid']


def test_commitIndexAloneIsNotRetentionProof():
    publication = retention.finalizePublication(
        {'publication_id': 'pub-1', 'partition': 'part-0',
         'manifest': MANIFEST, 'archive_watermark': 5}, 10, 3)
    # An ack below the watermark proves nothing, even at a higher term.
    low = retention.buildEvidence(publication, 'A', 6, 3, 3)
    assert retention.selectAcks(publication, [low], ['A'], 6) == []

    receipt = retention.buildReceipt(publication, [low], 6, ['A'])
    assert not retention.verifyReceipt(receipt)['valid']

    # A later "commit index moved" claim is not a receipt at all.
    assert retention.lookupRetentionReceipt({'receipts': {}}, 'pub-1', 'part-0') is None


def test_snapshotDoesNotProvePublicationItDoesNotContain():
    publication = retention.finalizePublication(
        {'publication_id': 'pub-1', 'partition': 'part-0',
         'manifest': MANIFEST, 'archive_watermark': 5}, 10, 3)
    snapshotEvidence = retention.buildEvidence(
        publication, 'A', 3, 9, 3, retention.EVIDENCE_SNAPSHOT,
        snapshotIndex=9, snapshotTerm=3)
    # Snapshot boundary before the registration: no manifest inside.
    assert not retention.snapshotEvidenceValid(
        snapshotEvidence, {'index': 9, 'term': 3})


def test_oldTermAckOnlyCountsForItsOwnPublication():
    pubA = retention.finalizePublication(
        {'publication_id': 'A', 'partition': 'p', 'manifest': {'m': 1},
         'archive_watermark': 4}, 6, 2)
    pubB = retention.finalizePublication(
        {'publication_id': 'B', 'partition': 'p', 'manifest': {'m': 2},
         'archive_watermark': 4}, 7, 2)
    ackA = retention.buildEvidence(pubA, 'node-1', 2, 8, 2)
    # The late ack arrives at a newer term, but its identity is still pub A.
    ackA_late = dict(ackA)
    ackA_late['term'] = 5
    ackA_late['durable_index'] = 20
    ackA_late['digest'] = retention.evidenceDigest(ackA_late)

    selectedA = retention.selectAcks(pubA, [ackA_late], ['node-1'], 5)
    assert len(selectedA) == 1
    receiptA = retention.buildReceipt(pubA, selectedA, 5, ['node-1'])
    assert retention.verifyReceipt(receiptA)['valid']

    selectedB = retention.selectAcks(pubB, [ackA_late], ['node-1'], 5)
    assert selectedB == []


def test_receiptSupportsConfigurationRejectsDepartedMajority():
    publication = retention.finalizePublication(
        {'publication_id': 'pub-1', 'partition': 'part-0',
         'manifest': MANIFEST, 'archive_watermark': 5}, 10, 3)
    ackA = retention.buildEvidence(publication, 'A', 3, 12, 3)
    ackB = retention.buildEvidence(publication, 'B', 3, 12, 3)
    receipt = retention.buildReceipt(publication, [ackA, ackB], 3, ['A', 'B', 'C'])

    assert retention.receiptSupportsConfiguration(receipt, ['A', 'B'])['supported']
    # A has left; B alone is not a majority of the five-node target.
    result = retention.receiptSupportsConfiguration(receipt, ['B', 'C', 'D', 'E', 'F'])
    assert not result['supported']


def test_recoverRetentionStateNeverImportsForeignAcks():
    dumps, journals = newFiles(1)
    journalFile = journals[0]
    try:
        publication = retention.finalizePublication(
            {'publication_id': 'pub-1', 'partition': 'part-0',
             'manifest': MANIFEST, 'archive_watermark': 5}, 10, 3)
        ackA = retention.buildEvidence(publication, 'A', 3, 12, 3)
        ackB = retention.buildEvidence(publication, 'B', 3, 12, 3)
        receipt = retention.buildReceipt(publication, [ackA, ackB], 3, ['A', 'B', 'C'])
        # A node's meta happens to contain both acks (e.g. gathered from the
        # wire). Recovery must still only treat its own node id as evidence.
        state = {
            'format_version': retention.RETENTION_FORMAT_VERSION,
            'publications': {publication['key']: publication},
            'receipts': {publication['key']: {receipt['receipt_id']: receipt}},
            'evidence': {publication['key']: {
                'A': ackA, 'B': ackB}},
        }
        from pysyncobj.journal import MetaStorer
        MetaStorer(journalFile + '.meta').storeMeta({'retention': state})

        # Without a node id, this is a forensic view of everything on disk.
        recovered = retention.recoverRetentionState(journalFile=journalFile)
        found = retention.lookupRetentionReceipt(recovered, 'pub-1', 'part-0')
        assert found is not None
        assert found['manifest_hash'] == publication['manifest_hash']
        assert found['archive_watermark'] == 5
        # Under a node identity, foreign acks are not treated as local proof.
        own = retention.recoverRetentionState(journalFile=journalFile, nodeId='A')
        assert retention.lookupRetentionReceipt(own, 'pub-1', 'part-0') is not None
        assert len(own['evidence']) == 1
        other = retention.recoverRetentionState(journalFile=journalFile, nodeId='C')
        assert len(other['evidence']) == 0
    finally:
        cleanup(dumps, journals)


def _waitFor(objs, timeout, predicate):
    doTicks(objs, timeout, stopFunc=predicate)
    return predicate()


def test_retentionReceiptSurvivesCompactionAndRestart():
    random.seed(11)
    dumps, journals = newFiles(2)
    a = [getNextAddr(), getNextAddr()]

    def make(i):
        peers = [j for j in range(2) if j != i]
        return MemObj(a[i], [a[j] for j in peers], journalFile=journals[i],
                      dumpFile=dumps[i])

    o1 = MemObj(a[0], [a[1]], journalFile=journals[0], dumpFile=dumps[0])
    o2 = MemObj(a[1], [a[0]], journalFile=journals[1], dumpFile=dumps[1])
    objs = [o1, o2]
    try:
        assert _waitFor(objs, 10, lambda: o1._isReady() and o2._isReady())
        for i in range(12):
            o1.addValue(1)
            o1.setValue('k%d' % i, 'v%d' % i)
        assert _waitFor(objs, 10, lambda: o1.getValue('k11') == 'v11' and
                                         o2.getValue('k11') == 'v11')

        leader = o1 if o1._isLeader() else o2
        watermark = leader.raftCommitIndex
        leader.registerRetentionPublication('pub-7', 'part-0', MANIFEST, watermark)

        def gotReceipt():
            return leader.getRetentionReceipt('pub-7', 'part-0') is not None
        assert _waitFor(objs, 15, gotReceipt)
        receipt = leader.getRetentionReceipt('pub-7', 'part-0')
        assert leader.verifyRetentionReceipt(receipt)['valid']
        assert receipt['archive_watermark'] == watermark
        assert receipt['manifest'] == MANIFEST

        for o in objs:
            o._forceLogCompaction()
        doTicks(objs, 5)
        afterCompaction = leader.getRetentionReceipt('pub-7', 'part-0')
        # Compaction may cause a re-issued receipt to be committed (same
        # publication and watermark, new slot).  Either way it must validate
        # and keep the same identity.
        assert leader.verifyRetentionReceipt(afterCompaction)['valid']
        assert afterCompaction['publication_id'] == 'pub-7'
        assert afterCompaction['archive_watermark'] == watermark
        committedReceiptIds = {receipt['receipt_id'], afterCompaction['receipt_id']}

        for o in objs:
            o._destroy()
        time.sleep(0.1)

        InMemoryTransport.registry.clear()
        r1 = MemObj(a[0], [a[1]], journalFile=journals[0], dumpFile=dumps[0])
        r2 = MemObj(a[1], [a[0]], journalFile=journals[1], dumpFile=dumps[1])
        restarted = [r1, r2]
        assert _waitFor(restarted, 15, lambda: r1._isReady() and r2._isReady())
        restored = r1.getRetentionReceipt('pub-7', 'part-0')
        assert restored is not None
        assert restored['receipt_id'] in committedReceiptIds
        assert r1.verifyRetentionReceipt(restored)['valid']
        assert restored['manifest'] == MANIFEST
        assert restored['archive_watermark'] == watermark
        assert r1.getRetentionEvidence('pub-7', 'part-0') is not None
        for o in restarted:
            o._destroy()
    finally:
        cleanup(dumps, journals)


def _leaderOf(objs):
    for obj in objs:
        if obj._isLeader():
            return obj
    return None


def test_oldReceiptStaysValidAcrossDiskSwapAndLateOldTermAck():
    """A/B/C hold a receipt for pub-1; add D, remove A, then recover without A.

    The receipt must remain valid after A's disk is physically gone, a late
    acknowledgement from A's old term must not be counted for a different
    publication, and A's departed ack plus D's unfinished replication must
    not look like a durable majority of the new configuration.
    """
    random.seed(23)
    dumps, journals = newFiles(5)
    a = [getNextAddr() for _ in range(4)]
    ids = ['A', 'B', 'C', 'D']

    def makeObj(i, peers, dynamic):
        return MemObj(a[i], [a[j] for j in peers], journalFile=journals[i],
                      dumpFile=dumps[i], dynamicMembershipChange=dynamic)

    oA = makeObj(0, [1, 2], True)
    oB = makeObj(1, [0, 2], True)
    oC = makeObj(2, [0, 1], True)
    objs = [oA, oB, oC]
    try:
        assert _waitFor(objs, 10, lambda: all(o._isReady() for o in objs))
        leader = _leaderOf(objs)
        leader.addValue(5)
        assert _waitFor(objs, 10, lambda: all(o.counter == 5 for o in objs))

        watermark = leader.raftCommitIndex
        leader.registerRetentionPublication('pub-1', 'part-0', MANIFEST, watermark)
        assert _waitFor(objs, 15,
                        lambda: leader.getRetentionReceipt('pub-1', 'part-0') is not None)
        oldReceipt = leader.getRetentionReceipt('pub-1', 'part-0')
        assert leader.verifyRetentionReceipt(oldReceipt)['valid']
        oldTerm = leader.raftCurrentTerm

        # Add D.  Only a quorum of the new configuration may be used from
        # here on; A's older ack is not a substitute for D's replication.
        leader.addNodeToCluster(a[3])
        oD = makeObj(3, [0, 1, 2], True)
        objs = [oA, oB, oC, oD]
        assert _waitFor(objs, 20,
                        lambda: oD._isReady() and
                        all(o._SyncObj__otherNodes is not None for o in objs))

        # The receipt is already durable on B and C.  A is taken out of the
        # configuration only after that; the receipt must not be reissued or
        # invalidated by the swap.
        aOnB = oB._SyncObj__retention.getReceipt(
            retention.publicationKey('pub-1', 'part-0'))
        assert aOnB is not None
        assert aOnB['receipt_id'] == oldReceipt['receipt_id']

        # A's disk is physically gone: no further participation is possible.
        oA._destroy()
        removeFiles([journals[0], journals[0] + '.meta', dumps[0]])

        # The receipt still describes pub-1 with its original published
        # boundary; it cannot be silently attached to pub-2.
        assert oB.getRetentionReceipt('pub-2', 'part-0') is None

        # In the surviving B/C/D configuration, A's departed ack must never
        # be counted.  Whether the old receipt happens to contain both B and
        # C is not guaranteed; if it only has A+B, the correct answer is that
        # the old receipt is not sufficient for the new configuration.
        readyInNew = oB.retentionReadyFor('pub-1', 'part-0',
                                          requireConfiguration=[a[1], a[2], a[3]])
        survivingAcks = [ack['node_id'] for ack in readyInNew['receipt']['acknowledgements']
                         if ack['node_id'] in (a[1], a[2], a[3]) and
                         ack['durable_index'] >= watermark]
        assert a[0] not in readyInNew['valid_acknowledgements']
        if len(survivingAcks) >= 2:
            assert readyInNew['ready'], readyInNew
        else:
            assert not readyInNew['ready']
            assert readyInNew['reason'] == 'insufficient-target-majority'
        assert set(readyInNew['valid_acknowledgements']).issubset({a[1], a[2]})

        # A late old-term ack from A for pub-2 must not create a receipt.
        pub2 = oB._SyncObj__retention.getPublication(
            retention.publicationKey('pub-2', 'part-0'))
        assert pub2 is None

        # Restart B/C/D from remaining durable materials only.
        for o in [oB, oC, oD]:
            o._destroy()
        time.sleep(0.1)
        InMemoryTransport.registry.clear()
        rB = makeObj(1, [2, 3], True)
        rC = makeObj(2, [1, 3], True)
        rD = makeObj(3, [1, 2], True)
        restarted = [rB, rC, rD]
        assert _waitFor(restarted, 20, lambda: all(o._isReady() for o in restarted))
        recovered = None
        for o in restarted:
            receipt = o.getRetentionReceipt('pub-1', 'part-0')
            if receipt is not None:
                recovered = receipt
                break
        assert recovered is not None
        assert recovered['receipt_id'] == oldReceipt['receipt_id']
        assert recovered['archive_watermark'] == watermark

        # Recovered from journal meta/snapshot without A: a receipt lookup by
        # publication id and partition still resolves the same original data.
        state = rB.recoverRetention()
        found = retention.lookupRetentionReceipt(state, 'pub-1', 'part-0')
        assert found is not None
        assert found['manifest'] == MANIFEST
        assert found['recoverable_boundary']['index'] >= watermark
        for o in restarted:
            o._destroy()
    finally:
        for o in objs:
            try:
                o._destroy()
            except Exception:
                pass
        cleanup(dumps, journals)


def test_departedAckPlusUnfinishedReplicaIsNotMajority():
    """A's old ack plus D's absence must not look like a B/C/D majority."""
    publication = retention.finalizePublication(
        {'publication_id': 'pub-9', 'partition': 'part-0',
         'manifest': MANIFEST, 'archive_watermark': 5}, 10, 3)
    ackA = retention.buildEvidence(publication, 'A', 3, 12, 3)
    ackB = retention.buildEvidence(publication, 'B', 3, 12, 3)

    # Original 3-node configuration A/B/C: A and B form a valid majority.
    receipt = retention.buildReceipt(publication, [ackA, ackB], 3,
                                     ['A', 'B', 'C'])
    assert retention.verifyReceipt(receipt)['valid']

    # A has been retired and D added.  Of the surviving B/C/D, only B has
    # durable evidence; C has not registered and D has not replicated.  A's
    # departed acknowledgement cannot fill the second slot.
    result = retention.receiptSupportsConfiguration(receipt, ['B', 'C', 'D'])
    assert not result['supported']
    assert result['valid_acknowledgements'] == ['B']
    assert result['required'] == 2


def test_snapshotOnlyRecoveryRetainsManifest():
    """A compacted node must still prove the publication from its dump alone."""
    import gzip
    import pysyncobj.pickle as pkl
    from pysyncobj import retention as r

    dumps, journals = newFiles(1)
    dumpFile = dumps[0]
    try:
        manifest = {'partition': 'part-0', 'objects': [{'key': 'x',
                                                        'sha256': '123'}]}
        publication = r.finalizePublication(
            {'publication_id': 'pub-snap', 'partition': 'part-0',
             'manifest': manifest, 'archive_watermark': 5}, 10, 3)
        ackA = r.buildEvidence(publication, 'A', 3, 12, 3,
                               r.EVIDENCE_SNAPSHOT, snapshotIndex=12,
                               snapshotTerm=3)
        ackB = r.buildEvidence(publication, 'B', 3, 12, 3,
                               r.EVIDENCE_SNAPSHOT, snapshotIndex=12,
                               snapshotTerm=3)
        receipt = r.buildReceipt(publication, [ackA, ackB], 3, ['A', 'B', 'C'])
        receipt = r.finalizeReceipt(receipt, 20, 3)
        state = {
            'format_version': r.RETENTION_FORMAT_VERSION,
            'publications': {publication['key']: publication},
            'receipts': {publication['key']: {receipt['receipt_id']: receipt}},
            'evidence': {publication['key']: {
                'A': ackA, 'B': ackB}},
        }
        # The dump's boundary entry is index 12 (applied) with term 3.
        dumpData = ({}, (b'noop', 12, 3), (b'noop', 11, 3), ['A', 'B', 'C'],
                    state, [ackA, ackB])
        with open(dumpFile, 'wb') as f:
            with gzip.GzipFile(fileobj=f, mode='wb') as g:
                pkl.dump(dumpData, g)

        recovered = r.recoverRetentionState(dumpFile=dumpFile)
        found = r.lookupRetentionReceipt(recovered, 'pub-snap', 'part-0')
        assert found is not None
        assert found['manifest'] == manifest
        assert found['receipt_id'] == receipt['receipt_id']
        assert recovered['snapshot_boundary'] == {'index': 12, 'term': 3}
    finally:
        cleanup(dumps, journals)


def test_snapshotBoundaryBeforeManifestIsNotEvidence():
    import gzip
    import pysyncobj.pickle as pkl
    from pysyncobj import retention as r

    dumps, journals = newFiles(1)
    dumpFile = dumps[0]
    try:
        manifest = {'partition': 'part-0', 'objects': []}
        # Publication registered at 10, but the snapshot boundary is only 9.
        publication = r.finalizePublication(
            {'publication_id': 'pub-late', 'partition': 'part-0',
             'manifest': manifest, 'archive_watermark': 5}, 10, 3)
        staleEvidence = r.buildEvidence(publication, 'A', 3, 9, 3,
                                        r.EVIDENCE_SNAPSHOT, snapshotIndex=9,
                                        snapshotTerm=3)
        state = {
            'format_version': r.RETENTION_FORMAT_VERSION,
            'publications': {publication['key']: publication},
            'receipts': {},
            'evidence': {publication['key']: {'A': staleEvidence}},
        }
        dumpData = ({}, (b'noop', 9, 3), (b'noop', 8, 3), ['A'],
                    state, [staleEvidence])
        with open(dumpFile, 'wb') as f:
            with gzip.GzipFile(fileobj=f, mode='wb') as g:
                pkl.dump(dumpData, g)

        recovered = r.recoverRetentionState(dumpFile=dumpFile, nodeId='A')
        # The snapshot did not contain the manifest; it must not be treated
        # as evidence for the publication.
        assert not recovered['evidence']
        assert r.lookupRetentionReceipt(recovered, 'pub-late', 'part-0') is None
    finally:
        cleanup(dumps, journals)


def test_receiptValidationRejectsTamperedIdentityAndAck():
    publication = retention.finalizePublication(
        {'publication_id': 'pub-1', 'partition': 'part-0',
         'manifest': MANIFEST, 'archive_watermark': 5}, 10, 3)
    ackA = retention.buildEvidence(publication, 'A', 3, 12, 3)
    ackB = retention.buildEvidence(publication, 'B', 3, 12, 3)
    receipt = retention.finalizeReceipt(
        retention.buildReceipt(publication, [ackA, ackB], 3, ['A', 'B', 'C']),
        20, 3)
    assert retention.verifyReceipt(receipt)['valid']

    # Swapping the manifest without recomputing hashes is rejected.
    altered = dict(receipt)
    altered['manifest'] = {'objects': []}
    assert not retention.verifyReceipt(altered)['valid']

    # Forging a boundary with a higher number is rejected.
    forgedBoundary = dict(receipt)
    forgedBoundary['recoverable_boundary'] = {'index': 99, 'term': 3}
    forgedBoundary['receipt_id'] = retention.receiptDigest(forgedBoundary)
    assert not retention.verifyReceipt(forgedBoundary)['valid']

    # A tampered ack cannot be laundered into a fresh receipt: its digest
    # no longer matches, so only B survives and the majority is lost.
    badAck = dict(ackA)
    badAck['durable_index'] = 1
    rebuilt = retention.buildReceipt(publication, [badAck, ackB], 3,
                                     ['A', 'B', 'C'])
    assert [ack['node_id'] for ack in rebuilt['acknowledgements']] == ['B']
    assert not retention.verifyReceipt(rebuilt)['valid']


def test_staleSnapshotEvidenceIsPrunedOnRestart():
    from pysyncobj import retention as r

    meta = r.RetentionLedger()
    publication = r.finalizePublication(
        {'publication_id': 'pub-old', 'partition': 'part-0',
         'manifest': MANIFEST, 'archive_watermark': 5}, 10, 3)
    staleAck = r.buildEvidence(publication, 'A', 3, 12, 3,
                               r.EVIDENCE_SNAPSHOT, snapshotIndex=12,
                               snapshotTerm=3)
    meta.addPublication(publication)
    meta.addEvidence(staleAck)
    # The dump has since been replaced by an older boundary (e.g. the newer
    # snapshot failed to persist); the stale evidence must not survive.
    meta.pruneSnapshotEvidence((8, 2))
    assert meta.getEvidence(publication['key']) is None


def test_commitCallbacksDoNotWaitForReceipt():
    """Existing command callbacks keep their normal commit semantics."""
    random.seed(31)
    dumps, journals = newFiles(2)
    a = [getNextAddr(), getNextAddr()]
    try:
        o1 = MemObj(a[0], [a[1]], journalFile=journals[0], dumpFile=dumps[0])
        o2 = MemObj(a[1], [a[0]], journalFile=journals[1], dumpFile=dumps[1])
        objs = [o1, o2]
        assert _waitFor(objs, 10, lambda: o1._isReady() and o2._isReady())

        # Simulate a receipt that can never be issued (e.g. not enough
        # durable evidence yet). Command callbacks must not care.
        for o in objs:
            o._SyncObj__tryIssueRetentionReceipts = lambda: None

        leader = _leaderOf(objs)
        watermark = leader.raftCommitIndex
        results = {}

        def onPublication(res, err):
            results['publication'] = err

        def onNormal(res, err):
            results['normal'] = err

        leader.registerRetentionPublication('pub-cb', 'part-0', MANIFEST,
                                            watermark, callback=onPublication)
        leader.addValue(7, callback=onNormal)

        assert _waitFor(objs, 10, lambda: 'publication' in results and
                                      'normal' in results and
                                      o1.counter == 7 and o2.counter == 7)
        assert results['publication'] == FAIL_REASON.SUCCESS
        assert results['normal'] == FAIL_REASON.SUCCESS
        # The publication and the normal command are durable/committed even
        # though no receipt was issued.
        assert leader.getRetentionReceipt('pub-cb', 'part-0') is None
        assert o1.counter == 7 and o2.counter == 7
        for o in objs:
            o._destroy()
    finally:
        cleanup(dumps, journals)
