"""Durable retention receipts for archived partitions.

The Raft journal and the local full dump are the only storage this module
relies on.  A receipt is only issued once a strict majority of the *current*
cluster configuration has proven, with durable local evidence, that it can
restore a named publication's manifest at the archived watermark.  Receipts
are then themselves replicated through the Raft log, which makes them part of
the cluster's durable state and keeps older receipts valid across ordinary
membership changes and disk swaps.
"""

import gzip
import hashlib
import json
import os

import pysyncobj.pickle as pickle


RETENTION_FORMAT_VERSION = 1
EVIDENCE_JOURNAL = 'journal'
EVIDENCE_SNAPSHOT = 'snapshot'

# Command bytes used in the raft log. Kept here as well as in syncobj so the
# offline recovery helper can replay retention commands without importing the
# whole SyncObj module.
COMMAND_RETENTION_PUBLISH = 4
COMMAND_RETENTION_RECEIPT = 5


def _canonical(value):
    """Return a deterministic byte representation used for hashing."""
    if isinstance(value, bytes):
        return b'b:' + value
    if isinstance(value, str):
        return b's:' + value.encode('utf-8')
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False).encode('utf-8')
        return b'j:' + encoded
    except (TypeError, ValueError):
        return b'p:' + pickle.dumps(value)


