import time
import random
import os
import sys
import threading
import weakref
import collections
import functools
import struct
import logging
import copy
import types
try:
    import Queue
    is_py3 = False

    def iteritems(v):
        return v.iteritems()
except ImportError:  # python3
    import queue as Queue
    is_py3 = True
    xrange = range

    def iteritems(v):
        return v.items()

import pysyncobj.pickle as pickle

from .dns_resolver import globalDnsResolver
from .poller import createPoller

try:
    from .pipe_notifier import PipeNotifier
    PIPE_NOTIFIER_ENABLED = True
except ImportError:
    PIPE_NOTIFIER_ENABLED = False

from .serializer import Serializer, SERIALIZER_STATE
from .node import Node, TCPNode
from .transport import Transport, TCPTransport, TransportNotReadyError
from .journal import createJournal
from .config import SyncObjConf, FAIL_REASON
from .encryptor import HAS_CRYPTO, getEncryptor
from .version import VERSION
from .fast_queue import FastQueue
from .monotonic import monotonic as monotonicTime

logger = logging.getLogger(__name__)


class _RAFT_STATE:
    FOLLOWER = 0
    CANDIDATE = 1
    LEADER = 2

class _COMMAND_TYPE:
    REGULAR = 0
    NO_OP = 1
    MEMBERSHIP = 2
    VERSION = 3
    # Regular command encoded with stable consumer identities instead of the
    # numeric method IDs of a particular consumer registration order.
    REGULAR_IDENTITY = 4

_bchr = functools.partial(struct.pack, 'B')

#: Marker used by snapshots that store consumer state by business identity
#: instead of by position in the `consumers` list.
_CONSUMER_SNAPSHOT_MAGIC = '__pysyncobj_consumer_snapshot_v1__'

#: Reserved owner name for methods implemented directly on SyncObj.
_SELF_OWNER = '__syncobj__'


class SyncObjException(Exception):
    def __init__(self, errorCode, *args, **kwargs):
        Exception.__init__(self, errorCode, *args, **kwargs)
        self.errorCode = errorCode

class SyncObjExceptionWrongVer(SyncObjException):
    def __init__(self, ver):
        SyncObjException.__init__(self, 'wrongVer')
        self.ver = ver

class SyncObjExceptionAmbiguousOwnership(SyncObjException):
    """Raised when the owner of legacy snapshot data / log entries cannot be
    determined unambiguously (missing or conflicting legacy manifests)."""
    def __init__(self, message):
        SyncObjException.__init__(self, 'AmbiguousConsumerOwnership', message)

class SyncObjConsumer(object):
    def __init__(self, consumerID=None, **kwargs):
        if consumerID is None:
            consumerID = kwargs.get('consumerId', kwargs.get('consumerIdentity'))
        self._syncObj = None
        #: Stable business identity of this consumer. It may also be supplied
        #: through SyncObj(consumers=[(identity, consumer)]), SyncObjConf.consumerIDs,
        #: or by setting the attribute before the SyncObj is created.
        self._consumerID = consumerID
        #: Optional alias kept for integrations that use the ``consumerId`` name.
        self.consumerId = consumerID
        self.__properies = set()
        for key in self.__dict__:
            self.__properies.add(key)

    def _destroy(self):
        self._syncObj = None

    def _serialize(self):
        return dict([(k, v) for k, v in iteritems(self.__dict__) if k not in self.__properies])

    def _deserialize(self, data):
        for k, v in iteritems(data):
            self.__dict__[k] = v


# https://github.com/bakwc/PySyncObj


def _normalizeConsumerID(consumerID):
    """Return a hashable, stable business identity or raise on ambiguity."""
    if isinstance(consumerID, (str, int)) and not isinstance(consumerID, bool):
        return consumerID
    if isinstance(consumerID, bytes):
        return consumerID.decode('utf-8')
    raise SyncObjException('Consumer identity must be a string or an integer, got %r' % (consumerID,))


def _replicatedMethods(obj):
    return sorted([m for m in dir(obj) if callable(getattr(obj, m)) and
                   getattr(getattr(obj, m), 'replicated', False) and
                   m != getattr(getattr(obj, m), 'origName')])


def _legacyManifestFromConf(conf):
    """Normalize the legacy consumer/method manifests into a single dict.

    Accepted layouts::

        legacyConsumers: ['sites', 'failedBatches']        # old positions -> identity
        legacyMethods: {0: ('sites', 'set'),
                        5: (None, 'addValue'),
                        7: ('failedBatches', 'set', 1)}
        legacyManifest: {'consumers': [...], 'methods': {...},
                         'registryVersion': 1}

    Returns None when no legacy information was configured.
    """
    manifest = conf.legacyManifest
    if manifest is None:
        consumers = conf.legacyConsumers
        methods = conf.legacyMethods
        registryVersion = conf.consumerRegistryVersion
        if consumers is None and methods is None:
            return None
    else:
        if not isinstance(manifest, dict):
            raise SyncObjExceptionAmbiguousOwnership(
                'legacyManifest must be a dict, got %r' % (type(manifest),))
        manifestConsumers = manifest.get('consumers', manifest.get('consumerIDs',
                              manifest.get('consumerIds', None)))
        manifestMethods = manifest.get('methods', manifest.get('methodIDs',
                            manifest.get('methodIds', None)))
        if manifestConsumers is not None and conf.legacyConsumers is not None and \
                list(manifestConsumers) != list(conf.legacyConsumers):
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy manifest and SyncObjConf.legacyConsumers disagree about the old '
                'consumer order')
        if manifestMethods is not None and conf.legacyMethods is not None and \
                dict(manifestMethods) != dict(conf.legacyMethods):
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy manifest and SyncObjConf.legacyMethods disagree about the old '
                'method IDs')
        consumers = manifestConsumers if manifestConsumers is not None else conf.legacyConsumers
        methods = manifestMethods if manifestMethods is not None else conf.legacyMethods
        registryVersion = manifest.get('registryVersion',
                          manifest.get('version', conf.consumerRegistryVersion))

    if registryVersion is None:
        registryVersion = 1

    normalizedConsumers = None
    if consumers is not None:
        if isinstance(consumers, dict):
            # {position: identity} - positions must be exactly 0..n-1.
            positions = sorted(consumers)
            if positions != list(range(len(positions))):
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy consumer positions must be dense 0..n-1, got %r' % (positions,))
            consumers = [consumers[p] for p in positions]
        normalizedConsumers = [_normalizeConsumerID(c) for c in consumers]
        seen = set()
        for identity in normalizedConsumers:
            if identity in seen:
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy consumer manifest lists identity %r more than once' % (identity,))
            seen.add(identity)

    normalizedMethods = {}
    if methods is not None:
        for methodID, description in methods.items():
            if not isinstance(methodID, int) or isinstance(methodID, bool):
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy method ID must be an integer, got %r' % (methodID,))
            normalizedMethods[methodID] = _normalizeLegacyMethodDescription(description, methodID)

    return {
        'consumers': normalizedConsumers,
        'methods': normalizedMethods,
        'registryVersion': int(registryVersion),
    }


def _normalizeLegacyMethodDescription(description, methodID):
    """Normalize one legacy method entry to ``(owner, name, ver)``.

    ``owner`` is ``None`` for methods implemented on SyncObj itself; a string /
    integer business identity for a consumer; or an integer position into the
    legacy consumer list (resolved later by the caller).
    """
    if isinstance(description, dict):
        owner = description.get('consumer', description.get('owner', description.get('id', None)))
        if owner is None:
            owner = description.get('self', None) and _SELF_OWNER
        name = description.get('name', description.get('method'))
        ver = description.get('ver', description.get('version', None))
        if isinstance(owner, bool):
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy method %d has an invalid owner %r' % (methodID, owner))
        if isinstance(owner, str) and owner in ('self', _SELF_OWNER):
            owner = None
        return owner, name, ver
    if isinstance(description, (tuple, list)):
        if len(description) == 2:
            owner, name = description
            return owner, name, None
        if len(description) == 3:
            return description[0], description[1], description[2]
    raise SyncObjExceptionAmbiguousOwnership(
        'legacy method %d has an unsupported description %r' % (methodID, description))


class SyncObj(object):
    def __init__(self, selfNode, otherNodes, conf=None, consumers=None, nodeClass = TCPNode, transport = None, transportClass = TCPTransport):
        """
        Main SyncObj class, you should inherit your own class from it.

        :param selfNode: object representing the self-node or address of the current node server 'host:port'
        :type selfNode: Node or str
        :param otherNodes: objects representing the other nodes or addresses of partner nodes ['host1:port1', 'host2:port2', ...]
        :type otherNodes: iterable of Node or iterable of str
        :param conf: configuration object
        :type conf: SyncObjConf
        :param consumers: objects to be replicated. Each item may be a consumer or an
            ``(identity, consumer)`` pair; identities decouple snapshots and log
            ownership from the position of the consumer in this list.
        :type consumers: list of SyncObjConsumer inherited objects
        :param nodeClass: class used for representation of nodes
        :type nodeClass: class
        :param transport: transport object; if None, transportClass is used to initialise such an object
        :type transport: Transport or None
        :param transportClass: the Transport subclass to be used for transferring messages to and from other nodes
        :type transportClass: class
        """

        if conf is None:
            self.__conf = SyncObjConf()
        else:
            self.__conf = conf

        self.__conf.validate()

        if self.__conf.password is not None:
            if not HAS_CRYPTO:
                raise ImportError("Please install 'cryptography' module")
            self.__encryptor = getEncryptor(self.__conf.password)
        else:
            self.__encryptor = None

        consumers, consumerIDs, identityModeEnabled = self.__normalizeConsumers(
            consumers, self.__conf.consumerIDs)
        self.__consumers = consumers
        self.__consumerIDs = consumerIDs
        self.__identityModeEnabled = identityModeEnabled
        origSelfNode = selfNode
        if not isinstance(selfNode, Node) and selfNode is not None:
            selfNode = nodeClass(selfNode)
        self.__selfNode = selfNode
        self.__otherNodes = set() # set of Node
        for otherNode in otherNodes:
            if otherNode == origSelfNode:
                continue
            if not isinstance(otherNode, Node):
                otherNode = nodeClass(otherNode)
            self.__otherNodes.add(otherNode)
        self.__readonlyNodes = set() # set of Node
        self.__connectedNodes = set() # set of Node
        self.__nodeClass = nodeClass

        self.__raftState = _RAFT_STATE.FOLLOWER
        self.__raftCurrentTerm = 0
        self.__votedForNodeId = None
        self.__votesCount = 0
        self.__raftLeader = None
        self.__raftElectionDeadline = monotonicTime() + self.__generateRaftTimeout()
        # Set while a leadership transfer (Raft 'TimeoutNow') is pending, to stop
        # the outgoing leader's heartbeats from postponing the forced election.
        self.__transferInProgress = False
        self.__raftLog = createJournal(self.__conf.journalFile)
        if len(self.__raftLog) == 0:
            self.__raftLog.add(_bchr(_COMMAND_TYPE.NO_OP), 1, self.__raftCurrentTerm)
        # Restore raft persistent state (see raft paper §5 - currentTerm and votedFor
        # must survive restarts, otherwise already-committed entries may be overwritten
        # by a stale member after a simultaneous restart of the cluster). The max() with
        # the last log term covers journals written by versions that did not store the
        # term: currentTerm is always >= the term of the last log entry.
        self.__raftCurrentTerm = max(self.__raftLog.getCurrentTerm(), self.__getCurrentLogTerm())
        self.__votedForNodeId = self.__raftLog.getVotedForNodeId()
        self.__raftCommitIndex = self.__raftLog.getRaftCommitIndex()
        self.__raftLastApplied = 1
        self.__raftNextIndex = {}
        self.__lastResponseTime = {}
        self.__raftMatchIndex = {}
        self.__lastSerializedTime = monotonicTime()
        self.__lastSerializedEntry = None
        self.__forceLogCompaction = False
        self.__leaderCommitIndex = None
        self.__onReadyCalled = False
        self.__changeClusterIDx = None
        self.__noopIDx = None
        self.__destroying = False
        self.__recvTransmission = ''

        self.__onTickCallbacks = []
        self.__onTickCallbacksLock = threading.Lock()

        self.__startTime = monotonicTime()
        self.__numOneSecondDumps = 0
        globalDnsResolver().setTimeouts(self.__conf.dnsCacheTime, self.__conf.dnsFailCacheTime)
        globalDnsResolver().setPreferredAddrFamily(self.__conf.preferredAddrType)
        self.__serializer = Serializer(self.__conf.fullDumpFile,
                                       self.__conf.logCompactionBatchSize,
                                       self.__conf.useFork,
                                       self.__conf.serializer,
                                       self.__conf.deserializer,
                                       self.__conf.serializeChecker)
        self.__lastInitTryTime = 0
        self._poller = createPoller(self.__conf.pollerType)

        if transport is not None:
            self.__transport = transport
        else:
            self.__transport = transportClass(self, self.__selfNode, self.__otherNodes)
        self.__transport.setOnNodeConnectedCallback(self.__onNodeConnected)
        self.__transport.setOnNodeDisconnectedCallback(self.__onNodeDisconnected)
        self.__transport.setOnMessageReceivedCallback(self.__onMessageReceived)
        self.__transport.setOnReadonlyNodeConnectedCallback(self.__onReadonlyNodeConnected)
        self.__transport.setOnReadonlyNodeDisconnectedCallback(self.__onReadonlyNodeDisconnected)
        self.__transport.setOnUtilityMessageCallback('status', self._getStatus)
        self.__transport.setOnUtilityMessageCallback('add', self._addNodeToCluster)
        self.__transport.setOnUtilityMessageCallback('remove', self._removeNodeFromCluster)
        self.__transport.setOnUtilityMessageCallback('set_version', self._setCodeVersion)
        self.__transport.setOnUtilityMessageCallback('transfer', self._transferLeadership)

        self._methodToID = {}
        self._idToMethod = {}
        self._idToConsumer = {}

        self.__selfCodeVersion = 0
        self.__currentVersionFuncNames = {}

        # Stable identity registry. Method IDs are still enumerated in the
        # historical order (version, self before consumers by list position,
        # method name) so that a node which keeps the old registration order
        # produces byte-for-byte compatible log entries.
        self.__consumerByIdentity = {}
        self.__consumerIdentityById = {}
        self.__methodKeyToID = {}
        self.__idToOwnerKey = {}
        self.__idToMethodName = {}
        self.__legacyManifest = _legacyManifestFromConf(self.__conf)
        self.__registryVersion = self.__legacyManifest['registryVersion'] \
            if self.__legacyManifest is not None else 0

        methodsToEnumerate = []

        for method in _replicatedMethods(self):
            ver = getattr(getattr(self, method), 'ver')
            methodsToEnumerate.append((ver, 0, method, self))

        for consumerNum, consumer in enumerate(consumers):
            consumerMethods = _replicatedMethods(consumer)
            for method in consumerMethods:
                ver = getattr(getattr(consumer, method), 'ver')
                methodsToEnumerate.append((ver, consumerNum + 1, method, consumer))

        currMethodID = 0
        for ver, ownerNum, method, obj in sorted(methodsToEnumerate):
            self.__selfCodeVersion = max(self.__selfCodeVersion, ver)
            if obj is self:
                ownerKey = _SELF_OWNER
                self._methodToID[method] = currMethodID
            else:
                ownerKey = consumerIDs[ownerNum - 1]
                self._methodToID[(id(obj), method)] = currMethodID
            self.__methodKeyToID[(ownerKey, method)] = currMethodID
            self._idToMethod[currMethodID] = getattr(obj, method)
            self.__idToOwnerKey[currMethodID] = ownerKey
            self.__idToMethodName[currMethodID] = method
            currMethodID += 1

        for consumerNum, consumer in enumerate(consumers):
            identity = consumerIDs[consumerNum]
            self.__consumerByIdentity[identity] = consumer
            self.__consumerIdentityById[id(consumer)] = identity
            consumer._syncObj = self

        for identity, consumer in iteritems(self.__consumerByIdentity):
            if self.__identityModeEnabled and consumer._consumerID is None:
                consumer._consumerID = identity
                consumer.consumerId = identity
        self._idToConsumer.update(self.__consumerByIdentity)

        self.__validateLegacyManifest()
        self.__buildLegacyRegistry()

        # A registry switch has no dedicated method of its own; allow activating
        # it even when no replicated method uses that version number.
        self.__selfCodeVersion = max(self.__selfCodeVersion, self.__registryVersion)

        self.__enabledCodeVersion = 0
        self.__onSetCodeVersion(0)

        self.__thread = None
        self.__mainThread = None
        self.__initialised = None
        self.__commandsQueue = FastQueue(self.__conf.commandsQueueSize)
        if not self.__conf.appendEntriesUseBatch and PIPE_NOTIFIER_ENABLED:
            self.__pipeNotifier = PipeNotifier(self._poller)
        self.__needLoadDumpFile = True

        self.__lastReadonlyCheck = 0
        self.__newAppendEntriesTime = 0

        self.__commandsWaitingCommit = collections.defaultdict(list)  # logID => [(termID, callback), ...]
        self.__commandsLocalCounter = 0
        self.__commandsWaitingReply = {}  # commandLocalCounter => callback

        self.__properies = set()
        for key in self.__dict__:
            self.__properies.add(key)

        if self.__conf.autoTick:
            self.__mainThread = threading.current_thread()
            self.__initialised = threading.Event()
            self.__thread = threading.Thread(target=SyncObj._autoTickThread, args=(weakref.proxy(self),))
            self.__thread.start()
            self.__initialised.wait()
            # while not self.__initialised.is_set():
            #     pass
        else:
            try:
                while not self.__transport.ready:
                    self.__transport.tryGetReady()
            except TransportNotReadyError:
                logger.exception('failed to perform initialization')
                raise SyncObjException('BindError') # Backwards compatibility

    def __normalizeConsumers(self, consumers, configuredIDs):
        """Normalize consumer objects and their stable business identities.

        Returns ``(consumers, identities, identityModeEnabled)``. When no identity
        is configured anywhere, identities are derived internally from positions
        and identity mode is disabled, preserving the historical behaviour.
        """
        if isinstance(consumers, dict):
            # Mapping of identity -> consumer; order is not meaningful, so sort
            # deterministically for a stable default enumeration.
            consumers = [consumers[key] for key in sorted(consumers, key=str)]
        consumers = consumers or []
        pairs = []
        for c in consumers:
            identity = None
            if isinstance(c, (tuple, list)) and len(c) == 2 and isinstance(c[1], SyncObjConsumer):
                identity, c = c
                identity = _normalizeConsumerID(identity)
            if not isinstance(c, SyncObjConsumer) and getattr(c, '_consumer', None):
                c = c._consumer()
            if not isinstance(c, SyncObjConsumer):
                raise SyncObjException('Consumers must be inherited from SyncObjConsumer')
            pairs.append([identity, c])

        if configuredIDs is not None:
            if len(configuredIDs) != len(pairs):
                raise SyncObjExceptionAmbiguousOwnership(
                    'SyncObjConf.consumerIDs has %d entries but %d consumers were supplied' %
                    (len(configuredIDs), len(pairs)))
            for i, identity in enumerate(configuredIDs):
                identity = _normalizeConsumerID(identity)
                if pairs[i][0] is not None and pairs[i][0] != identity:
                    raise SyncObjExceptionAmbiguousOwnership(
                        'conflicting identities for consumer %d: %r vs %r' %
                        (i, pairs[i][0], identity))
                pairs[i][0] = identity

        identityMode = configuredIDs is not None
        for identity, consumer in pairs:
            if identity is None:
                attrID = getattr(consumer, '_consumerID', None)
                if attrID is None:
                    attrID = getattr(consumer, 'consumerId', None)
                if attrID is not None:
                    identity = _normalizeConsumerID(attrID)
                    try:
                        consumer._consumerID = identity
                        consumer.consumerId = identity
                    except AttributeError:
                        pass
            if identity is not None:
                identityMode = True

        if not identityMode:
            identities = list(range(len(pairs)))
        else:
            missing = [i for i, pair in enumerate(pairs) if pair[0] is None]
            if missing:
                # Falling back to a position here would collide with the old
                # business identities the whole migration is meant to preserve.
                raise SyncObjExceptionAmbiguousOwnership(
                    'consumer identities are enabled, but consumers at positions %r have '
                    'no identity; pass (identity, consumer) pairs or set '
                    'SyncObjConsumer._consumerID' % (missing,))
            identities = [pair[0] for pair in pairs]
            seen = set()
            for identity in identities:
                if identity in seen:
                    raise SyncObjExceptionAmbiguousOwnership(
                        'duplicate consumer identity %r' % (identity,))
                seen.add(identity)

        return [pair[1] for pair in pairs], identities, identityMode

    def __requireLegacyManifest(self, what):
        if self.__legacyManifest is None:
            raise SyncObjExceptionAmbiguousOwnership(
                'cannot determine consumer ownership for %s: legacy consumer identity '
                'manifest is missing (set SyncObjConf.consumerIDs and '
                'SyncObjConf.legacyConsumers / legacyMethods)' % (what,))
        return self.__legacyManifest

    def __legacyConsumerIdentity(self, owner):
        """Resolve a legacy method owner to a business identity."""
        manifest = self.__requireLegacyManifest('legacy method owner %r' % (owner,))
        if owner is None or owner == _SELF_OWNER:
            return _SELF_OWNER
        if isinstance(owner, bool):
            raise SyncObjExceptionAmbiguousOwnership(
                'invalid legacy method owner %r' % (owner,))
        if isinstance(owner, int):
            positions = manifest['consumers']
            if positions is None:
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy method owner refers to consumer position %d but the legacy '
                    'consumer identity manifest is missing' % (owner,))
            if owner < 0 or owner >= len(positions):
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy method owner position %d is outside the legacy consumer '
                    'manifest of %d entries' % (owner, len(positions)))
            return positions[owner]
        return _normalizeConsumerID(owner)

    def __validateLegacyManifest(self):
        """Reject ambiguous legacy manifests before any state is touched."""
        manifest = self.__legacyManifest
        if manifest is None:
            return
        if not self.__identityModeEnabled:
            raise SyncObjExceptionAmbiguousOwnership(
                'a legacy consumer manifest was supplied, but the current consumers have '
                'no business identities; pass (identity, consumer) pairs or set '
                'SyncObjConsumer._consumerID so old data can be attributed')
        owners = {}
        for methodID, (owner, name, ver) in iteritems(manifest['methods']):
            identity = self.__legacyConsumerIdentity(owner)
            if not isinstance(name, str) or not name:
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy method %d does not specify a method name' % (methodID,))
            key = (identity, name)
            if key in owners and owners[key] != methodID:
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy method %d and %d both claim %s.%s' %
                    (owners[key], methodID, identity, name))
            owners[key] = methodID
            if identity not in self.__consumerByIdentity and identity != _SELF_OWNER:
                # The manifest may legitimately describe the old release only.
                # Applying such an entry will fail loudly; construction proceeds.
                continue
            if ver is not None:
                expected = name + '_v' + str(ver)
                if (identity, expected) not in self.__methodKeyToID and \
                        (identity, name) not in self.__methodKeyToID:
                    logger.warning('legacy method %d (%s.%s ver=%r) is not present in '
                                   'the current registry', methodID, identity, name, ver)
        if manifest['consumers'] is not None:
            for identity in manifest['consumers']:
                if identity not in self.__consumerByIdentity:
                    logger.warning('legacy consumer identity %r is not present in the '
                                   'current consumers list', identity)

    def destroy(self):
        """
        Correctly destroy SyncObj. Stop autoTickThread, close connections, etc.
        """
        if self.__conf.autoTick:
            self.__destroying = True
        else:
            self._doDestroy()

    def tick_thread_alive(self):
        """
        Check if the tick thread is alive.
        """
        if self.__thread and self.__thread.is_alive():
            return True
        return False

    def destroy_synchronous(self):
        """
        Correctly destroy SyncObj. Stop autoTickThread, close connections, etc. and ensure the threads are gone.
        """
        self.destroy()
        self.__thread.join()

    def waitReady(self):
        """
        Waits until the transport is ready for operation.

        :raises TransportNotReadyError: if the transport fails to get ready
        """
        self.__transport.waitReady()

    def waitBinded(self):
        """
        Waits until initialized (binded port).
        If success - just returns.
        If failed to initialized after conf.maxBindRetries - raise SyncObjException.
        """
        try:
            self.__transport.waitReady()
        except TransportNotReadyError:
            raise SyncObjException('BindError')
        if not self.__transport.ready:
            raise SyncObjException('BindError')

    def _destroy(self):
        self.destroy()

    def _doDestroy(self):
        self.__transport.destroy()
        for consumer in self.__consumers:
            consumer._destroy()
        self.__raftLog._destroy()

    def getCodeVersion(self):
        return self.__enabledCodeVersion

    def setCodeVersion(self, newVersion, callback = None):
        """Switch to a new code version on all cluster nodes. You
        should ensure that cluster nodes are updated, otherwise they
        won't be able to apply commands.

        :param newVersion: new code version
        :type int
        :param callback: will be called on success or fail
        :type callback: function(`FAIL_REASON <#pysyncobj.FAIL_REASON>`_, None)
        """
        assert isinstance(newVersion, int)
        if newVersion > self.__selfCodeVersion:
            raise Exception('wrong version, current version is %d, requested version is %d' % (self.__selfCodeVersion, newVersion))
        if newVersion < self.__enabledCodeVersion:
            raise Exception('wrong version, enabled version is %d, requested version is %d' % (self.__enabledCodeVersion, newVersion))
        self._applyCommand(pickle.dumps(newVersion), callback, _COMMAND_TYPE.VERSION)

    def addNodeToCluster(self, node, callback = None):
        """Add single node to cluster (dynamic membership changes). Async.
        You should wait until node successfully added before adding
        next node.

        :param node: node object or 'nodeHost:nodePort'
        :type node: Node | str
        :param callback: will be called on success or fail
        :type callback: function(`FAIL_REASON <#pysyncobj.FAIL_REASON>`_, None)
        """
        if not self.__conf.dynamicMembershipChange:
            raise Exception('dynamicMembershipChange is disabled')
        if not isinstance(node, Node):
            node = self.__nodeClass(node)
        self._applyCommand(pickle.dumps(['add', node.id, node]), callback, _COMMAND_TYPE.MEMBERSHIP)

    def removeNodeFromCluster(self, node, callback = None):
        """Remove single node from cluster (dynamic membership changes). Async.
        You should wait until node successfully added before adding
        next node.

        :param node: node object or 'nodeHost:nodePort'
        :type node: Node | str
        :param callback: will be called on success or fail
        :type callback: function(`FAIL_REASON <#pysyncobj.FAIL_REASON>`_, None)
        """
        if not self.__conf.dynamicMembershipChange:
            raise Exception('dynamicMembershipChange is disabled')
        if not isinstance(node, Node):
            node = self.__nodeClass(node)
        self._applyCommand(pickle.dumps(['rem', node.id, node]), callback, _COMMAND_TYPE.MEMBERSHIP)

    def _setCodeVersion(self, args, callback):
        self.setCodeVersion(args[0], callback)

    def _addNodeToCluster(self, args, callback):
        self.addNodeToCluster(args[0], callback)

    def _removeNodeFromCluster(self, args, callback):
        node = args[0]
        if node == self.__selfNode.address:
            callback(None, FAIL_REASON.REQUEST_DENIED)
        else:
            self.removeNodeFromCluster(node, callback)

    def transferLeadership(self, node=None, callback=None):
        """Gracefully transfer raft leadership to another node (Raft 'TimeoutNow').

        Must be called on the current leader. The target node must be fully
        caught up (matchIndex == leader's last log index), otherwise the
        request is denied. If node is None, the most caught-up connected
        follower is selected automatically. Async and best-effort: a
        successful callback only means the request was sent; poll
        getStatus()['leader'] to confirm the transfer completed.

        :param node: target node object or 'nodeHost:nodePort' (None = auto)
        :type node: Node | str | None
        :param callback: will be called on success or fail
        :type callback: function(`FAIL_REASON <#pysyncobj.FAIL_REASON>`_, None)
        """
        if self.__raftState != _RAFT_STATE.LEADER:
            self.__callErrCallback(FAIL_REASON.NOT_LEADER, callback)
            return
        if node is not None and not isinstance(node, Node):
            node = self.__nodeClass(node)
        if node is None:
            candidates = [(self.__raftMatchIndex.get(n, 0), n.id, n)
                          for n in self.__otherNodes if n in self.__connectedNodes]
            if not candidates:
                self.__callErrCallback(FAIL_REASON.REQUEST_DENIED, callback)
                return
            node = max(candidates)[2]
        if node == self.__selfNode or node not in self.__otherNodes:
            self.__callErrCallback(FAIL_REASON.REQUEST_DENIED, callback)
            return
        if self.__raftMatchIndex.get(node, 0) != self.__getCurrentLogIndex():
            # target is not fully synced - transferring would fail the election
            self.__callErrCallback(FAIL_REASON.REQUEST_DENIED, callback)
            return
        self.__transport.send(node, {
            'type': 'timeout_now',
            'term': self.__raftCurrentTerm,
        })
        if callback is not None:
            callback(None, FAIL_REASON.SUCCESS)

    def _transferLeadership(self, args, callback):
        self.transferLeadership(args[0] if args and args[0] else None, callback)

    def __onSetCodeVersion(self, newVersion):
        methods = _replicatedMethods(self)

        self.__currentVersionFuncNames = {}

        funcVersions = collections.defaultdict(set)
        for method in methods:
            ver = getattr(getattr(self, method), 'ver')
            origFuncName = getattr(getattr(self, method), 'origName')
            funcVersions[origFuncName].add(ver)

        for consumerNum, consumer in enumerate(self.__consumers):
            consumerKey = self.__consumerIDs[consumerNum]
            consumerMethods = [m for m in dir(consumer) if callable(getattr(consumer, m)) and \
                               getattr(getattr(consumer, m), 'replicated', False)]
            for method in consumerMethods:
                ver = getattr(getattr(consumer, method), 'ver')
                origFuncName = getattr(getattr(consumer, method), 'origName')
                funcVersions[(consumerKey, origFuncName)].add(ver)
                funcVersions[(id(consumer), origFuncName)].add(ver)

        for funcName, versions in iteritems(funcVersions):
            versions = sorted(list(versions))
            for v in versions:
                if v > newVersion:
                    break
                realFuncName = funcName[1] if isinstance(funcName, tuple) else funcName
                self.__currentVersionFuncNames[funcName] = realFuncName + '_v' + str(v)

    def _getFuncName(self, funcName):
        return self.__currentVersionFuncNames[funcName]

    def _identityForConsumer(self, consumer):
        identity = self.__consumerIdentityById.get(id(consumer))
        if identity is None:
            raise SyncObjExceptionAmbiguousOwnership(
                'consumer %r is not registered with this SyncObj' % (consumer,))
        return identity

    # ------------------------------------------------------------------
    # Stable-identity registry and legacy compatibility
    # ------------------------------------------------------------------

    def __resolveLegacyOwnerIdentity(self, owner):
        return self.__legacyConsumerIdentity(owner)

    def __resolveMethod(self, ownerIdentity, name, ver=None):
        """Find the versioned callable registered for ``ownerIdentity``."""
        if not isinstance(name, str):
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy method name for %r must be a string, got %r' % (ownerIdentity, name))
        if ownerIdentity != _SELF_OWNER and ownerIdentity not in self.__consumerByIdentity:
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy method owner identity %r is not one of the configured consumers' %
                (ownerIdentity,))
        keys = [key for key in self.__methodKeyToID if key[0] == ownerIdentity]
        exact = None
        variants = []
        for key in keys:
            methodName = key[1]
            if methodName == name:
                exact = key
            elif methodName.startswith(name + '_v'):
                suffix = methodName[len(name) + 2:]
                if suffix.isdigit():
                    variants.append((int(suffix), key))
        if ver is not None:
            for variantVer, key in variants:
                if variantVer == ver:
                    return key
            if exact is not None:
                return exact
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy manifest references %s.%s version %r, which is not registered' %
                (ownerIdentity, name, ver))
        if exact is not None and not variants:
            return exact
        if len(variants) == 1 and exact is None:
            return variants[0][1]
        if exact is not None and variants:
            # Prefer the newest registered variant for an unqualified legacy call.
            return max(variants)[1]
        if len(variants) > 1:
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy manifest references %s.%s without a version, but versions %r are '
                'registered' % (ownerIdentity, name, sorted(v for v, _ in variants)))
        raise SyncObjExceptionAmbiguousOwnership(
            'legacy manifest references unknown method %s.%s' % (ownerIdentity, name))

    def __buildLegacyRegistry(self):
        """Build numeric-ID -> callable and call -> numeric-ID maps.

        The maps come exclusively from the deployment-provided legacy method
        manifest. Numeric IDs are never reconstructed from the current class
        layout: a method added in the new release would otherwise silently take
        over an old ID. Snapshot recovery only needs the legacy consumer order,
        so a deployment that supplies just that can still restore old snapshots;
        old numeric log entries are then rejected until the method manifest is
        supplied.
        """
        self.__legacyIdToMethod = {}
        self.__legacyKeyToID = {}
        self.__legacyUnresolved = {}
        manifest = self.__legacyManifest
        if manifest is None:
            return

        resolved = {}
        for methodID, (owner, name, ver) in iteritems(manifest['methods']):
            identity = self.__resolveLegacyOwnerIdentity(owner)
            if identity != _SELF_OWNER and identity not in self.__consumerByIdentity:
                # The manifest may describe consumers that this release no longer
                # has. Applying such an entry must fail loudly, so register a
                # placeholder instead of guessing a current owner.
                self.__legacyUnresolved[methodID] = identity
                continue
            key = self.__resolveMethod(identity, name, ver)
            resolved[methodID] = key

        for methodID, key in iteritems(resolved):
            identity, methodKey = key
            if key in self.__legacyKeyToID:
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy method %d and %d both describe %s.%s' %
                    (self.__legacyKeyToID[key], methodID, identity, methodKey))
            self.__legacyKeyToID[key] = methodID
            self.__legacyIdToMethod[methodID] = self._idToMethod[self.__methodKeyToID[key]]

    def __usesLegacyEncoding(self):
        """Should outgoing regular commands use the old numeric encoding?"""
        return self.__legacyManifest is not None and \
            self.__enabledCodeVersion < self.__registryVersion

    def _makeReplicatedCommand(self, ownerIdentity, methodName, args, kwargs):
        """Encode a replicated call together with its command type.

        Before the registry switch the historical numeric encoding is used so
        that old nodes can still read operations sent by new nodes. After the
        switch the command carries the stable business identity instead.
        """
        resolvedName = self._getFuncName(methodName if ownerIdentity == _SELF_OWNER
                                         else (ownerIdentity, methodName))
        if self.__usesLegacyEncoding():
            key = (ownerIdentity, resolvedName)
            methodID = self.__legacyKeyToID.get(key)
            if methodID is None:
                raise SyncObjExceptionAmbiguousOwnership(
                    'cannot encode operation %s.%s with the old registry: no matching '
                    'legacy method ID was provided' % (ownerIdentity, resolvedName))
            payload = (methodID, args) if args else methodID
            if kwargs:
                payload = (methodID, args, kwargs)
            return pickle.dumps(payload), _COMMAND_TYPE.REGULAR

        if self.__identityModeEnabled:
            payload = (ownerIdentity, resolvedName, args, kwargs)
            return pickle.dumps(payload), _COMMAND_TYPE.REGULAR_IDENTITY

        if ownerIdentity == _SELF_OWNER:
            methodID = self._methodToID[resolvedName]
        else:
            methodID = self.__methodKeyToID[(ownerIdentity, resolvedName)]
        payload = (methodID, args) if args else methodID
        if kwargs:
            payload = (methodID, args, kwargs)
        return pickle.dumps(payload), _COMMAND_TYPE.REGULAR

    def __resolveIdentityCommand(self, command):
        ownerIdentity, methodName, args, kwargs = command
        if ownerIdentity != _SELF_OWNER and ownerIdentity not in self.__consumerByIdentity:
            raise SyncObjExceptionAmbiguousOwnership(
                'log entry refers to consumer identity %r, which is not configured' %
                (ownerIdentity,))
        key = (ownerIdentity, methodName)
        methodID = self.__methodKeyToID.get(key)
        if methodID is None:
            raise SyncObjExceptionAmbiguousOwnership(
                'log entry refers to unknown method %s.%s' % (ownerIdentity, methodName))
        return self._idToMethod[methodID](*args, **dict(kwargs, _doApply=True))

    def __resolveLegacyNumericCommand(self, funcID, args, kwargs):
        if self.__legacyManifest is None:
            raise SyncObjExceptionAmbiguousOwnership(
                'cannot determine ownership of legacy numeric log command %r: legacy '
                'consumer identity manifest is missing' % (funcID,))
        if funcID in self.__legacyUnresolved:
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy numeric log command %r belongs to consumer %r, which is not '
                'configured in this release' % (funcID, self.__legacyUnresolved[funcID]))
        method = self.__legacyIdToMethod.get(funcID)
        if method is None:
            raise SyncObjExceptionAmbiguousOwnership(
                'cannot determine ownership of legacy numeric log command %r: it is not '
                'described by the legacy method manifest' % (funcID,))
        return method(*args, **dict(kwargs, _doApply=True))

    def __snapshotLegacyOrder(self):
        manifest = self.__requireLegacyManifest('legacy snapshot positions')
        if manifest['consumers'] is None:
            raise SyncObjExceptionAmbiguousOwnership(
                'cannot write a snapshot readable by old nodes: the legacy consumer '
                'position manifest is missing')
        return list(manifest['consumers'])

    def __usesLegacySnapshotFormat(self):
        return self.__identityModeEnabled and self.__legacyManifest is not None and \
            self.__enabledCodeVersion < self.__registryVersion

    def __serializeConsumers(self):
        """Return the consumer portion of a snapshot.

        The result is always a list appended after the SyncObj state:

        * legacy / no-identity mode: one positional entry per consumer;
        * identity mode, switched: ``[identityStateDict]``;
        * identity mode, before the registry switch: the legacy positional
          entries followed by ``[identityStateDict]``. Old nodes read only the
          leading positions (and ignore the extra trailing entry), while new
          nodes use the identity map.
        """
        if not self.__consumers:
            return []
        states = dict((identity, self.__consumerByIdentity[identity]._serialize())
                      for identity in self.__consumerIDs)
        if not self.__identityModeEnabled:
            return [self.__consumerByIdentity[identity]._serialize()
                    for identity in self.__consumerIDs]

        identityPayload = {
            _CONSUMER_SNAPSHOT_MAGIC: 1,
            'consumers': states,
            'enabledVersion': self.__enabledCodeVersion,
        }
        if not self.__usesLegacySnapshotFormat():
            return [identityPayload]

        legacyStates = []
        for identity in self.__snapshotLegacyOrder():
            consumer = self.__consumerByIdentity.get(identity)
            if consumer is None:
                raise SyncObjExceptionAmbiguousOwnership(
                    'cannot write a snapshot readable by old nodes: legacy consumer '
                    'identity %r is not configured' % (identity,))
            legacyStates.append(consumer._serialize())
        return legacyStates + [identityPayload]

    def __planSnapshotConsumers(self, consumersData):
        """Validate a snapshot payload and return ``(consumerStates, enabledVersion)``.

        ``consumerStates`` maps business identity to stored state. Nothing is
        applied here: all ambiguity is reported before any state is touched.
        """
        if not self.__consumers:
            return {}, None

        # Switched format: a single identity map.
        if len(consumersData) == 1 and isinstance(consumersData[0], dict) and \
                consumersData[0].get(_CONSUMER_SNAPSHOT_MAGIC) is not None:
            return self.__validateIdentitySnapshot(consumersData[0])

        # Hybrid format: legacy positions followed by one identity map.
        if consumersData and isinstance(consumersData[-1], dict) and \
                consumersData[-1].get(_CONSUMER_SNAPSHOT_MAGIC) is not None:
            identityStates, enabledVersion = self.__validateIdentitySnapshot(consumersData[-1])
            legacyStates = list(consumersData[:-1])
            # The positional section intentionally covers only the consumers that
            # existed in the old release; the identity map may contain more. Verify
            # the overlap so the two representations cannot disagree.
            try:
                positionalStates, _ = self.__planPositionalConsumers(legacyStates)
            except SyncObjExceptionAmbiguousOwnership:
                return identityStates, enabledVersion
            for identity, state in iteritems(positionalStates):
                if identity in identityStates and identityStates[identity] != state:
                    raise SyncObjExceptionAmbiguousOwnership(
                        'snapshot consumer identity map conflicts with its positional '
                        'legacy state for %r' % (identity,))
            return identityStates, enabledVersion

        return self.__planPositionalConsumers(consumersData)

    def __validateIdentitySnapshot(self, payload):
        states = payload.get('consumers')
        if not isinstance(states, dict):
            raise SyncObjExceptionAmbiguousOwnership(
                'snapshot identity map is malformed')
        for identity in states:
            if identity not in self.__consumerByIdentity:
                raise SyncObjExceptionAmbiguousOwnership(
                    'snapshot contains consumer identity %r, which is not configured '
                    'in this release' % (identity,))
        return dict(states), payload.get('enabledVersion')

    def __planPositionalConsumers(self, legacyStates):
        legacyStates = list(legacyStates) if legacyStates is not None else []
        if not self.__identityModeEnabled:
            if len(legacyStates) != len(self.__consumers):
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy snapshot contains %d consumer states but %d consumers are '
                    'configured' % (len(legacyStates), len(self.__consumers)))
            return dict((identity, state) for identity, state in
                        zip(self.__consumerIDs, legacyStates)), None

        manifest = self.__requireLegacyManifest('positional consumer state in snapshot')
        legacyOrder = manifest['consumers']
        if legacyOrder is None:
            raise SyncObjExceptionAmbiguousOwnership(
                'cannot map positional consumer state in a legacy snapshot: the legacy '
                'consumer identity manifest is missing')
        if len(legacyStates) != len(legacyOrder):
            raise SyncObjExceptionAmbiguousOwnership(
                'legacy snapshot contains %d consumer states but the legacy consumer '
                'manifest describes %d consumers' % (len(legacyStates), len(legacyOrder)))
        states = {}
        for identity, state in zip(legacyOrder, legacyStates):
            if identity not in self.__consumerByIdentity:
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy snapshot consumer identity %r is not configured in this '
                    'release' % (identity,))
            if identity in states:
                raise SyncObjExceptionAmbiguousOwnership(
                    'legacy snapshot maps two positions to consumer identity %r' %
                    (identity,))
            states[identity] = state
        return states, None

    def _getRegistryInfo(self):
        """Introspection helper used by tests and deployment tooling."""
        return {
            'identities': list(self.__consumerIDs),
            'identityMode': self.__identityModeEnabled,
            'registryVersion': self.__registryVersion,
            'legacy': self.__legacyManifest is not None,
            'legacyMethodIDs': sorted(self.__legacyIdToMethod.keys()),
            'methodKeys': dict(self.__methodKeyToID),
        }

    def _applyCommand(self, command, callback, commandType = None):
        try:
            if commandType is None:
                self.__commandsQueue.put_nowait((command, callback))
            else:
                self.__commandsQueue.put_nowait((_bchr(commandType) + command, callback))
            if not self.__conf.appendEntriesUseBatch and PIPE_NOTIFIER_ENABLED:
                self.__pipeNotifier.notify()
        except Queue.Full:
            self.__callErrCallback(FAIL_REASON.QUEUE_FULL, callback)

    def _checkCommandsToApply(self):
        startTime = monotonicTime()

        while monotonicTime() - startTime < self.__conf.appendEntriesPeriod:
            if self.__raftLeader is None and self.__conf.commandsWaitLeader:
                break
            try:
                command, callback = self.__commandsQueue.get_nowait()
            except Queue.Empty:
                break

            requestNode, requestID = None, None
            if isinstance(callback, tuple):
                requestNode, requestID = callback

            if self.__raftState == _RAFT_STATE.LEADER:
                idx, term = self.__getCurrentLogIndex() + 1, self.__raftCurrentTerm

                if self.__conf.dynamicMembershipChange:
                    changeClusterRequest = self.__parseChangeClusterRequest(command)
                else:
                    changeClusterRequest = None

                if changeClusterRequest is None or self.__changeCluster(changeClusterRequest):

                    self.__raftLog.add(command, idx, term)

                    if requestNode is None:
                        if callback is not None:
                            self.__commandsWaitingCommit[idx].append((term, callback))
                    else:
                        self.__transport.send(requestNode, {
                            'type': 'apply_command_response',
                            'request_id': requestID,
                            'log_idx': idx,
                            'log_term': term,
                        })
                    if not self.__conf.appendEntriesUseBatch:
                        self.__sendAppendEntries()
                else:

                    if requestNode is None:
                        if callback is not None:
                            callback(None, FAIL_REASON.REQUEST_DENIED)
                    else:
                        self.__transport.send(requestNode, {
                            'type': 'apply_command_response',
                            'request_id': requestID,
                            'error': FAIL_REASON.REQUEST_DENIED,
                        })

            elif self.__raftLeader is not None:
                if requestNode is None:
                    message = {
                        'type': 'apply_command',
                        'command': command,
                    }

                    if callback is not None:
                        self.__commandsLocalCounter += 1
                        self.__commandsWaitingReply[self.__commandsLocalCounter] = callback
                        message['request_id'] = self.__commandsLocalCounter

                    self.__transport.send(self.__raftLeader, message)
                else:
                    self.__transport.send(requestNode, {
                        'type': 'apply_command_response',
                        'request_id': requestID,
                        'error': FAIL_REASON.NOT_LEADER,
                    })
            else:
                self.__callErrCallback(FAIL_REASON.MISSING_LEADER, callback)

    def _autoTickThread(self):
        try:
            self.__transport.tryGetReady()
        except TransportNotReadyError:
            logger.exception('failed to perform initialization')
            return
        finally:
            self.__initialised.set()
        time.sleep(0.1)
        try:
            while True:
                if not self.__mainThread.is_alive():
                    break
                if self.__destroying:
                    self._doDestroy()
                    break
                try:
                    self._onTick(self.__conf.autoTickPeriod)
                except Exception:
                    # log, wait a little and retry
                    logger.exception('failed _onTick in _autoTickThread')
                    time.sleep(self.__conf.autoTickPeriod)
        except ReferenceError:
            pass

    def doTick(self, timeToWait=0.0):
        """Performs single tick. Should be called manually if `autoTick <#pysyncobj.SyncObjConf.autoTick>`_ disabled

        :param timeToWait: max time to wait for next tick. If zero - perform single tick without waiting for new events.
            Otherwise - wait for new socket event and return.
        :type timeToWait: float
        """
        assert not self.__conf.autoTick
        self._onTick(timeToWait)

    def _onTick(self, timeToWait=0.0):
        if not self.__transport.ready:
            try:
                self.__transport.tryGetReady()
            except TransportNotReadyError:
                # Implicitly handled in the 'if not self.__transport.ready' below
                pass

        if not self.__transport.ready:
            time.sleep(timeToWait)
            self.__applyLogEntries()
            return

        if self.__needLoadDumpFile:
            if self.__conf.fullDumpFile is not None and os.path.isfile(self.__conf.fullDumpFile):
                self.__loadDumpFile(clearJournal=False)
            self.__needLoadDumpFile = False

        workTime = monotonicTime() - self.__startTime
        if workTime > self.__numOneSecondDumps:
            self.__numOneSecondDumps += 1
            self.__raftLog.onOneSecondTimer()

        if self.__raftState in (_RAFT_STATE.FOLLOWER, _RAFT_STATE.CANDIDATE) and self.__selfNode is not None:
            if self.__raftElectionDeadline < monotonicTime() and self.__connectedToAnyone():
                self.__raftElectionDeadline = monotonicTime() + self.__generateRaftTimeout()
                self.__transferInProgress = False
                self.__raftLeader = None
                self.__setState(_RAFT_STATE.CANDIDATE)
                self.__setCurrentTerm(self.__raftCurrentTerm + 1, self.__selfNode.id)
                self.__votesCount = 1
                for node in self.__otherNodes:
                    self.__transport.send(node, {
                        'type': 'request_vote',
                        'term': self.__raftCurrentTerm,
                        'last_log_index': self.__getCurrentLogIndex(),
                        'last_log_term': self.__getCurrentLogTerm(),
                    })
                self.__onLeaderChanged()
                if self.__votesCount > (len(self.__otherNodes) + 1) / 2:
                    self.__onBecomeLeader()

        if self.__raftState == _RAFT_STATE.LEADER:

            commitIdx = self.__raftCommitIndex
            nextCommitIdx = self.__raftCommitIndex

            while commitIdx < self.__getCurrentLogIndex():
                commitIdx += 1
                count = 1
                for node in self.__otherNodes:
                    if self.__raftMatchIndex[node] >= commitIdx:
                        count += 1
                if count <= (len(self.__otherNodes) + 1) / 2:
                    break
                entries = self.__getEntries(commitIdx, 1)
                if not entries:
                    continue
                commitTerm = entries[0][2]
                if commitTerm != self.__raftCurrentTerm:
                    continue
                nextCommitIdx = commitIdx

            if self.__raftCommitIndex != nextCommitIdx:
                self.__raftCommitIndex = nextCommitIdx
                self.__raftLog.setRaftCommitIndex(self.__raftCommitIndex)

            self.__leaderCommitIndex = self.__raftCommitIndex
            deadline = monotonicTime() - self.__conf.leaderFallbackTimeout
            count = 1
            for node in self.__otherNodes:
                if self.__lastResponseTime[node] > deadline:
                    count += 1
            if count <= (len(self.__otherNodes) + 1) / 2:
                self.__setState(_RAFT_STATE.FOLLOWER)
                self.__raftLeader = None

        needSendAppendEntries = self.__applyLogEntries()

        if self.__raftState == _RAFT_STATE.LEADER:
            if monotonicTime() > self.__newAppendEntriesTime or needSendAppendEntries:
                self.__sendAppendEntries()

        if not self.__onReadyCalled and self.__raftLastApplied == self.__leaderCommitIndex:
            if self.__conf.onReady:
                self.__conf.onReady()
            self.__onReadyCalled = True

        self._checkCommandsToApply()
        self.__tryLogCompaction()

        with self.__onTickCallbacksLock:
            for callback in self.__onTickCallbacks:
                callback()

        self._poller.poll(timeToWait)

    def __preflightLogEntries(self, entries):
        """Reject entries whose owner cannot be determined before applying any.

        The check is intentionally side-effect free: a missing or conflicting
        legacy manifest must be reported before the first entry mutates state.
        """
        for entry in entries:
            command = entry[0]
            commandType = ord(command[:1])
            if commandType == _COMMAND_TYPE.REGULAR_IDENTITY:
                ownerIdentity, methodName = pickle.loads(command[1:])[:2]
                if ownerIdentity != _SELF_OWNER and \
                        ownerIdentity not in self.__consumerByIdentity:
                    raise SyncObjExceptionAmbiguousOwnership(
                        'log entry refers to consumer identity %r, which is not '
                        'configured' % (ownerIdentity,))
                if (ownerIdentity, methodName) not in self.__methodKeyToID:
                    raise SyncObjExceptionAmbiguousOwnership(
                        'log entry refers to unknown method %s.%s' %
                        (ownerIdentity, methodName))
            elif commandType == _COMMAND_TYPE.REGULAR:
                if not self.__identityModeEnabled:
                    continue
                parsed = pickle.loads(command[1:])
                funcID = parsed if not isinstance(parsed, tuple) else parsed[0]
                if funcID not in self.__legacyIdToMethod and \
                        funcID not in self.__legacyUnresolved:
                    raise SyncObjExceptionAmbiguousOwnership(
                        'cannot determine ownership of numeric log command %r: the '
                        'legacy method manifest does not describe it (a positional '
                        'guess is not allowed)' % (funcID,))

    def __applyLogEntries(self):
        needSendAppendEntries = False

        if self.__raftCommitIndex > self.__raftLastApplied:
            count = self.__raftCommitIndex - self.__raftLastApplied
            entries = self.__getEntries(self.__raftLastApplied + 1, count)
            self.__preflightLogEntries(entries)
            for entry in entries:
                try:
                    currentTermID = entry[2]
                    subscribers = self.__commandsWaitingCommit.pop(entry[1], [])
                    res = self.__doApplyCommand(entry[0])
                    for subscribeTermID, callback in subscribers:
                        if subscribeTermID == currentTermID:
                            callback(res, FAIL_REASON.SUCCESS)
                        else:
                            callback(None, FAIL_REASON.DISCARDED)

                    self.__raftLastApplied += 1
                except SyncObjExceptionWrongVer as e:
                    logger.error(
                        'request to switch to unsupported code version (self version: %d, requested version: %d)' %
                        (self.__selfCodeVersion, e.ver))

            if not self.__conf.appendEntriesUseBatch:
                needSendAppendEntries = True

        return needSendAppendEntries

    def addOnTickCallback(self, callback):
        with self.__onTickCallbacksLock:
            self.__onTickCallbacks.append(callback)

    def removeOnTickCallback(self, callback):
        with self.__onTickCallbacksLock:
            try:
                self.__onTickCallbacks.remove(callback)
            except ValueError:
                # callback not in list, ignore
                pass

    def isNodeConnected(self, node):
        """
        Checks if the given node is connected
        :param node: node to check
        :type node: Node
        :rtype: bool
        """
        return node in self.__connectedNodes

    @property
    def selfNode(self):
        """
        :rtype: Node
        """
        return self.__selfNode

    @property
    def otherNodes(self):
        """
        :rtype: set of Node
        """
        return self.__otherNodes.copy()

    @property
    def readonlyNodes(self):
        """
        :rtype: set of Node
        """
        return self.__readonlyNodes.copy()

    @property
    def raftLastApplied(self):
        """
        :rtype: int
        """
        return self.__raftLastApplied

    @property
    def raftCommitIndex(self):
        """
        :rtype: int
        """
        return self.__raftCommitIndex

    @property
    def raftCurrentTerm(self):
        """
        :rtype: int
        """
        return self.__raftCurrentTerm

    @property
    def hasQuorum(self):
        '''
        Does the cluster have a quorum according to this node

        :rtype: bool
        '''

        nodes = self.__otherNodes
        node_count = len(nodes)
        # Get number of connected nodes that participate in cluster quorum
        connected_count = len(nodes.intersection(self.__connectedNodes))

        if self.__selfNode is not None:
            # This node participates in cluster quorum
            connected_count += 1
            node_count += 1

        return connected_count > node_count / 2

    def getStatus(self):
        """Dumps different debug info about cluster to dict and return it"""

        status = {}
        status['version'] = VERSION
        status['revision'] = 'deprecated'
        status['self'] = self.selfNode
        status['state'] = self.__raftState
        status['leader'] = self.__raftLeader
        status['has_quorum'] = self.hasQuorum
        status['partner_nodes_count'] = len(self.__otherNodes)
        for node in self.__otherNodes:
            status['partner_node_status_server_' + node.id] = 2 if self.isNodeConnected(node) else 0
        status['readonly_nodes_count'] = len(self.__readonlyNodes)
        for node in self.__readonlyNodes:
            status['readonly_node_status_server_' + node.id] = 2 if self.isNodeConnected(node) else 0
        status['log_len'] = len(self.__raftLog)
        status['last_applied'] = self.raftLastApplied
        status['commit_idx'] = self.raftCommitIndex
        status['raft_term'] = self.raftCurrentTerm
        status['next_node_idx_count'] = len(self.__raftNextIndex)
        for node, idx in iteritems(self.__raftNextIndex):
            status['next_node_idx_server_' + node.id] = idx
        status['match_idx_count'] = len(self.__raftMatchIndex)
        for node, idx in iteritems(self.__raftMatchIndex):
            status['match_idx_server_' + node.id] = idx
        status['leader_commit_idx'] = self.__leaderCommitIndex
        status['uptime'] = int(monotonicTime() - self.__startTime)
        status['self_code_version'] = self.__selfCodeVersion
        status['enabled_code_version'] = self.__enabledCodeVersion
        return status

    def _getStatus(self, args, callback):
        callback(self.getStatus(), None)

    def printStatus(self):
        """Dumps different debug info about cluster to default logger"""
        status = self.getStatus()
        for k, v in iteritems(status):
            logger.info('%s: %s' % (str(k), str(v)))

    def _printStatus(self):
        self.printStatus()

    def forceLogCompaction(self):
        """Force to start log compaction (without waiting required time or required number of entries)"""
        self.__forceLogCompaction = True

    def _forceLogCompaction(self):
        self.forceLogCompaction()

    def __doApplyCommand(self, command):
        commandType = ord(command[:1])
        # Skip no-op and membership change commands
        if commandType == _COMMAND_TYPE.VERSION:
            ver = pickle.loads(command[1:])
            if self.__selfCodeVersion < ver:
                raise SyncObjExceptionWrongVer(ver)
            oldVer = self.__enabledCodeVersion
            self.__enabledCodeVersion = ver
            callback = self.__conf.onCodeVersionChanged
            self.__onSetCodeVersion(ver)
            if callback is not None:
                callback(oldVer, ver)
            return

        #  This is required only after node restarts and apply journal
        # for normal case it is already done earlier and calls will be ignored
        clusterChangeRequest = self.__parseChangeClusterRequest(command)
        if clusterChangeRequest is not None:
             self.__doChangeCluster(clusterChangeRequest)
             return

        if commandType != _COMMAND_TYPE.REGULAR and \
                commandType != _COMMAND_TYPE.REGULAR_IDENTITY:
            return
        command = pickle.loads(command[1:])

        if commandType == _COMMAND_TYPE.REGULAR_IDENTITY:
            return self.__resolveIdentityCommand(command)

        args = []
        kwargs = {}
        if not isinstance(command, tuple):
            funcID = command
        elif len(command) == 2:
            funcID, args = command
        else:
            funcID, args, newKwArgs = command
            kwargs.update(newKwArgs)

        if funcID not in self._idToMethod:
            # The numeric ID is meaningless without the registry it was produced
            # under. Resolve it through the legacy manifest, never by position.
            return self.__resolveLegacyNumericCommand(funcID, args, kwargs)

        if not self.__identityModeEnabled:
            # Historical path: the numeric registry is the authority.
            return self._idToMethod[funcID](*args, **dict(kwargs, _doApply=True))

        # Identity mode: every numeric ID in the log was produced under the old
        # registration order (this node encodes identity commands after the
        # registry switch, and old nodes never emit identity commands). Its owner
        # therefore comes exclusively from the legacy manifest, not from where a
        # similarly-typed consumer happens to sit in the new list.
        return self.__resolveLegacyNumericCommand(funcID, args, kwargs)

    def __onMessageReceived(self, node, message):

        if message['type'] == 'timeout_now' and self.__selfNode is not None:
            # Graceful leadership transfer (Raft 'TimeoutNow'): trigger an
            # immediate election, but only on request of the current leader.
            if message['term'] >= self.__raftCurrentTerm and node == self.__raftLeader:
                self.__raftElectionDeadline = 0
                self.__transferInProgress = True

        if message['type'] == 'request_vote' and self.__selfNode is not None:

            if message['term'] > self.__raftCurrentTerm:
                self.__setCurrentTerm(message['term'], None)
                self.__setState(_RAFT_STATE.FOLLOWER)
                self.__raftLeader = None

            if self.__raftState in (_RAFT_STATE.FOLLOWER, _RAFT_STATE.CANDIDATE):
                lastLogTerm = message['last_log_term']
                lastLogIdx = message['last_log_index']
                if message['term'] >= self.__raftCurrentTerm:
                    if lastLogTerm < self.__getCurrentLogTerm():
                        return
                    if lastLogTerm == self.__getCurrentLogTerm() and \
                            lastLogIdx < self.__getCurrentLogIndex():
                        return
                    if self.__votedForNodeId is not None:
                        return

                    self.__setCurrentTerm(self.__raftCurrentTerm, node.id)

                    self.__raftElectionDeadline = monotonicTime() + self.__generateRaftTimeout()
                    self.__transport.send(node, {
                        'type': 'response_vote',
                        'term': message['term'],
                    })

        if message['type'] == 'append_entries' and message['term'] >= self.__raftCurrentTerm:
            if message['term'] > self.__raftCurrentTerm:
                # A new leader at a higher term supersedes any pending transfer.
                self.__transferInProgress = False
            if not self.__transferInProgress:
                # While a transfer is pending, ignore same-term heartbeats from
                # the outgoing leader so they can't postpone the forced election.
                self.__raftElectionDeadline = monotonicTime() + self.__generateRaftTimeout()
            if self.__raftLeader != node:
                self.__onLeaderChanged()
            self.__raftLeader = node
            if message['term'] > self.__raftCurrentTerm:
                self.__setCurrentTerm(message['term'], None)
            self.__setState(_RAFT_STATE.FOLLOWER)
            newEntries = message.get('entries', [])
            serialized = message.get('serialized', None)
            self.__leaderCommitIndex = leaderCommitIndex = message['commit_index']

            # Regular append entries
            if 'prevLogIdx' in message:
                transmission = message.get('transmission', None)
                if transmission is not None:
                    if transmission == 'start':
                        self.__recvTransmission = message['data']
                        self.__sendNextNodeIdx(node, success=False, reset=False)
                        return
                    elif transmission == 'process':
                        self.__recvTransmission += message['data']
                        self.__sendNextNodeIdx(node, success=False, reset=False)
                        return
                    elif transmission == 'finish':
                        self.__recvTransmission += message['data']
                        newEntries = [pickle.loads(self.__recvTransmission)]
                        self.__recvTransmission = ''
                    else:
                        raise Exception('Wrong transmission type')

                prevLogIdx = message['prevLogIdx']
                prevLogTerm = message['prevLogTerm']
                prevEntries = self.__getEntries(prevLogIdx)
                if not prevEntries:
                    self.__sendNextNodeIdx(node, success=False, reset=True)
                    return
                if prevEntries[0][2] != prevLogTerm:
                    self.__sendNextNodeIdx(node, nextNodeIdx = prevLogIdx, success = False, reset=True)
                    return
                if len(prevEntries) > 1:
                    # rollback cluster changes
                    if self.__conf.dynamicMembershipChange:
                        for entry in reversed(prevEntries[1:]):
                            clusterChangeRequest = self.__parseChangeClusterRequest(entry[0])
                            if clusterChangeRequest is not None:
                                self.__doChangeCluster(clusterChangeRequest, reverse=True)

                    if prevLogIdx + 1 <= self.__raftLastApplied:
                        logger.critical(
                            'truncating already-applied log entries from index %d '
                            '(last applied: %d) - state machine may have diverged from '
                            'other cluster members', prevLogIdx + 1, self.__raftLastApplied)
                    self.__deleteEntriesFrom(prevLogIdx + 1)
                for entry in newEntries:
                    self.__raftLog.add(*entry)

                # apply cluster changes
                if self.__conf.dynamicMembershipChange:
                    for entry in newEntries:
                        clusterChangeRequest = self.__parseChangeClusterRequest(entry[0])
                        if clusterChangeRequest is not None:
                            self.__doChangeCluster(clusterChangeRequest)

                nextNodeIdx = prevLogIdx + 1
                if newEntries:
                    nextNodeIdx = newEntries[-1][1] + 1

                self.__sendNextNodeIdx(node, nextNodeIdx=nextNodeIdx, success=True)

            # Install snapshot
            elif serialized is not None:
                if self.__serializer.setTransmissionData(serialized):
                    self.__loadDumpFile(clearJournal=True)
                    self.__sendNextNodeIdx(node, success=True)

            # If the log was truncated below the commit index, don't persist a commit
            # index covering entries that are no longer the committed ones.
            if self.__raftCommitIndex > self.__getCurrentLogIndex():
                self.__raftCommitIndex = self.__getCurrentLogIndex()

            if leaderCommitIndex > self.__raftCommitIndex:
                self.__raftCommitIndex = min(leaderCommitIndex, self.__getCurrentLogIndex())

            self.__raftLog.setRaftCommitIndex(self.__raftCommitIndex)

        if message['type'] == 'apply_command':
            if 'request_id' in message:
                self._applyCommand(message['command'], (node, message['request_id']))
            else:
                self._applyCommand(message['command'], None)

        if message['type'] == 'apply_command_response':
            requestID = message['request_id']
            error = message.get('error', None)
            callback = self.__commandsWaitingReply.pop(requestID, None)
            if callback is not None:
                if error is not None:
                    callback(None, error)
                else:
                    idx = message['log_idx']
                    term = message['log_term']
                    if idx <= self.__raftLastApplied:
                        # Stale apply_command_response:
                        # The log position was already applied (e.g. via snapshot installed after leader reconnect)
                        # before this response was processed. Or the leader was restarted without notice (and remains leader).
                        # The command outcome cannot be tracked; signal the caller so it can retry rather than hanging
                        # forever.
                        callback(None, FAIL_REASON.UNKNOWN_OUTCOME)
                    else:
                        self.__commandsWaitingCommit[idx].append((term, callback))

        if self.__raftState == _RAFT_STATE.CANDIDATE:
            if message['type'] == 'response_vote' and message['term'] == self.__raftCurrentTerm:
                self.__votesCount += 1

                if self.__votesCount > (len(self.__otherNodes) + 1) / 2:
                    self.__onBecomeLeader()

        if self.__raftState == _RAFT_STATE.LEADER:
            if message['type'] == 'next_node_idx':
                reset = message['reset']
                nextNodeIdx = message['next_node_idx']
                success = message['success']

                currentNodeIdx = nextNodeIdx - 1
                if reset:
                    self.__raftNextIndex[node] = nextNodeIdx
                if success:
                    if self.__raftMatchIndex[node] < currentNodeIdx:
                        self.__raftMatchIndex[node] = currentNodeIdx
                        self.__raftNextIndex[node] = nextNodeIdx
                self.__lastResponseTime[node] = monotonicTime()

    def __callErrCallback(self, err, callback):
        if callback is None:
            return
        if isinstance(callback, tuple):
            requestNode, requestID = callback
            self.__transport.send(requestNode, {
                'type': 'apply_command_response',
                'request_id': requestID,
                'error': err,
            })
            return
        callback(None, err)

    def __sendNextNodeIdx(self, node, reset=False, nextNodeIdx = None, success = False):
        if nextNodeIdx is None:
            nextNodeIdx = self.__getCurrentLogIndex() + 1
        self.__transport.send(node, {
            'type': 'next_node_idx',
            'next_node_idx': nextNodeIdx,
            'reset': reset,
            'success': success,
        })

    def __generateRaftTimeout(self):
        minTimeout = self.__conf.raftMinTimeout
        maxTimeout = self.__conf.raftMaxTimeout
        return minTimeout + (maxTimeout - minTimeout) * random.random()

    def __onReadonlyNodeConnected(self, node):
        self.__readonlyNodes.add(node)
        self.__connectedNodes.add(node)
        self.__raftNextIndex[node] = self.__getCurrentLogIndex() + 1
        self.__raftMatchIndex[node] = 0

    def __onReadonlyNodeDisconnected(self, node):
        self.__readonlyNodes.discard(node)
        self.__connectedNodes.discard(node)
        self.__raftNextIndex.pop(node, None)
        self.__raftMatchIndex.pop(node, None)
        node._destroy()

    def __onNodeConnected(self, node):
        self.__connectedNodes.add(node)

    def __onNodeDisconnected(self, node):
        self.__connectedNodes.discard(node)

    def __getCurrentLogIndex(self):
        return self.__raftLog[-1][1]

    def __getCurrentLogTerm(self):
        return self.__raftLog[-1][2]

    def __getPrevLogIndexTerm(self, nextNodeIndex):
        prevIndex = nextNodeIndex - 1
        entries = self.__getEntries(prevIndex, 1)
        if entries:
            return prevIndex, entries[0][2]
        return None, None

    def __getEntries(self, fromIDx, count=None, maxSizeBytes = None):
        firstEntryIDx = self.__raftLog[0][1]
        if fromIDx is None or fromIDx < firstEntryIDx:
            return []
        diff = fromIDx - firstEntryIDx
        if count is None:
            result = self.__raftLog[diff:]
        else:
            result = self.__raftLog[diff:diff + count]
        if maxSizeBytes is None:
            return result
        totalSize = 0
        i = 0
        for i, entry in enumerate(result):
            totalSize += len(entry[0])
            if totalSize >= maxSizeBytes:
                break
        return result[:i + 1]

    def _isLeader(self):
        """ Check if current node has a leader state.
        WARNING: there could be multiple leaders at the same time!

        :return: True if leader, False otherwise
        :rtype: bool
        """
        return self.__raftState == _RAFT_STATE.LEADER

    def _getLeader(self):
        """ Returns last known leader.

        WARNING: this information could be outdated, eg. there could be another leader selected!
        WARNING: there could be multiple leaders at the same time!

        :return: the last known leader node.
        :rtype: Node
        """
        return self.__raftLeader

    def isReady(self):
        """Check if current node is initially synced with others and has an actual data.

        :return: True if ready, False otherwise
        :rtype: bool
        """
        return self.__onReadyCalled

    def _isReady(self):
        return self.isReady()

    def _getTerm(self):
        return self.__raftCurrentTerm

    def _getRaftLogSize(self):
        return len(self.__raftLog)

    def __deleteEntriesFrom(self, fromIDx):
        firstEntryIDx = self.__raftLog[0][1]
        diff = fromIDx - firstEntryIDx
        if diff < 0:
            return
        self.__raftLog.deleteEntriesFrom(diff)

    def __deleteEntriesTo(self, toIDx):
        firstEntryIDx = self.__raftLog[0][1]
        diff = toIDx - firstEntryIDx
        if diff < 0:
            return
        self.__raftLog.deleteEntriesTo(diff)

    def __onBecomeLeader(self):
        self.__raftLeader = self.__selfNode
        self.__setState(_RAFT_STATE.LEADER)

        self.__lastResponseTime.clear()
        for node in self.__otherNodes | self.__readonlyNodes:
            self.__raftNextIndex[node] = self.__getCurrentLogIndex() + 1
            self.__raftMatchIndex[node] = 0
            self.__lastResponseTime[node] = monotonicTime()

        # No-op command after leader election.
        idx, term = self.__getCurrentLogIndex() + 1, self.__raftCurrentTerm
        self.__raftLog.add(_bchr(_COMMAND_TYPE.NO_OP), idx, term)
        self.__noopIDx = idx
        if not self.__conf.appendEntriesUseBatch:
            self.__sendAppendEntries()

        self.__sendAppendEntries()

    def __setCurrentTerm(self, term, votedForNodeId):
        self.__raftCurrentTerm = term
        self.__votedForNodeId = votedForNodeId
        self.__raftLog.setCurrentTerm(term, votedForNodeId)

    def __setState(self, newState):
        oldState = self.__raftState
        self.__raftState = newState
        callback = self.__conf.onStateChanged
        if callback is not None and oldState != newState:
            callback(oldState, newState)

    def __onLeaderChanged(self):
        for id in sorted(self.__commandsWaitingReply):
            self.__commandsWaitingReply[id](None, FAIL_REASON.LEADER_CHANGED)
        self.__commandsWaitingReply = {}

    def __sendAppendEntries(self):
        self.__newAppendEntriesTime = monotonicTime() + self.__conf.appendEntriesPeriod

        startTime = monotonicTime()

        batchSizeBytes = self.__conf.appendEntriesBatchSizeBytes

        for node in self.__otherNodes | self.__readonlyNodes:
            if node not in self.__connectedNodes:
                self.__serializer.cancelTransmisstion(node)
                continue

            sendSingle = True
            sendingSerialized = False
            nextNodeIndex = self.__raftNextIndex[node]

            while nextNodeIndex <= self.__getCurrentLogIndex() or sendSingle or sendingSerialized:
                if nextNodeIndex > self.__raftLog[0][1]:
                    prevLogIdx, prevLogTerm = self.__getPrevLogIndexTerm(nextNodeIndex)
                    entries = []
                    if nextNodeIndex <= self.__getCurrentLogIndex():
                        entries = self.__getEntries(nextNodeIndex, None, batchSizeBytes)
                        self.__raftNextIndex[node] = entries[-1][1] + 1

                    if len(entries) == 1 and len(entries[0][0]) >= batchSizeBytes:
                        entry = pickle.dumps(entries[0])
                        for pos in xrange(0, len(entry), batchSizeBytes):
                            currData = entry[pos:pos + batchSizeBytes]
                            if pos == 0:
                                transmission = 'start'
                            elif pos + batchSizeBytes >= len(entries[0][0]):
                                transmission = 'finish'
                            else:
                                transmission = 'process'
                            message = {
                                'type': 'append_entries',
                                'transmission': transmission,
                                'data': currData,
                                'term': self.__raftCurrentTerm,
                                'commit_index': self.__raftCommitIndex,
                                'prevLogIdx': prevLogIdx,
                                'prevLogTerm': prevLogTerm,
                            }
                            self.__transport.send(node, message)
                            if node not in self.__connectedNodes:
                                break
                    else:
                        message = {
                            'type': 'append_entries',
                            'term': self.__raftCurrentTerm,
                            'commit_index': self.__raftCommitIndex,
                            'entries': entries,
                            'prevLogIdx': prevLogIdx,
                            'prevLogTerm': prevLogTerm,
                        }
                        self.__transport.send(node, message)
                        if node not in self.__connectedNodes:
                            break
                else:
                    transmissionData = self.__serializer.getTransmissionData(node)
                    message = {
                        'type': 'append_entries',
                        'term': self.__raftCurrentTerm,
                        'commit_index': self.__raftCommitIndex,
                        'serialized': transmissionData,
                    }
                    self.__transport.send(node, message)
                    if node not in self.__connectedNodes:
                        break

                    if transmissionData is not None:
                        isLast = transmissionData[2]
                        if isLast:
                            self.__raftNextIndex[node] = self.__raftLog[1][1] + 1
                            sendingSerialized = False
                        else:
                            sendingSerialized = True
                    else:
                        sendingSerialized = False

                nextNodeIndex = self.__raftNextIndex[node]

                sendSingle = False

                delta = monotonicTime() - startTime
                if delta > self.__conf.appendEntriesPeriod:
                    break

    def __connectedToAnyone(self):
        return len(self.__connectedNodes) > 0 or len(self.__otherNodes) == 0

    def _getConf(self):
        return self.__conf

    @property
    def conf(self):
        return self.__conf

    def _getEncryptor(self):
        return self.__encryptor

    @property
    def encryptor(self):
        return self.__encryptor

    def __changeCluster(self, request):
        if self.__raftLastApplied < self.__noopIDx:
            # No-op entry was not commited yet
            return False

        if self.__changeClusterIDx is not None:
            if self.__raftLastApplied >= self.__changeClusterIDx:
                self.__changeClusterIDx = None

        # Previous cluster change request was not commited yet
        if self.__changeClusterIDx is not None:
            return False

        return self.__doChangeCluster(request)

    def __setCodeVersion(self, newVersion):
        self.__enabledCodeVersion = newVersion
        self.__onSetCodeVersion(newVersion)

    def __doChangeCluster(self, request, reverse = False):
        requestType = request[0]
        requestNodeId = request[1]
        if len(request) >= 3:
            requestNode = request[2]
            if not isinstance(requestNode, Node): # Actually shouldn't be necessary, but better safe than sorry.
                requestNode = self.__nodeClass(requestNode)
        else:
            requestNode = self.__nodeClass(requestNodeId)

        if requestType == 'add':
            adding = not reverse
        elif requestType == 'rem':
            adding = reverse
        else:
            return False

        if adding:
            newNode = requestNode
            # Node already exists in cluster
            if newNode == self.__selfNode or newNode in self.__otherNodes:
                return False
            self.__otherNodes.add(newNode)
            self.__raftNextIndex[newNode] = self.__getCurrentLogIndex() + 1
            self.__raftMatchIndex[newNode] = 0
            if self._isLeader():
                self.__lastResponseTime[newNode] = monotonicTime()
            self.__transport.addNode(newNode)
            return True
        else:
            oldNode = requestNode
            if oldNode == self.__selfNode:
                return False
            if oldNode not in self.__otherNodes:
                return False
            self.__otherNodes.discard(oldNode)
            self.__raftNextIndex.pop(oldNode, None)
            self.__raftMatchIndex.pop(oldNode, None)
            self.__transport.dropNode(oldNode)
            return True

    def __parseChangeClusterRequest(self, command):
        commandType = ord(command[:1])
        if commandType != _COMMAND_TYPE.MEMBERSHIP:
            return None
        return pickle.loads(command[1:])

    def __tryLogCompaction(self):
        currTime = monotonicTime()
        serializeState, serializeID = self.__serializer.checkSerializing()

        if serializeState == SERIALIZER_STATE.SUCCESS:
            self.__lastSerializedTime = currTime
            self.__deleteEntriesTo(serializeID)
            self.__lastSerializedEntry = serializeID

        if serializeState == SERIALIZER_STATE.FAILED:
            logger.warning('Failed to store full dump')

        if serializeState != SERIALIZER_STATE.NOT_SERIALIZING:
            return

        if len(self.__raftLog) <= self.__conf.logCompactionMinEntries and \
                                currTime - self.__lastSerializedTime <= self.__conf.logCompactionMinTime and \
                not self.__forceLogCompaction:
            return

        if self.__conf.logCompactionSplit:
            allNodeIds = sorted([node.id for node in (self.__otherNodes | {self.__selfNode})])
            nodesCount = len(allNodeIds)
            selfIdx = allNodeIds.index(self.__selfNode.id)
            interval = self.__conf.logCompactionMinTime
            periodStart = int(currTime / interval ) * interval
            nodeInterval = float(interval) / nodesCount
            nodeIntervalStart = periodStart + selfIdx * nodeInterval
            nodeIntervalEnd = nodeIntervalStart + 0.3 * nodeInterval
            if currTime < nodeIntervalStart or currTime >= nodeIntervalEnd:
                return

        self.__forceLogCompaction = False

        lastAppliedEntries = self.__getEntries(self.__raftLastApplied - 1, 2)
        if len(lastAppliedEntries) < 2 or lastAppliedEntries[0][1] == self.__lastSerializedEntry:
            self.__lastSerializedTime = currTime
            return

        if self.__conf.serializer is None:
            selfData = dict([(k, v) for k, v in iteritems(self.__dict__) if k not in self.__properies])
            data = selfData
            if self.__consumers:
                data = [selfData] + self.__serializeConsumers()
        else:
            data = None
        cluster = self.__otherNodes | {self.__selfNode}
        self.__serializer.serialize((data, lastAppliedEntries[1], lastAppliedEntries[0], cluster), lastAppliedEntries[0][1])

    def __loadDumpFile(self, clearJournal):
        try:
            data = self.__serializer.deserialize()
        except Exception:
            logger.exception('failed to load full dump')
            return

        # Plan every consumer restore before touching any state. A snapshot whose
        # ownership is ambiguous must be rejected as a whole, not partially
        # applied to whichever dictionary happens to sit at the new position.
        try:
            if data[0] is not None:
                if self.__consumers:
                    if not isinstance(data[0], (list, tuple)):
                        raise SyncObjExceptionAmbiguousOwnership(
                            'snapshot is missing the consumer state section')
                    selfData = data[0][0]
                    consumerStates, enabledVersion = self.__planSnapshotConsumers(
                        data[0][1:])
                else:
                    selfData = data[0]
                    consumerStates = {}
                    enabledVersion = None
            else:
                selfData = None
                consumerStates = {}
                enabledVersion = None
        except SyncObjExceptionAmbiguousOwnership:
            logger.exception('refusing to load full dump: consumer ownership is ambiguous')
            raise
        except Exception:
            logger.exception('failed to load full dump')
            return

        try:
            if data[0] is not None:
                for k, v in iteritems(selfData):
                    self.__dict__[k] = v
                for identity, state in iteritems(consumerStates):
                    self.__consumerByIdentity[identity]._deserialize(state)

            if clearJournal or \
                    len(self.__raftLog) < 2 or \
                    self.__raftLog[0] != data[2] or \
                    self.__raftLog[1] != data[1]:
                self.__raftLog.clear()
                self.__raftLog.add(*data[2])
                self.__raftLog.add(*data[1])

            self.__raftLastApplied = data[1][1]

            if self.__raftCommitIndex > self.__getCurrentLogIndex():
                logger.warning('raftCommitIndex (%d) exceeds journal end (%d) after loading dump, '
                               'likely due to unsynced journal entries lost on crash; '
                               'clamping to journal end',
                               self.__raftCommitIndex, self.__getCurrentLogIndex())
                self.__raftCommitIndex = self.__getCurrentLogIndex()
                self.__raftLog.setRaftCommitIndex(self.__raftCommitIndex)

            if self.__conf.dynamicMembershipChange:
                self.__updateClusterConfiguration([node for node in data[3] if node != self.__selfNode])
            # Snapshots taken after the registry switch record the enabled version
            # so that switching does not need to be replayed from the truncated log.
            if enabledVersion is not None and enabledVersion != self.__enabledCodeVersion:
                oldVer = self.__enabledCodeVersion
                self.__enabledCodeVersion = enabledVersion
                callback = self.__conf.onCodeVersionChanged
                self.__onSetCodeVersion(enabledVersion)
                if callback is not None:
                    callback(oldVer, enabledVersion)
            elif enabledVersion is None:
                # Legacy snapshot without version metadata: replaying the log tail
                # restores the actual version. Keep the historical reset here.
                self.__onSetCodeVersion(0)
            else:
                self.__onSetCodeVersion(enabledVersion)
        except SyncObjExceptionAmbiguousOwnership:
            logger.exception('refusing to load full dump: consumer ownership is ambiguous')
            raise
        except Exception:
            logger.exception('failed to load full dump')

    def __updateClusterConfiguration(self, newNodes):
        # newNodes: list of Node or node ID
        newNodes = {self.__nodeClass(node) if not isinstance(node, Node) else node for node in newNodes}
        nodesToRemove = self.__otherNodes - newNodes
        nodesToAdd = newNodes - self.__otherNodes
        for node in nodesToRemove:
            self.__raftNextIndex.pop(node, None)
            self.__raftMatchIndex.pop(node, None)
            self.__transport.dropNode(node)
        self.__otherNodes = newNodes
        for node in nodesToAdd:
            self.__transport.addNode(node)
            self.__raftNextIndex[node] = self.__getCurrentLogIndex() + 1
            self.__raftMatchIndex[node] = 0