def _sha256(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def manifestHash(manifest):
    """Hash a manifest in a representation-independent way."""
    return _sha256(_canonical(manifest))


def publicationKey(publicationId, partition):
    """Stable key for a publication id / partition pair."""
    return _sha256(b'publication\x00' + _canonical(publicationId) +
                   b'\x00' + _canonical(partition))


def configDigest(nodeIds):
    return _sha256(_canonical(sorted(str(nodeId) for nodeId in nodeIds)))


def strictMajority(nodeCount):
    return nodeCount // 2 + 1


def _without(mapping, *names):
    result = dict(mapping)
    for name in names:
        result.pop(name, None)
    return result


def evidenceDigest(evidence):
    return _sha256(_canonical(_without(evidence, 'digest')))


def receiptDigest(receipt):
    return _sha256(_canonical(_without(receipt, 'receipt_id')))


def _normalizeAck(ack):
    """Copy an acknowledgement and recompute its digest."""
    ack = dict(ack)
    ack['digest'] = evidenceDigest(ack)
    return ack


def verifyReceipt(receipt):
    """Validate a receipt without trusting any node's live state.

    The receipt is self-contained: publication identity, manifest hash,
    archived watermark, the configuration that authorised it, and the
    acknowledgements that formed its quorum.
    """
    errors = []
    if not isinstance(receipt, dict):
        return {'valid': False, 'errors': ['receipt is not a dict']}

    try:
        expectedId = receiptDigest(receipt)
    except Exception as exc:
        return {'valid': False, 'errors': ['receipt digest failed: %s' % exc]}
    if receipt.get('receipt_id') != expectedId:
        errors.append('receipt digest mismatch')

    for field in ('publication_key', 'publication_id', 'partition',
                  'manifest', 'manifest_hash', 'archive_watermark',
                  'publication_term', 'term', 'configuration',
                  'recoverable_boundary', 'acknowledgements'):
        if field not in receipt:
            errors.append('missing field: %s' % field)
    if errors:
        return {'valid': False, 'errors': errors}

    if receipt.get('format_version') != RETENTION_FORMAT_VERSION:
        errors.append('unsupported receipt format')
    if not isinstance(receipt['archive_watermark'], int) or \
            receipt['archive_watermark'] < 1:
        errors.append('invalid archive watermark')
    if not isinstance(receipt['publication_term'], int):
        errors.append('invalid publication term')
    if not isinstance(receipt['term'], int):
        errors.append('invalid receipt term')
    try:
        if publicationKey(receipt['publication_id'], receipt['partition']) != \
                receipt['publication_key']:
            errors.append('publication key does not match id/partition')
        if manifestHash(receipt['manifest']) != receipt['manifest_hash']:
            errors.append('manifest hash mismatch')
    except Exception as exc:
        errors.append('receipt identity failed: %s' % exc)
    if errors:
        return {'valid': False, 'errors': errors}

    configuration = list(receipt['configuration'])
    if not configuration:
        errors.append('empty configuration')
    if receipt.get('configuration_digest') != configDigest(configuration):
        errors.append('configuration digest mismatch')

    required = strictMajority(len(configuration))
    configSet = set(configuration)
    seen = set()
    validAcks = []
    for ack in receipt['acknowledgements']:
        if not isinstance(ack, dict):
            errors.append('acknowledgement is not a dict')
            continue
        if ack.get('digest') != evidenceDigest(ack):
            errors.append('acknowledgement digest mismatch for %s' % ack.get('node_id'))
            continue
        nodeId = ack.get('node_id')
        if nodeId not in configSet:
            errors.append('acknowledgement from node outside configuration: %s' % nodeId)
            continue
        if nodeId in seen:
            errors.append('duplicate acknowledgement from: %s' % nodeId)
            continue
        if ack.get('publication_key') != receipt.get('publication_key'):
            errors.append('acknowledgement publication mismatch for %s' % nodeId)
            continue
        if ack.get('manifest_hash') != receipt.get('manifest_hash'):
            errors.append('acknowledgement manifest mismatch for %s' % nodeId)
            continue
        if ack.get('publication_term') != receipt.get('publication_term'):
            errors.append('acknowledgement publication term mismatch for %s' % nodeId)
            continue
        if ack.get('archive_watermark') != receipt.get('archive_watermark'):
            errors.append('acknowledgement watermark mismatch for %s' % nodeId)
            continue
        if not isinstance(ack.get('term'), int) or ack['term'] > receipt.get('term', -1):
            errors.append('acknowledgement from a different term for %s' % nodeId)
            continue
        if ack.get('term', 0) < receipt.get('publication_term', 0):
            errors.append('acknowledgement predates publication for %s' % nodeId)
            continue
        if not isinstance(ack.get('durable_index'), int):
            errors.append('invalid durable index for %s' % nodeId)
            continue
        if ack['durable_index'] < receipt['archive_watermark']:
            errors.append('acknowledgement does not cover watermark for %s' % nodeId)
            continue
        if ack['durable_index'] < receipt.get('publication_index', 0):
            errors.append('acknowledgement does not cover publication registration for %s' %
                          nodeId)
            continue
        seen.add(nodeId)
        validAcks.append(ack)

    if len(validAcks) < required:
        errors.append('insufficient durable majority: %d/%d, need %d' %
                      (len(validAcks), len(configuration), required))

    boundary = receipt['recoverable_boundary']
    if not isinstance(boundary, dict) or not isinstance(boundary.get('index'), int):
        errors.append('invalid recoverable boundary')
    elif boundary['index'] < receipt['archive_watermark']:
        errors.append('recoverable boundary is below archive watermark')

    if validAcks:
        minimum = min(ack['durable_index'] for ack in validAcks)
        if isinstance(boundary, dict) and boundary.get('index') != minimum:
            errors.append('recoverable boundary does not match acknowledgements')

    return {'valid': not errors, 'errors': errors,
            'valid_acknowledgements': [ack['node_id'] for ack in validAcks],
            'required': required}


class RetentionLedger(object):
    """In-memory retention state with explicit export/import.

    The ledger itself does no I/O.  SyncObj persists its export in the file
    journal's meta sidecar and embeds the same export in full dumps.
    """

    def __init__(self):
        self.__publications = {}
        self.__receipts = {}
        self.__evidence = {}

    # ---- publications -------------------------------------------------
    def addPublication(self, publication):
        key = publication['key']
        current = self.__publications.get(key)
        if current is None:
            self.__publications[key] = dict(publication)
            return True
        if (current.get('manifest_hash') == publication.get('manifest_hash') and
                current.get('archive_watermark') == publication.get('archive_watermark')):
            # Replaying a duplicate registration is harmless, but the first
            # registration term/index remains the publication's identity.
            return False
        return False

    def getPublication(self, key):
        publication = self.__publications.get(key)
        return dict(publication) if publication is not None else None

    def publications(self):
        return dict((key, dict(value)) for key, value in self.__publications.items())

    # ---- local durable evidence --------------------------------------
    def addEvidence(self, evidence):
        key = evidence['publication_key']
        nodeId = str(evidence['node_id'])
        byNode = self.__evidence.setdefault(key, {})
        current = byNode.get(nodeId)
        if current is not None:
            if current.get('storage_kind') == evidence.get('storage_kind') and \
                    current.get('durable_index') == evidence.get('durable_index') and \
                    current.get('term') == evidence.get('term') and \
                    current.get('snapshot_index') == evidence.get('snapshot_index'):
                return False
            if current.get('term') == evidence.get('term'):
                if current.get('durable_index', -1) > evidence.get('durable_index', -1):
                    return False
            elif current.get('term', -1) > evidence.get('term', -1):
                # Do not let an older-term observation overwrite a newer one.
                return False
        normalized = dict(evidence)
        normalized['digest'] = evidenceDigest(normalized)
        if current == normalized:
            return False
        byNode[nodeId] = normalized
        return True

    def getEvidence(self, key, nodeId=None):
        byNode = self.__evidence.get(key)
        if not byNode:
            return None
        if nodeId is not None:
            evidence = byNode.get(str(nodeId))
            return dict(evidence) if evidence is not None else None
        return dict((node, dict(value)) for node, value in byNode.items())

    def evidence(self):
        return dict((key, dict((node, dict(value)) for node, value in byNode.items()))
                    for key, byNode in self.__evidence.items())

    def allEvidence(self):
        result = []
        for byNode in self.__evidence.values():
            result.extend(dict(value) for value in byNode.values())
        return result

    def discardEvidence(self, key, nodeId=None):
        if nodeId is None:
            return self.__evidence.pop(key, None) is not None
        byNode = self.__evidence.get(key, {})
        removed = byNode.pop(str(nodeId), None)
        return removed is not None

    def pruneSnapshotEvidence(self, snapshotBoundary):
        """Drop snapshot evidence that no longer matches the local dump.

        A crash can leave the meta sidecar describing the previous dump.
        That evidence must not be offered for a publication the current dump
        no longer contains.
        """
        changed = False
        boundaryIndex = None if snapshotBoundary is None else snapshotBoundary[0]
        boundaryTerm = None if snapshotBoundary is None else snapshotBoundary[1]
        for key, byNode in list(self.__evidence.items()):
            publication = self.__publications.get(key)
            for nodeId, evidence in list(byNode.items()):
                if evidence.get('storage_kind') != EVIDENCE_SNAPSHOT:
                    continue
                stale = (boundaryIndex is None or
                         evidence.get('snapshot_index') != boundaryIndex or
                         evidence.get('snapshot_term') != boundaryTerm or
                         (publication is not None and
                          publication.get('registered_index', 0) > boundaryIndex))
                if stale:
                    byNode.pop(nodeId, None)
                    changed = True
            if not byNode:
                self.__evidence.pop(key, None)
        return changed

    # ---- receipts ----------------------------------------------------
    def addReceipt(self, receipt):
        key = receipt['publication_key']
        receipt = dict(receipt)
        receipt['receipt_id'] = receiptDigest(receipt)
        byId = self.__receipts.setdefault(key, {})
        if receipt['receipt_id'] in byId:
            return False
        byId[receipt['receipt_id']] = receipt
        return True

    def getReceipt(self, key, receiptId=None):
        byId = self.__receipts.get(key)
        if not byId:
            return None
        if receiptId is not None:
            receipt = byId.get(receiptId)
            return dict(receipt) if receipt is not None else None
        # The newest committed receipt for a publication is the useful one;
        # older receipts are kept because membership changes must not erase
        # evidence that a partition was already retained.
        def sortKey(receipt):
            return (receipt.get('issued_index', 0), receipt.get('term', 0),
                    receipt.get('receipt_id'))
        return dict(max(byId.values(), key=sortKey))

    def receipts(self):
        result = {}
        for key, byId in self.__receipts.items():
            result[key] = dict((rid, dict(receipt))
                               for rid, receipt in byId.items())
        return result

    def hasReceipt(self, key):
        return bool(self.__receipts.get(key))

    # ---- export / import ---------------------------------------------
    def export(self):
        return {
            'format_version': RETENTION_FORMAT_VERSION,
            'publications': self.publications(),
            'receipts': self.receipts(),
            'evidence': self.evidence(),
        }

    def importState(self, state, includeEvidence=True, evidenceNodes=None):
        if not state or state.get('format_version') != RETENTION_FORMAT_VERSION:
            return
        changed = False
        for publication in state.get('publications', {}).values():
            if 'key' in publication and self.addPublication(publication):
                changed = True
        for byId in state.get('receipts', {}).values():
            for receipt in byId.values():
                if self.addReceipt(receipt):
                    changed = True
        if includeEvidence:
            allowed = None if evidenceNodes is None else set(str(node) for node in evidenceNodes)
            for byNode in state.get('evidence', {}).values():
                if not isinstance(byNode, dict):
                    continue
                for evidence in byNode.values():
                    if not isinstance(evidence, dict):
                        continue
                    if allowed is not None and str(evidence.get('node_id')) not in allowed:
                        continue
                    if self.addEvidence(evidence):
                        changed = True
        return changed


def buildEvidence(publication, nodeId, term, durableIndex, durableTerm,
                  storageKind=EVIDENCE_JOURNAL, snapshotIndex=None,
                  snapshotTerm=None):
    evidence = {
        'publication_key': publication['key'],
        'publication_id': publication['publication_id'],
        'partition': publication['partition'],
        'manifest_hash': publication['manifest_hash'],
        'archive_watermark': publication['archive_watermark'],
        'publication_term': publication['registered_term'],
        'publication_index': publication['registered_index'],
        'node_id': nodeId,
        'term': term,
        'durable_index': durableIndex,
        'durable_term': durableTerm,
        'storage_kind': storageKind,
    }
    if snapshotIndex is not None:
        evidence['snapshot_index'] = snapshotIndex
    if snapshotTerm is not None:
        evidence['snapshot_term'] = snapshotTerm
    evidence['digest'] = evidenceDigest(evidence)
    return evidence


def finalizePublication(publication, index, term, key=None):
    """Bind a publication registration to the raft slot it committed in."""
    publication = dict(publication)
    if key is not None:
        publication['key'] = key
    if 'key' not in publication:
        publication['key'] = publicationKey(publication['publication_id'],
                                            publication['partition'])
    publication['registered_index'] = int(index)
    publication['registered_term'] = int(term)
    publication['manifest_hash'] = manifestHash(publication['manifest'])
    return publication


def buildReceipt(publication, acknowledgements, term, configuration,
                 configurationIndex=0, issuedIndex=0):
    acks = []
    seen = set()
    for ack in acknowledgements:
        nodeId = ack.get('node_id')
        if nodeId is None or nodeId in seen:
            continue
        if 'digest' in ack and ack.get('digest') != evidenceDigest(ack):
            # Never launder an ack whose stored digest has been altered.
            continue
        seen.add(nodeId)
        acks.append(_normalizeAck(ack))
    acks.sort(key=lambda ack: str(ack['node_id']))

    if not acks:
        return None
    minimumIdx = min(ack['durable_index'] for ack in acks)
    boundaryAck = min(acks, key=lambda ack: (ack['durable_index'],
                                             str(ack['node_id'])))
    receipt = {
        'format_version': RETENTION_FORMAT_VERSION,
        'publication_key': publication['key'],
        'publication_id': publication['publication_id'],
        'partition': publication['partition'],
        'manifest': publication['manifest'],
        'manifest_hash': publication['manifest_hash'],
        'archive_watermark': publication['archive_watermark'],
        'publication_term': publication['registered_term'],
        'publication_index': publication['registered_index'],
        'term': term,
        'configuration': sorted(str(nodeId) for nodeId in configuration),
        'configuration_digest': configDigest(configuration),
        'configuration_index': configurationIndex,
        'issued_index': issuedIndex,
        'recoverable_boundary': {
            'index': minimumIdx,
            'term': boundaryAck['durable_term'],
        },
        'acknowledgements': acks,
    }
    receipt['receipt_id'] = receiptDigest(receipt)
    return receipt


def finalizeReceipt(receipt, issuedIndex, issuedTerm):
    """Bind an issued receipt to the raft slot where it was committed.

    Every node applying the receipt command sees the same index/term, so the
    resulting receipt id is identical cluster-wide.
    """
    receipt = dict(receipt)
    receipt['issued_index'] = int(issuedIndex)
    receipt['issued_term'] = int(issuedTerm)
    receipt['term'] = int(issuedTerm)
    receipt['receipt_id'] = receiptDigest(receipt)
    return receipt


def receiptSupportsConfiguration(receipt, configuration):
    """Whether a receipt's durable acknowledgements cover a configuration.

    This is deliberately stricter than :func:`verifyReceipt`: a historical
    receipt remains valid on its own, but before an old disk is retired we
    require a strict majority *of the nodes that will remain* to have
    acknowledged this publication.  Acknowledgements from nodes that have
    already left the configuration never contribute to that majority.
    """
    verification = verifyReceipt(receipt)
    if not verification['valid']:
        return {'supported': False, 'errors': verification['errors'],
                'valid_acknowledgements': verification.get('valid_acknowledgements', [])}

    config = sorted(str(nodeId) for nodeId in configuration)
    if not config:
        return {'supported': False, 'errors': ['empty target configuration'],
                'valid_acknowledgements': []}
    configSet = set(config)
    acks = []
    for ack in receipt['acknowledgements']:
        if str(ack.get('node_id')) not in configSet:
            continue
        if ack['durable_index'] < receipt['archive_watermark']:
            continue
        if ack['durable_index'] < receipt.get('publication_index', 0):
            continue
        acks.append(ack)
    required = strictMajority(len(config))
    errors = []
    if len(acks) < required:
        errors.append('insufficient durable majority in target configuration: %d/%d, need %d' %
                      (len(acks), len(config), required))
    boundary = min((ack['durable_index'] for ack in acks), default=None)
    if boundary is not None and boundary < receipt['archive_watermark']:
        errors.append('target acknowledgement does not cover watermark')
    return {
        'supported': not errors,
        'errors': errors,
        'required': required,
        'valid_acknowledgements': sorted(str(ack['node_id']) for ack in acks),
        'recoverable_boundary': boundary,
    }


def lookupRetentionReceipt(state, publicationId, partition):
    """Find a recovered receipt by publication identity.

    ``state`` may be the result of :func:`recoverRetentionState` or a raw
    receipts mapping.  Returns ``None`` if no receipt for the pair survived.
    """
    if not state:
        return None
    key = publicationKey(publicationId, partition)
    receipts = state.get('receipts', state)
    if not isinstance(receipts, dict):
        return None
    candidates = []
    for value in receipts.values():
        if not isinstance(value, dict):
            continue
        if value.get('publication_key') == key and 'receipt_id' in value:
            candidates.append(value)
    for byId in receipts.values():
        if not isinstance(byId, dict):
            continue
        for receipt in byId.values():
            if isinstance(receipt, dict) and receipt.get('publication_key') == key:
                candidates.append(receipt)
    if not candidates:
        return None
    candidates.sort(key=lambda receipt: (receipt.get('issued_index', 0),
                                         receipt.get('term', 0),
                                         receipt.get('receipt_id', '')))
    return dict(candidates[-1])


def selectAcks(publication, acks, configuration, term):
    """Select acknowledgement records that may contribute to a receipt.

    An acknowledgement from an older term is still usable: it is bound to
    this publication by identity (key, manifest hash, watermark and
    publication term), so it can never migrate to a different publication.
    Acknowledgements from the future are ignored.
    """
    configSet = set(str(nodeId) for nodeId in configuration)
    selected = []
    seen = set()
    for ack in acks:
        ack = dict(ack)
        nodeId = str(ack.get('node_id'))
        if nodeId not in configSet or nodeId in seen:
            continue
        if ack.get('digest') != evidenceDigest(ack):
            continue
        if ack.get('publication_key') != publication['key']:
            continue
        if ack.get('manifest_hash') != publication['manifest_hash']:
            continue
        if ack.get('archive_watermark') != publication['archive_watermark']:
            continue
        if ack.get('publication_term') != publication['registered_term']:
            continue
        if not isinstance(ack.get('term'), int) or ack['term'] > term:
            continue
        if ack['term'] < publication['registered_term']:
            continue
        if not isinstance(ack.get('durable_index'), int):
            continue
        if ack['durable_index'] < publication['archive_watermark']:
            continue
        if ack['durable_index'] < publication.get('registered_index', 0):
            # The ack must also cover the registration, otherwise the
            # recoverable boundary would not include the manifest.
            continue
        seen.add(nodeId)
        selected.append(ack)
    return selected


def snapshotEvidenceValid(evidence, snapshotBoundary):
    """Whether snapshot evidence really covers the archived watermark.

    A snapshot that only contains state at or after the watermark is not
    proof for a publication whose manifest is not inside it.  Callers encode
    that check in ``snapshot_boundary`` plus the evidence's explicit
    ``snapshot_index``/``snapshot_term``; we additionally require the evidence
    to be marked as snapshot storage.
    """
    if not evidence or evidence.get('storage_kind') != EVIDENCE_SNAPSHOT:
        return False
    if not isinstance(snapshotBoundary, dict):
        return False
    index = snapshotBoundary.get('index')
    if not isinstance(index, int):
        return False
    if evidence.get('snapshot_index') != index:
        return False
    if evidence.get('snapshot_term') != snapshotBoundary.get('term'):
        return False
    if evidence.get('publication_index', 0) > index:
        # The snapshot boundary predates the manifest registration, so the
        # manifest is not actually inside the snapshot.
        return False
    return index >= evidence.get('archive_watermark', 0)


def mergeRecoveredState(states):
    ledger = RetentionLedger()
    for state in states:
        if state:
            ledger.importState(state, includeEvidence=True)
    return ledger


def replayRetentionCommands(journal, initialPublications=None):
    """Replay retention commands from a raw journal reader.

    Used by the offline recovery tooling: it reconstructs publications and
    committed receipts from the log itself, without trusting the in-memory
    ledger of any node.  ``journal`` is any object supporting ``len()`` and
    ``[i]`` with ``(command, index, term)`` entries.
    """
    publications = dict(initialPublications or {})
    receipts = {}
    for i in range(len(journal)):
        command, index, term = journal[i]
        if not command:
            continue
        commandType = ord(command[:1])
        if commandType == COMMAND_RETENTION_PUBLISH:
            try:
                record = pickle.loads(command[1:])
            except Exception:
                continue
            publication = finalizePublication(record, index, term)
            publications[publication['key']] = publication
        elif commandType == COMMAND_RETENTION_RECEIPT:
            try:
                receipt = pickle.loads(command[1:])
            except Exception:
                continue
            receipt = finalizeReceipt(receipt, index, term)
            verification = verifyReceipt(receipt)
            if verification['valid']:
                receipts[receipt['receipt_id']] = receipt
    return {'publications': publications, 'receipts': receipts}


def replayJournalFile(journalFile):
    """Replay retention commands from a file journal without a live node."""
    from .journal import createJournal
    journal = createJournal(journalFile)
    try:
        return replayRetentionCommands(journal)
    finally:
        try:
            journal._destroy()
        except Exception:
            pass

def _readSnapshotRetention(dumpFile, deserializer=None):
    if dumpFile is None or not os.path.isfile(dumpFile):
        return None, None, []
    try:
        if deserializer is not None:
            data = deserializer(dumpFile)
        else:
            with open(dumpFile, 'rb') as f:
                with gzip.GzipFile(fileobj=f) as g:
                    data = pickle.load(g)
        retention = data[4] if len(data) > 4 else None
        boundary = None
        if len(data) > 1 and isinstance(data[1], tuple) and len(data[1]) >= 2:
            boundary = {'index': data[1][1], 'term': data[1][2]}
        evidence = list(data[5]) if len(data) > 5 and data[5] else []
        return retention, boundary, evidence
    except Exception:
        return None, None, []


def peekSnapshotBoundary(dumpFile, deserializer=None):
    """Return ``(index, term)`` of an existing full dump, or None."""
    _, boundary, _ = _readSnapshotRetention(dumpFile, deserializer)
    if boundary is None:
        return None
    return boundary['index'], boundary['term']


def recoverRetentionState(journalFile=None, dumpFile=None, deserializer=None,
                          nodeId=None):
    """Rebuild retention state from a node's remaining durable materials.

    The journal meta records this node's own acknowledgements and receipts.
    The full dump contributes receipts/publications after journal compaction.
    Evidence recorded under another node's id is *not* imported: a node can
    only vouch for its own disk, and replaying another node's ack from a
    copied meta file would counterfeit a durable majority.

    Invalid receipts are returned in ``invalid_receipts`` instead of being
    silently accepted.
    """
    states = []
    if journalFile is not None:
        try:
            from .journal import MetaStorer
            meta = MetaStorer(journalFile + '.meta').getMeta()
            states.append((meta.get('retention'), None))
        except Exception:
            pass
    snapshotState, snapshotBoundary, snapshotEvidence = _readSnapshotRetention(
        dumpFile, deserializer)
    if snapshotState:
        states.append((snapshotState, snapshotBoundary))

    publications = {}
    receipts = {}
    evidence = {}
    for state, boundary in states:
        if not state or state.get('format_version') != RETENTION_FORMAT_VERSION:
            continue
        for publication in state.get('publications', {}).values():
            if isinstance(publication, dict) and 'key' in publication:
                publications[publication['key']] = dict(publication)
        for byId in state.get('receipts', {}).values():
            if not isinstance(byId, dict):
                continue
            for receiptId, receipt in byId.items():
                if isinstance(receipt, dict):
                    receipts[receiptId] = dict(receipt)
        for byNode in state.get('evidence', {}).values():
            if not isinstance(byNode, dict):
                continue
            for evidenceRecord in byNode.values():
                if not isinstance(evidenceRecord, dict):
                    continue
                if evidenceRecord.get('digest') != evidenceDigest(evidenceRecord):
                    continue
                if nodeId is not None and \
                        str(evidenceRecord.get('node_id')) != str(nodeId):
                    # A node can only vouch for its own durable storage.
                    continue
                if evidenceRecord.get('storage_kind') == EVIDENCE_SNAPSHOT:
                    # Evidence copied from a snapshot is only meaningful if
                    # the snapshot being read actually has that boundary and
                    # contains the publication's manifest.
                    if boundary is None:
                        continue
                    if evidenceRecord.get('snapshot_index') != boundary.get('index'):
                        continue
                    if evidenceRecord.get('snapshot_term') != boundary.get('term'):
                        continue
                    snapshotPublication = publications.get(
                        evidenceRecord.get('publication_key'))
                    if snapshotPublication is not None and \
                            snapshotPublication.get('registered_index', 0) > \
                            boundary.get('index', 0):
                        continue
                evidenceKey = (evidenceRecord.get('publication_key'),
                               str(evidenceRecord.get('node_id')))
                current = evidence.get(evidenceKey)
                if current is None or \
                        (current.get('term', -1), current.get('durable_index', -1)) < \
                        (evidenceRecord.get('term', -1), evidenceRecord.get('durable_index', -1)):
                    evidence[evidenceKey] = evidenceRecord

    for evidenceRecord in snapshotEvidence:
        if not isinstance(evidenceRecord, dict):
            continue
        if evidenceRecord.get('digest') != evidenceDigest(evidenceRecord):
            continue
        if snapshotBoundary is None:
            continue
        if evidenceRecord.get('storage_kind') != EVIDENCE_SNAPSHOT:
            continue
        if evidenceRecord.get('snapshot_index') != snapshotBoundary.get('index'):
            continue
        if evidenceRecord.get('snapshot_term') != snapshotBoundary.get('term'):
            continue
        if nodeId is not None and str(evidenceRecord.get('node_id')) != str(nodeId):
            continue
        publication = publications.get(evidenceRecord.get('publication_key'))
        if publication is None:
            continue
        if publication.get('registered_index', 0) > snapshotBoundary.get('index', 0):
            continue
        evidenceKey = (evidenceRecord.get('publication_key'),
                       str(evidenceRecord.get('node_id')))
        evidence[evidenceKey] = evidenceRecord

    valid = {}
    invalid = {}
    for receiptId, receipt in receipts.items():
        verification = verifyReceipt(receipt)
        if verification['valid']:
            valid[receiptId] = receipt
        else:
            invalid[receiptId] = {'receipt': receipt,
                                  'errors': verification['errors']}
    return {
        'publications': publications,
        'evidence': evidence,
        'receipts': valid,
        'invalid_receipts': invalid,
        'snapshot_boundary': snapshotBoundary,
    }