def __copy_func(f, name):
    if is_py3:
        res = types.FunctionType(f.__code__, f.__globals__, name, f.__defaults__, f.__closure__)
        res.__dict__ = f.__dict__
    else:
        res = types.FunctionType(f.func_code, f.func_globals, name, f.func_defaults, f.func_closure)
        res.func_dict = f.func_dict
    return res

class AsyncResult(object):
    def __init__(self):
        self.result = None
        self.error = None
        self.event = threading.Event()

    def onResult(self, res, err):
        self.result = res
        self.error = err
        self.event.set()


def replicated(*decArgs, **decKwargs):
    """Replicated decorator. Use it to mark your class members that modifies
    a class state. Function will be called asynchronously. Function accepts
    flowing additional parameters (optional):
        'callback': callback(result, failReason), failReason - `FAIL_REASON <#pysyncobj.FAIL_REASON>`_.
        'sync': True - to block execution and wait for result, False - async call. If callback is passed,
            'sync' option is ignored.
        'timeout': if 'sync' is enabled, and no result is available for 'timeout' seconds -
            SyncObjException will be raised.
    These parameters are reserved and should not be used in kwargs of your replicated method.

    :param func: arbitrary class member
    :type func: function
    :param ver: (optional) - code version (for zero deployment)
    :type ver: int
    """
    def replicatedImpl(func):
        def newFunc(self, *args, **kwargs):

            if kwargs.pop('_doApply', False):
                return func(self, *args, **kwargs)
            else:
                if isinstance(self, SyncObj):
                    syncObj = self
                    ownerIdentity = _SELF_OWNER
                elif isinstance(self, SyncObjConsumer):
                    syncObj = self._syncObj
                    if syncObj is None:
                        raise SyncObjException(
                            'consumer is not attached to a SyncObj')
                    # The registry is authoritative: a stale _consumerID on the
                    # instance must not override the identity it was registered
                    # under.
                    ownerIdentity = syncObj._identityForConsumer(self)
                else:
                    raise SyncObjException("Class should be inherited from SyncObj or SyncObjConsumer")

                callback = kwargs.pop('callback', None)
                sync = kwargs.pop('sync', False)
                if callback is not None:
                    sync = False

                if sync:
                    asyncResult = AsyncResult()
                    callback = asyncResult.onResult

                timeout = kwargs.pop('timeout', None)
                command, commandType = syncObj._makeReplicatedCommand(
                    ownerIdentity, func.__name__, args, kwargs)
                syncObj._applyCommand(command, callback, commandType)

                if sync:
                    res = asyncResult.event.wait(timeout)
                    if not res:
                        raise SyncObjException('Timeout')
                    if not asyncResult.error == 0:
                        raise SyncObjException(asyncResult.error)
                    return asyncResult.result

        func_dict = newFunc.__dict__ if is_py3 else newFunc.func_dict
        func_dict['replicated'] = True
        func_dict['ver'] = int(decKwargs.get('ver', 0))
        func_dict['origName'] = func.__name__

        callframe = sys._getframe(1 if decKwargs else 2)
        namespace = callframe.f_locals
        newFuncName = func.__name__ + '_v' + str(func_dict['ver'])
        namespace[newFuncName] = __copy_func(newFunc, newFuncName)
        functools.update_wrapper(newFunc, func)
        return newFunc

    if len(decArgs) == 1 and len(decKwargs) == 0 and callable(decArgs[0]):
        return replicatedImpl(decArgs[0])

    return replicatedImpl

def replicated_sync(*decArgs, **decKwargs):
    def replicated_sync_impl(func, timeout = None):
        """Same as replicated, but synchronous by default.

        :param func: arbitrary class member
        :type func: function
        :param timeout: time to wait (seconds). Default: None
        :type timeout: float or None
        """

        def newFunc(self, *args, **kwargs):
            if kwargs.get('_doApply', False):
                return replicated(func)(self, *args, **kwargs)
            else:
                kwargs.setdefault('timeout', timeout)
                kwargs.setdefault('sync', True)
                return replicated(func)(self, *args, **kwargs)
        func_dict = newFunc.__dict__ if is_py3 else newFunc.func_dict
        func_dict['replicated'] = True
        func_dict['ver'] = int(decKwargs.get('ver', 0))
        func_dict['origName'] = func.__name__

        callframe = sys._getframe(1 if decKwargs else 2)
        namespace = callframe.f_locals
        newFuncName = func.__name__ + '_v' + str(func_dict['ver'])
        namespace[newFuncName] = __copy_func(newFunc, newFuncName)
        functools.update_wrapper(newFunc, func)
        return newFunc

    if len(decArgs) == 1 and len(decKwargs) == 0 and callable(decArgs[0]):
        return replicated_sync_impl(decArgs[0])

    return replicated_sync_impl
