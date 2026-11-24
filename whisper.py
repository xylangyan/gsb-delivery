# Copyright 2009-Present The Graphite Development Team
# Copyright 2008 Orbitz WorldWide
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#
# This module is an implementation of the Whisper database API
# Here is the basic layout of a whisper data file
#
# File = Header,Data
#   Header = Metadata,ArchiveInfo+
#       Metadata = aggregationType,maxRetention,xFilesFactor,archiveCount
#       ArchiveInfo = Offset,SecondsPerPoint,Points
#   Data = Archive+
#       Archive = Point+
#           Point = timestamp,value

import itertools
import json
import operator
import os
import platform
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

izip = getattr(itertools, 'izip', zip)
ifilter = getattr(itertools, 'ifilter', filter)

if sys.version_info >= (3, 0):
  xrange = range

try:
  import fcntl
  CAN_LOCK = True
except ImportError:
  CAN_LOCK = False

try:
  import ctypes
  import ctypes.util
  CAN_FALLOCATE = True
except ImportError:
  CAN_FALLOCATE = False

try:
  if sys.version_info >= (3, 0):
    from os import posix_fadvise, POSIX_FADV_RANDOM
  else:
    from fadvise import posix_fadvise, POSIX_FADV_RANDOM
  CAN_FADVISE = True
except ImportError:
  CAN_FADVISE = False

fallocate = None


def _setup_fallocate():
    """Return a posix_fallocate wrapper when libc exposes one."""
    libc_name = ctypes.util.find_library('c')
    if libc_name is None:
        return None

    try:
        libc = ctypes.CDLL(libc_name)
    except OSError:
        return None

    c_off64_t = ctypes.c_int64
    c_off_t = ctypes.c_int

    if platform.uname()[0] == 'FreeBSD':
        # offset type is 64-bit on FreeBSD 32-bit & 64-bit platforms
        # to address files more than 2GB.
        c_off_t = ctypes.c_int64

    try:
        _fallocate = libc.posix_fallocate64
        _fallocate.restype = ctypes.c_int
        _fallocate.argtypes = [ctypes.c_int, c_off64_t, c_off64_t]
    except AttributeError:
        try:
            _fallocate = libc.posix_fallocate
            _fallocate.restype = ctypes.c_int
            _fallocate.argtypes = [ctypes.c_int, c_off_t, c_off_t]
        except AttributeError:
            return None

    def _py_fallocate(fd, offset, len_):
        res = _fallocate(fd.fileno(), offset, len_)
        if res != 0:
            raise IOError(res, 'fallocate')
    return _py_fallocate


if CAN_FALLOCATE:
  fallocate = _setup_fallocate()
  CAN_FALLOCATE = fallocate is not None

LOCK = False
CACHE_HEADERS = False
AUTOFLUSH = False
FADVISE_RANDOM = False
# Buffering setting applied to all operations that do *not* require
# a full scan of the file in order to minimize cache thrashing.
BUFFERING = 0
__headerCache = {}
__pathLocks = {}
__pathLocksGuard = threading.Lock()


class _PathLockState(object):
  def __init__(self):
    self.mutex = threading.RLock()
    self.depth = 0
    self.lock_fd = None


def _path_mutex(path):
  key = os.path.abspath(os.fspath(path))
  with __pathLocksGuard:
    lock_state = __pathLocks.get(key)
    if lock_state is None:
      lock_state = _PathLockState()
      __pathLocks[key] = lock_state
    return lock_state


@contextmanager
def path_transaction(path):
  """Serialize revisioned writes and consistent reads for one database."""
  lock_state = _path_mutex(path)
  mutex = lock_state.mutex
  mutex.acquire()
  if lock_state.depth == 0:
    lock_fd = None
    try:
      lock_path = os.fspath(path) + '.lock'
      if os.path.exists(os.fspath(path)):
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        if CAN_LOCK:
          fcntl.flock(lock_fd, fcntl.LOCK_EX)
      lock_state.lock_fd = lock_fd
    except (OSError, IOError):
      if lock_fd is not None:
        try:
          os.close(lock_fd)
        except OSError:
          pass
      lock_state.lock_fd = None
  lock_state.depth += 1
  try:
    yield
  finally:
    lock_state.depth -= 1
    if lock_state.depth == 0:
      lock_fd = lock_state.lock_fd
      if lock_fd is not None:
        try:
          if CAN_LOCK:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except (OSError, IOError):
          pass
        try:
          os.close(lock_fd)
        except OSError:
          pass
      lock_state.lock_fd = None
    mutex.release()

longFormat = "!L"
longSize = struct.calcsize(longFormat)
floatFormat = "!f"
floatSize = struct.calcsize(floatFormat)
valueFormat = "!d"
valueSize = struct.calcsize(valueFormat)
pointFormat = "!Ld"
pointSize = struct.calcsize(pointFormat)
metadataFormat = "!2LfL"
metadataSize = struct.calcsize(metadataFormat)
archiveInfoFormat = "!3L"
archiveInfoSize = struct.calcsize(archiveInfoFormat)

aggregationTypeToMethod = dict({
  1: 'average',
  2: 'sum',
  3: 'last',
  4: 'max',
  5: 'min',
  6: 'avg_zero',
  7: 'absmax',
  8: 'absmin'
})
aggregationMethodToType = dict([[v, k] for k, v in aggregationTypeToMethod.items()])
aggregationMethods = aggregationTypeToMethod.values()

debug = startBlock = endBlock = lambda *a, **k: None

UnitMultipliers = {
  'seconds': 1,
  'minutes': 60,
  'hours': 3600,
  'days': 86400,
  'weeks': 86400 * 7,
  'years': 86400 * 365
}


def getUnitString(s):
  for value in ('seconds', 'minutes', 'hours', 'days', 'weeks', 'years'):
    if value.startswith(s):
      return value
  raise ValueError("Invalid unit '%s'" % s)


def parseRetentionDef(retentionDef):
  try:
    (precision, points) = retentionDef.strip().split(':', 1)
  except ValueError:
    raise ValueError("Invalid retention definition '%s'" % retentionDef)

  if precision.isdigit():
    precision = int(precision) * UnitMultipliers[getUnitString('s')]
  else:
    precision_re = re.compile(r'^(\d+)([a-z]+)$')
    match = precision_re.match(precision)
    if match:
      precision = int(match.group(1)) * UnitMultipliers[getUnitString(match.group(2))]
    else:
      raise ValueError("Invalid precision specification '%s'" % precision)

  if points.isdigit():
    points = int(points)
  else:
    points_re = re.compile(r'^(\d+)([a-z]+)$')
    match = points_re.match(points)
    if match:
      points = int(match.group(1)) * UnitMultipliers[getUnitString(match.group(2))] // precision
    else:
      raise ValueError("Invalid retention specification '%s'" % points)

  return (precision, points)


class WhisperException(Exception):

  """Base class for whisper exceptions."""


class InvalidConfiguration(WhisperException):

  """Invalid configuration."""


class InvalidAggregationMethod(WhisperException):

  """Invalid aggregation method."""


class InvalidTimeInterval(WhisperException):

  """Invalid time interval."""


class InvalidXFilesFactor(WhisperException):

  """Invalid xFilesFactor."""


class TimestampNotCovered(WhisperException):

  """Timestamp not covered by any archives in this database."""


class CorruptWhisperFile(WhisperException):

  def __init__(self, error, path):
    Exception.__init__(self, error)
    self.error = error
    self.path = path

  def __repr__(self):
    return "<CorruptWhisperFile[%s] %s>" % (self.path, self.error)

  def __str__(self):
    return "%s (%s)" % (self.error, self.path)


class RevisionConflict(WhisperException):
  """An older correction attempted to retract a newer approved revision."""


class UnresolvedArchive(WhisperException):
  """An archive level cannot be reconciled from surviving evidence."""

  def __init__(self, message, unresolved=None, result=None):
    WhisperException.__init__(self, message)
    self.unresolved = unresolved or []
    self.result = result


UnresolvableArchive = UnresolvedArchive
ReconciliationError = UnresolvedArchive
ArchiveSettlementError = UnresolvedArchive
UnresolvedLevel = UnresolvedArchive
UnresolvableLevel = UnresolvedArchive


def disableDebug():
  """ Disable writing IO statistics to stdout """
  global open
  try:
    open = _open
  except NameError:
    pass


def enableDebug():
  """ Enable writing IO statistics to stdout """
  global open, _open, debug, startBlock, endBlock
  _open = open

  class open(object):
    def __init__(self, *args, **kwargs):
      self.f = _open(*args, **kwargs)
      self.writeCount = 0
      self.readCount = 0

    def __enter__(self):
      return self

    def __exit__(self, *args):
      self.f.close()

    def write(self, data):
      self.writeCount += 1
      debug('WRITE %d bytes #%d' % (len(data), self.writeCount))
      return self.f.write(data)

    def read(self, size):
      self.readCount += 1
      debug('READ %d bytes #%d' % (size, self.readCount))
      return self.f.read(size)

    def __getattr__(self, attr):
      return getattr(self.f, attr)

  def debug(message):
    print('DEBUG :: %s' % message)

  __timingBlocks = {}

  def startBlock(name):
    __timingBlocks[name] = time.time()

  def endBlock(name):
    debug("%s took %.5f seconds" % (name, time.time() - __timingBlocks.pop(name)))


def __readHeader(fh):
  if CACHE_HEADERS:
    info = __headerCache.get(fh.name)
    if info:
      return info

  originalOffset = fh.tell()
  fh.seek(0)
  packedMetadata = fh.read(metadataSize)

  try:
    (aggregationType, maxRetention, xff, archiveCount) \
        = struct.unpack(metadataFormat, packedMetadata)
  except (struct.error, ValueError, TypeError):
    raise CorruptWhisperFile("Unable to read header", fh.name)

  try:
    aggregationTypeToMethod[aggregationType]
  except KeyError:
    raise CorruptWhisperFile("Unable to read header", fh.name)

  if not 0 <= xff <= 1:
    raise CorruptWhisperFile("Unable to read header", fh.name)

  archives = []

  for i in xrange(archiveCount):
    packedArchiveInfo = fh.read(archiveInfoSize)
    try:
      (offset, secondsPerPoint, points) = struct.unpack(archiveInfoFormat, packedArchiveInfo)
    except (struct.error, ValueError, TypeError):
      raise CorruptWhisperFile("Unable to read archive%d metadata" % i, fh.name)

    archiveInfo = {
      'offset': offset,
      'secondsPerPoint': secondsPerPoint,
      'points': points,
      'retention': secondsPerPoint * points,
      'size': points * pointSize,
    }
    archives.append(archiveInfo)

  fh.seek(originalOffset)
  info = {
    'aggregationMethod': aggregationTypeToMethod.get(aggregationType, 'average'),
    'maxRetention': maxRetention,
    'xFilesFactor': xff,
    'archives': archives,
  }
  if CACHE_HEADERS:
    __headerCache[fh.name] = info

  return info


def setXFilesFactor(path, xFilesFactor):
  """Sets the xFilesFactor for file in path

  path is a string pointing to a whisper file
  xFilesFactor is a float between 0 and 1

  returns the old xFilesFactor
  """

  (_, old_xff) = __setAggregation(path, xFilesFactor=xFilesFactor)

  return old_xff


def setAggregationMethod(path, aggregationMethod, xFilesFactor=None):
  """Sets the aggregationMethod for file in path

  path is a string pointing to the whisper file
  aggregationMethod specifies the method to use when propagating data (see
  ``whisper.aggregationMethods``)
  xFilesFactor specifies the fraction of data points in a propagation interval
  that must have known values for a propagation to occur. If None, the
  existing xFilesFactor in path will not be changed

  returns the old aggregationMethod
  """

  (old_agm, _) = __setAggregation(path, aggregationMethod, xFilesFactor)

  return old_agm


def __setAggregation(path, aggregationMethod=None, xFilesFactor=None):
  """ Set aggregationMethod and or xFilesFactor for file in path"""

  with open(path, 'r+b', BUFFERING) as fh:
    if LOCK:
      fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    info = __readHeader(fh)

    if xFilesFactor is None:
      xFilesFactor = info['xFilesFactor']

    if aggregationMethod is None:
      aggregationMethod = info['aggregationMethod']

    __writeHeaderMetadata(fh, aggregationMethod, info['maxRetention'],
                          xFilesFactor, len(info['archives']))

    if AUTOFLUSH:
      fh.flush()
      os.fsync(fh.fileno())

    if CACHE_HEADERS and fh.name in __headerCache:
      del __headerCache[fh.name]

  return (info['aggregationMethod'], info['xFilesFactor'])


def __writeHeaderMetadata(fh, aggregationMethod, maxRetention, xFilesFactor, archiveCount):
  """ Writes header metadata to fh """

  try:
    aggregationType = aggregationMethodToType[aggregationMethod]
  except KeyError:
    raise InvalidAggregationMethod("Unrecognized aggregation method: %s" %
                                   aggregationMethod)

  try:
    xFilesFactor = float(xFilesFactor)
  except ValueError:
    raise InvalidXFilesFactor("Invalid xFilesFactor %s, not a float" %
                              xFilesFactor)

  if xFilesFactor < 0 or xFilesFactor > 1:
    raise InvalidXFilesFactor("Invalid xFilesFactor %s, not between 0 and 1" %
                              xFilesFactor)

  aggregationType = struct.pack(longFormat, aggregationType)
  maxRetention = struct.pack(longFormat, maxRetention)
  xFilesFactor = struct.pack(floatFormat, xFilesFactor)
  archiveCount = struct.pack(longFormat, archiveCount)

  packedMetadata = aggregationType + maxRetention + xFilesFactor + archiveCount

  fh.seek(0)
  fh.write(packedMetadata)


def validateArchiveList(archiveList):
  """ Validates an archiveList.
  An ArchiveList must:
  1. Have at least one archive config. Example: (60, 86400)
  2. No archive may be a duplicate of another.
  3. Higher precision archives' precision must evenly divide all lower
     precision archives' precision.
  4. Lower precision archives must cover larger time intervals than higher
     precision archives.
  5. Each archive must have at least enough points to consolidate to the next
     archive

  Returns True or False
  """

  if not archiveList:
    raise InvalidConfiguration("You must specify at least one archive configuration!")

  archiveList.sort(key=lambda a: a[0])  # Sort by precision (secondsPerPoint)

  for i, archive in enumerate(archiveList):
    if i == len(archiveList) - 1:
      break

    nextArchive = archiveList[i + 1]
    if not archive[0] < nextArchive[0]:
      raise InvalidConfiguration(
        "A Whisper database may not be configured having "
        "two archives with the same precision (archive%d: %s, archive%d: %s)" %
        (i, archive, i + 1, nextArchive))

    if nextArchive[0] % archive[0] != 0:
      raise InvalidConfiguration(
        "Higher precision archives' precision "
        "must evenly divide all lower precision archives' precision "
        "(archive%d: %s, archive%d: %s)" %
        (i, archive[0], i + 1, nextArchive[0]))

    retention = archive[0] * archive[1]
    nextRetention = nextArchive[0] * nextArchive[1]

    if not nextRetention > retention:
      raise InvalidConfiguration(
        "Lower precision archives must cover "
        "larger time intervals than higher precision archives "
        "(archive%d: %s seconds, archive%d: %s seconds)" %
        (i, retention, i + 1, nextRetention))

    archivePoints = archive[1]
    pointsPerConsolidation = nextArchive[0] // archive[0]
    if not archivePoints >= pointsPerConsolidation:
      raise InvalidConfiguration(
        "Each archive must have at least enough points "
        "to consolidate to the next archive (archive%d consolidates %d of "
        "archive%d's points but it has only %d total points)" %
        (i + 1, pointsPerConsolidation, i, archivePoints))


def create(path, archiveList, xFilesFactor=None, aggregationMethod=None,
           sparse=False, useFallocate=False):
  """create(path,archiveList,xFilesFactor=0.5,aggregationMethod='average')

  path               is a string
  archiveList        is a list of archives, each of which is of the form
                     (secondsPerPoint, numberOfPoints)
  xFilesFactor       specifies the fraction of data points in a propagation interval
                     that must have known values for a propagation to occur
  aggregationMethod  specifies the function to use when propagating data (see
                     ``whisper.aggregationMethods``)
  """
  # Set default params
  if xFilesFactor is None:
    xFilesFactor = 0.5
  if aggregationMethod is None:
    aggregationMethod = 'average'

  # Validate archive configurations...
  validateArchiveList(archiveList)

  # Looks good, now we create the file and write the header
  if os.path.exists(path):
    raise InvalidConfiguration("File %s already exists!" % path)

  with open(path, 'wb', BUFFERING) as fh:
    try:
      if LOCK:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
      if CAN_FADVISE and FADVISE_RANDOM:
        posix_fadvise(fh.fileno(), 0, 0, POSIX_FADV_RANDOM)

      oldest = max([secondsPerPoint * points for secondsPerPoint, points in archiveList])

      __writeHeaderMetadata(fh, aggregationMethod, oldest, xFilesFactor,
                            len(archiveList))

      headerSize = metadataSize + (archiveInfoSize * len(archiveList))
      archiveOffsetPointer = headerSize

      for secondsPerPoint, points in archiveList:
        archiveInfo = struct.pack(archiveInfoFormat, archiveOffsetPointer, secondsPerPoint, points)
        fh.write(archiveInfo)
        archiveOffsetPointer += (points * pointSize)

      # If configured to use fallocate and capable of fallocate use that, else
      # attempt sparse if configure or zero pre-allocate if sparse isn't configured.
      if CAN_FALLOCATE and useFallocate:
        remaining = archiveOffsetPointer - headerSize
        fallocate(fh, headerSize, remaining)
      elif sparse:
        fh.seek(archiveOffsetPointer - 1)
        fh.write(b'\x00')
      else:
        remaining = archiveOffsetPointer - headerSize
        chunksize = 16384
        zeroes = b'\x00' * chunksize
        while remaining > chunksize:
          fh.write(zeroes)
          remaining -= chunksize
        fh.write(zeroes[:remaining])

      if AUTOFLUSH:
        fh.flush()
        os.fsync(fh.fileno())
      # Explicitly close the file to catch IOError on close()
      fh.close()
    except IOError:
      # if we got an IOError above, the file is either empty or half created.
      # Better off deleting it to avoid surprises later
      os.unlink(fh.name)
      raise


def aggregate(aggregationMethod, knownValues, neighborValues=None):
  if aggregationMethod == 'average':
    return float(sum(knownValues)) / float(len(knownValues))
  elif aggregationMethod == 'sum':
    return float(sum(knownValues))
  elif aggregationMethod == 'last':
    return knownValues[-1]
  elif aggregationMethod == 'max':
    return max(knownValues)
  elif aggregationMethod == 'min':
    return min(knownValues)
  elif aggregationMethod == 'avg_zero':
    if not neighborValues:
      raise InvalidAggregationMethod("Using avg_zero without neighborValues")
    values = [x or 0 for x in neighborValues]
    return float(sum(values)) / float(len(values))
  elif aggregationMethod == 'absmax':
    return max(knownValues, key=abs)
  elif aggregationMethod == 'absmin':
    return min(knownValues, key=abs)
  else:
    raise InvalidAggregationMethod(
      "Unrecognized aggregation method %s" % aggregationMethod)


def __propagate(fh, header, timestamp, higher, lower):
  aggregationMethod = header['aggregationMethod']
  xff = header['xFilesFactor']

  lowerIntervalStart = timestamp - (timestamp % lower['secondsPerPoint'])

  fh.seek(higher['offset'])
  packedPoint = fh.read(pointSize)
  try:
    (higherBaseInterval, higherBaseValue) = struct.unpack(pointFormat, packedPoint)
  except struct.error:
    raise CorruptWhisperFile("Unable to read base datapoint", fh.name)

  if higherBaseInterval == 0:
    higherFirstOffset = higher['offset']
  else:
    timeDistance = lowerIntervalStart - higherBaseInterval
    pointDistance = timeDistance // higher['secondsPerPoint']
    byteDistance = pointDistance * pointSize
    higherFirstOffset = higher['offset'] + (byteDistance % higher['size'])

  higherPoints = lower['secondsPerPoint'] // higher['secondsPerPoint']
  higherSize = higherPoints * pointSize
  relativeFirstOffset = higherFirstOffset - higher['offset']
  relativeLastOffset = (relativeFirstOffset + higherSize) % higher['size']
  higherLastOffset = relativeLastOffset + higher['offset']
  fh.seek(higherFirstOffset)

  if higherFirstOffset < higherLastOffset:  # We don't wrap the archive
    seriesString = fh.read(higherLastOffset - higherFirstOffset)
  else:  # We do wrap the archive
    higherEnd = higher['offset'] + higher['size']
    seriesString = fh.read(higherEnd - higherFirstOffset)
    fh.seek(higher['offset'])
    seriesString += fh.read(higherLastOffset - higher['offset'])

  # Now we unpack the series data we just read
  byteOrder, pointTypes = pointFormat[0], pointFormat[1:]
  points = len(seriesString) // pointSize
  seriesFormat = byteOrder + (pointTypes * points)
  try:
    unpackedSeries = struct.unpack(seriesFormat, seriesString)
  except struct.error:
    raise CorruptWhisperFile("Unable to read datapoints", fh.name)

  # And finally we construct a list of values
  neighborValues = [None] * points
  currentInterval = lowerIntervalStart
  step = higher['secondsPerPoint']

  for i in xrange(0, len(unpackedSeries), 2):
    pointTime = unpackedSeries[i]
    if pointTime == currentInterval:
      neighborValues[i // 2] = unpackedSeries[i + 1]
    currentInterval += step

  # Propagate aggregateValue to propagate from neighborValues if we have enough known points
  knownValues = [v for v in neighborValues if v is not None]
  if not knownValues:
    return False

  knownPercent = float(len(knownValues)) / float(len(neighborValues))
  if knownPercent >= xff:  # We have enough data to propagate a value!
    aggregateValue = aggregate(aggregationMethod, knownValues, neighborValues)
    myPackedPoint = struct.pack(pointFormat, lowerIntervalStart, aggregateValue)
    fh.seek(lower['offset'])
    packedPoint = fh.read(pointSize)
    try:
      (lowerBaseInterval, lowerBaseValue) = struct.unpack(pointFormat, packedPoint)
    except struct.error:
      raise CorruptWhisperFile("Unable to read base datapoint", fh.name)

    if lowerBaseInterval == 0:  # First propagated update to this lower archive
      fh.seek(lower['offset'])
      fh.write(myPackedPoint)
    else:  # Not our first propagated update to this lower archive
      timeDistance = lowerIntervalStart - lowerBaseInterval
      pointDistance = timeDistance // lower['secondsPerPoint']
      byteDistance = pointDistance * pointSize
      lowerOffset = lower['offset'] + (byteDistance % lower['size'])
      fh.seek(lowerOffset)
      fh.write(myPackedPoint)

    return True

  else:
    return False


def update(path, value, timestamp=None, now=None, revision=None, source=None,
           source_id=None, **kwargs):
  """
  update(path, value, timestamp=None)

  path is a string
  value is a float
  timestamp is either an int or float
  """
  if revision is not None or source is not None or source_id is not None or \
          kwargs.get('provenance') is not None or kwargs.get('source_record') is not None:
    return record_observations(
      path, timestamp=timestamp, value=value, revision=revision, source=source,
      source_id=source_id, now=now, **kwargs)
  value = float(value)
  with path_transaction(path):
    with open(path, 'r+b', BUFFERING) as fh:
      if CAN_FADVISE and FADVISE_RANDOM:
        posix_fadvise(fh.fileno(), 0, 0, POSIX_FADV_RANDOM)
      return file_update(fh, value, timestamp, now)


def file_update(fh, value, timestamp, now=None):
  if LOCK:
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

  header = __readHeader(fh)
  if now is None:
    now = int(time.time())
  if timestamp is None:
    timestamp = now

  timestamp = int(timestamp)
  diff = now - timestamp
  if not ((diff < header['maxRetention']) and diff >= 0):
    raise TimestampNotCovered(
      "Timestamp not covered by any archives in this database.")

  # Find the highest-precision archive that covers timestamp
  for i, archive in enumerate(header['archives']):
    if archive['retention'] < diff:
      continue
    # We'll pass on the update to these lower precision archives later
    lowerArchives = header['archives'][i + 1:]
    break

  # First we update the highest-precision archive
  myInterval = timestamp - (timestamp % archive['secondsPerPoint'])
  myPackedPoint = struct.pack(pointFormat, myInterval, value)
  fh.seek(archive['offset'])
  packedPoint = fh.read(pointSize)
  try:
    (baseInterval, baseValue) = struct.unpack(pointFormat, packedPoint)
  except struct.error:
    raise CorruptWhisperFile("Unable to read base datapoint", fh.name)

  if baseInterval == 0:  # This file's first update
    fh.seek(archive['offset'])
    fh.write(myPackedPoint)
    baseInterval = myInterval
  else:  # Not our first update
    timeDistance = myInterval - baseInterval
    pointDistance = timeDistance // archive['secondsPerPoint']
    byteDistance = pointDistance * pointSize
    myOffset = archive['offset'] + (byteDistance % archive['size'])
    fh.seek(myOffset)
    fh.write(myPackedPoint)

  # Now we propagate the update to lower-precision archives
  higher = archive
  for lower in lowerArchives:
    if not __propagate(fh, header, myInterval, higher, lower):
      break
    higher = lower

  if AUTOFLUSH:
    fh.flush()
    os.fsync(fh.fileno())


def update_many(path, points, now=None, **kwargs):
  """update_many(path,points)

path is a string
points is a list of (timestamp,value) points
"""
  if not points:
    return

  revisioned_records = []
  ordinary_points = []
  revisioned = bool(kwargs.get('revision') is not None or kwargs.get('source_record') is not None)
  for point in points:
    if isinstance(point, dict):
      revisioned = True
      revisioned_records.append(point)
      timestamp = point.get('timestamp', point.get('ts', point.get('time')))
      value = point.get('value', point.get('v'))
      ordinary_points.append((int(timestamp), float(value)))
    elif isinstance(point, (list, tuple)) and len(point) > 2:
      revisioned = True
      record = {'timestamp': point[0], 'value': point[1]}
      if len(point) >= 3:
        if isinstance(point[2], str):
          record['source'] = point[2]
        else:
          record['revision'] = point[2]
      if len(point) >= 4 and isinstance(point[3], str):
        record['source'] = point[3]
      if len(point) >= 5 and isinstance(point[4], dict):
        record.update(point[4])
      revisioned_records.append(record)
      ordinary_points.append((int(point[0]), float(point[1])))
    else:
      timestamp, value = point[0], point[1]
      ordinary_points.append((int(timestamp), float(value)))

  if revisioned:
    source_record = (kwargs.get('source_record') or kwargs.get('sources') or
                     kwargs.get('provenance') or kwargs.get('record'))
    return record_observations(
      path, revisioned_records, now=now,
      source_record=source_record,
      provenance_path=kwargs.get('provenance_path'))

  points = [(int(t), float(v)) for (t, v) in ordinary_points]
  points.sort(key=lambda p: p[0], reverse=True)  # Order points by timestamp, newest first
  with path_transaction(path):
    with open(path, 'r+b', BUFFERING) as fh:
      if CAN_FADVISE and FADVISE_RANDOM:
        posix_fadvise(fh.fileno(), 0, 0, POSIX_FADV_RANDOM)
      return file_update_many(fh, points, now)


def file_update_many(fh, points, now=None):
  if LOCK:
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

  header = __readHeader(fh)
  if now is None:
    now = int(time.time())
  archives = iter(header['archives'])
  currentArchive = next(archives)
  currentPoints = []

  for point in points:
    age = now - point[0]

    while currentArchive['retention'] < age:  # We can't fit any more points in this archive
      if currentPoints:  # Commit all the points we've found that it can fit
        currentPoints.reverse()  # Put points in chronological order
        __archive_update_many(fh, header, currentArchive, currentPoints)
        currentPoints = []
      try:
        currentArchive = next(archives)
      except StopIteration:
        currentArchive = None
        break

    if not currentArchive:
      break  # Drop remaining points that don't fit in the database

    currentPoints.append(point)

  # Don't forget to commit after we've checked all the archives
  if currentArchive and currentPoints:
    currentPoints.reverse()
    __archive_update_many(fh, header, currentArchive, currentPoints)

  if AUTOFLUSH:
    fh.flush()
    os.fsync(fh.fileno())


def __archive_update_many(fh, header, archive, points):
  step = archive['secondsPerPoint']
  alignedPoints = [(timestamp - (timestamp % step), value)
                   for (timestamp, value) in points]
  # Create a packed string for each contiguous sequence of points
  packedStrings = []
  previousInterval = None
  currentString = b""
  lenAlignedPoints = len(alignedPoints)
  for i in xrange(0, lenAlignedPoints):
    # Take last point in run of points with duplicate intervals
    if i + 1 < lenAlignedPoints and alignedPoints[i][0] == alignedPoints[i + 1][0]:
      continue
    (interval, value) = alignedPoints[i]
    if (not previousInterval) or (interval == previousInterval + step):
      currentString += struct.pack(pointFormat, interval, value)
      previousInterval = interval
    else:
      numberOfPoints = len(currentString) // pointSize
      startInterval = previousInterval - (step * (numberOfPoints - 1))
      packedStrings.append((startInterval, currentString))
      currentString = struct.pack(pointFormat, interval, value)
      previousInterval = interval
  if currentString:
    numberOfPoints = len(currentString) // pointSize
    startInterval = previousInterval - (step * (numberOfPoints - 1))
    packedStrings.append((startInterval, currentString))

  # Read base point and determine where our writes will start
  fh.seek(archive['offset'])
  packedBasePoint = fh.read(pointSize)
  try:
    (baseInterval, baseValue) = struct.unpack(pointFormat, packedBasePoint)
  except struct.error:
    raise CorruptWhisperFile("Unable to read base datapoint", fh.name)

  if baseInterval == 0:  # This file's first update
    # Use our first string as the base, so we start at the start
    baseInterval = packedStrings[0][0]

  # Write all of our packed strings in locations determined by the baseInterval
  for (interval, packedString) in packedStrings:
    timeDistance = interval - baseInterval
    pointDistance = timeDistance // step
    byteDistance = pointDistance * pointSize
    myOffset = archive['offset'] + (byteDistance % archive['size'])
    fh.seek(myOffset)
    archiveEnd = archive['offset'] + archive['size']
    bytesBeyond = (myOffset + len(packedString)) - archiveEnd

    if bytesBeyond > 0:
      fh.write(packedString[:-bytesBeyond])
      assert fh.tell() == archiveEnd, (
        "archiveEnd=%d fh.tell=%d bytesBeyond=%d len(packedString)=%d" %
        (archiveEnd, fh.tell(), bytesBeyond, len(packedString))
      )
      fh.seek(archive['offset'])
      # Safe because it can't exceed the archive (retention checking logic above)
      fh.write(packedString[-bytesBeyond:])
    else:
      fh.write(packedString)

  # Now we propagate the updates to lower-precision archives
  higher = archive
  lowerArchives = [arc for arc in header['archives']
                   if arc['secondsPerPoint'] > archive['secondsPerPoint']]

  for lower in lowerArchives:
    def fit(i):
      return i - (i % lower['secondsPerPoint'])
    lowerIntervals = [fit(p[0]) for p in alignedPoints]
    uniqueLowerIntervals = set(lowerIntervals)
    propagateFurther = False
    for interval in uniqueLowerIntervals:
      if __propagate(fh, header, interval, higher, lower):
        propagateFurther = True

    if not propagateFurther:
      break
    higher = lower


def info(path):
  """
  info(path)

  path is a string
  """
  try:
    with open(path, 'rb') as fh:
      return __readHeader(fh)
  except (IOError, OSError):
    pass
  return None


def fetch(path, fromTime, untilTime=None, now=None, archiveToSelect=None,
          version=None, result_id=None, correction_id=None, **kwargs):
  """fetch(path,fromTime,untilTime=None,archiveToSelect=None)

path is a string
fromTime is an epoch time
untilTime is also an epoch time, but defaults to now.
archiveToSelect is the requested granularity, but defaults to None.

Returns a tuple of (timeInfo, valueList)
where timeInfo is itself a tuple of (fromTime, untilTime, step)

Returns None if no data can be returned
"""
  result_id = result_id or kwargs.get('result_id')
  version = version if version is not None else kwargs.get('version')
  correction_id = correction_id or kwargs.get('correction_id')
  if result_id is not None or version is not None or correction_id is not None:
    result = get_result(path, result_id=result_id, version=version,
                        correction_id=correction_id,
                        provenance_path=kwargs.get('provenance_path'))
    if result is None:
      return None
    return result.fetch(archiveToSelect)

  with path_transaction(path):
    with open(path, 'rb') as fh:
      return file_fetch(fh, fromTime, untilTime, now, archiveToSelect)


def file_fetch(fh, fromTime, untilTime, now=None, archiveToSelect=None):
  header = __readHeader(fh)
  if now is None:
    now = int(time.time())
  if untilTime is None:
    untilTime = now
  fromTime = int(fromTime)
  untilTime = int(untilTime)

  # Here we try and be flexible and return as much data as we can.
  # If the range of data is from too far in the past or fully in the future, we
  # return nothing
  if fromTime > untilTime:
    raise InvalidTimeInterval(
        "Invalid time interval: from time '%s' is after until time '%s'" %
        (fromTime, untilTime))

  oldestTime = now - header['maxRetention']
  # Range is in the future
  if fromTime > now:
    return None
  # Range is beyond retention
  if untilTime < oldestTime:
    return None
  # Range requested is partially beyond retention, adjust
  if fromTime < oldestTime:
    fromTime = oldestTime
  # Range is partially in the future, adjust
  if untilTime > now:
    untilTime = now

  diff = now - fromTime

  # Parse granularity if requested
  if archiveToSelect:
    retentionStr = str(archiveToSelect) + ":1"
    archiveToSelect = parseRetentionDef(retentionStr)[0]

  for archive in header['archives']:
    if archiveToSelect:
      if archive['secondsPerPoint'] == archiveToSelect:
        break
      archive = None
    else:
      if archive['retention'] >= diff:
        break

  if archiveToSelect and not archive:
    raise ValueError("Invalid granularity: %s" % (archiveToSelect))

  return __archive_fetch(fh, archive, fromTime, untilTime)


def __archive_fetch(fh, archive, fromTime, untilTime):
  """
Fetch data from a single archive. Note that checks for validity of the time
period requested happen above this level so it's possible to wrap around the
archive on a read and request data older than the archive's retention
"""
  step = archive['secondsPerPoint']

  fromInterval = int(fromTime - (fromTime % step)) + step

  untilInterval = int(untilTime - (untilTime % step)) + step

  if fromInterval == untilInterval:
    # Zero-length time range: always include the next point
    untilInterval += step

  fh.seek(archive['offset'])
  packedPoint = fh.read(pointSize)
  try:
    (baseInterval, baseValue) = struct.unpack(pointFormat, packedPoint)
  except struct.error:
    raise CorruptWhisperFile("Unable to read base datapoint", fh.name)

  if baseInterval == 0:
    points = (untilInterval - fromInterval) // step
    timeInfo = (fromInterval, untilInterval, step)
    valueList = [None] * points
    return (timeInfo, valueList)

  # Determine fromOffset
  timeDistance = fromInterval - baseInterval
  pointDistance = timeDistance // step
  byteDistance = pointDistance * pointSize
  fromOffset = archive['offset'] + (byteDistance % archive['size'])

  # Determine untilOffset
  timeDistance = untilInterval - baseInterval
  pointDistance = timeDistance // step
  byteDistance = pointDistance * pointSize
  untilOffset = archive['offset'] + (byteDistance % archive['size'])

  # Read all the points in the interval
  fh.seek(fromOffset)
  if fromOffset < untilOffset:  # If we don't wrap around the archive
    seriesString = fh.read(untilOffset - fromOffset)
  else:  # We do wrap around the archive, so we need two reads
    archiveEnd = archive['offset'] + archive['size']
    seriesString = fh.read(archiveEnd - fromOffset)
    fh.seek(archive['offset'])
    seriesString += fh.read(untilOffset - archive['offset'])

  # Now we unpack the series data we just read (anything faster than unpack?)
  byteOrder, pointTypes = pointFormat[0], pointFormat[1:]
  points = len(seriesString) // pointSize
  seriesFormat = byteOrder + (pointTypes * points)
  try:
    unpackedSeries = struct.unpack(seriesFormat, seriesString)
  except struct.error:
    raise CorruptWhisperFile("Unable to read datapoints", fh.name)

  # And finally we construct a list of values (optimize this!)
  valueList = [None] * points  # Pre-allocate entire list for speed
  currentInterval = fromInterval

  for i in xrange(0, len(unpackedSeries), 2):
    pointTime = unpackedSeries[i]
    if pointTime == currentInterval:
      pointValue = unpackedSeries[i + 1]
      valueList[i // 2] = pointValue  # In-place reassignment is faster than append()
    currentInterval += step

  timeInfo = (fromInterval, untilInterval, step)
  return (timeInfo, valueList)


def merge(path_from, path_to, time_from=None, time_to=None, now=None):
  """ Merges the data from one whisper file into another. Each file must have
  the same archive configuration. time_from and time_to can optionally be
  specified for the merge.
"""
  # Python 2.7 will allow the following commented line
  # with open(path_from, 'rb') as fh_from, open(path_to, 'rb+') as fh_to:
  # But with Python 2.6 we need to use this (I prefer not to introduce
  # contextlib.nested just for this):
  with open(path_from, 'rb') as fh_from:
    with open(path_to, 'rb+') as fh_to:
      return file_merge(fh_from, fh_to, time_from, time_to, now)


def file_merge(fh_from, fh_to, time_from=None, time_to=None, now=None):
  headerFrom = __readHeader(fh_from)
  headerTo = __readHeader(fh_to)
  if headerFrom['archives'] != headerTo['archives']:
    raise NotImplementedError(
      "%s and %s archive configurations are unalike. "
      "Resize the input before merging" % (fh_from.name, fh_to.name))

  if now is None:
    now = int(time.time())

  if (time_to is not None):
    untilTime = time_to
  else:
    untilTime = now

  if (time_from is not None):
    fromTime = time_from
  else:
    fromTime = 0

  # Sanity check: do not mix the from/to values.
  if untilTime < fromTime:
    raise ValueError("time_to must be >= time_from")

  archives = headerFrom['archives']
  archives.sort(key=operator.itemgetter('retention'))

  for archive in archives:
    archiveFrom = fromTime
    archiveTo = untilTime
    if archiveFrom < now - archive['retention']:
      archiveFrom = now - archive['retention']
    # if untilTime is too old, skip this archive
    if archiveTo < now - archive['retention']:
      continue
    (timeInfo, values) = __archive_fetch(fh_from, archive, archiveFrom, archiveTo)
    (start, end, archive_step) = timeInfo
    pointsToWrite = list(ifilter(
      lambda points: points[1] is not None,
      izip(xrange(start, end, archive_step), values)))
    # skip if there are no points to write
    if len(pointsToWrite) == 0:
      continue
    __archive_update_many(fh_to, headerTo, archive, pointsToWrite)


def diff(path_from, path_to, ignore_empty=False, until_time=None, now=None):
  """ Compare two whisper databases. Each file must have the same archive configuration """
  with open(path_from, 'rb') as fh_from:
    with open(path_to, 'rb') as fh_to:
      return file_diff(fh_from, fh_to, ignore_empty, until_time, now)


def file_diff(fh_from, fh_to, ignore_empty=False, until_time=None, now=None):
  headerFrom = __readHeader(fh_from)
  headerTo = __readHeader(fh_to)

  if headerFrom['archives'] != headerTo['archives']:
    # TODO: Add specific whisper-resize commands to right size things
    raise NotImplementedError(
        "%s and %s archive configurations are unalike. "
        "Resize the input before diffing" % (fh_from.name, fh_to.name))

  archives = headerFrom['archives']
  archives.sort(key=operator.itemgetter('retention'))

  archive_diffs = []

  if now is None:
    now = int(time.time())
  if until_time:
    untilTime = until_time
  else:
    untilTime = now

  for archive_number, archive in enumerate(archives):
    diffs = []
    startTime = now - archive['retention']
    (fromTimeInfo, fromValues) = \
        __archive_fetch(fh_from, archive, startTime, untilTime)
    (toTimeInfo, toValues) = __archive_fetch(fh_to, archive, startTime, untilTime)
    (start, end, archive_step) =  \
        (min(fromTimeInfo[0], toTimeInfo[0]),
         max(fromTimeInfo[1], toTimeInfo[1]),
         min(fromTimeInfo[2], toTimeInfo[2]))

    points = map(lambda s: (s * archive_step + start, fromValues[s], toValues[s]),
                 xrange(0, (end - start) // archive_step))
    if ignore_empty:
      points = [p for p in points if p[1] is not None and p[2] is not None]
    else:
      points = [p for p in points if p[1] is not None or p[2] is not None]

    diffs = [p for p in points if p[1] != p[2]]

    archive_diffs.append((archive_number, diffs, points.__len__()))
    untilTime = min(startTime, untilTime)
  return archive_diffs


LINEAGE_FORMAT = 'whisper-revisioned-archive-1'
_DIRECT_KINDS = ('direct', 'manual', 'hourly', 'field', 'onsite', 'on-site',
                 'site', 'supplement', 'supplemental', 'backfill', 'backfilled')


class _AttrDict(dict):
  def __getattr__(self, name):
    try:
      return self[name]
    except KeyError:
      raise AttributeError(name)

  def __setattr__(self, name, value):
    self[name] = value


class _IntKeyDict(dict):
  def __getitem__(self, key):
    try:
      return dict.__getitem__(self, int(key))
    except (TypeError, ValueError, KeyError):
      return dict.__getitem__(self, key)

  def get(self, key, default=None):
    try:
      return self[key]
    except KeyError:
      return default


class LevelRef(int):
  """Integer level reference that also compares equal to its step size."""

  def __new__(cls, index, step=None, detail=None):
    obj = int.__new__(cls, index)
    obj.index = index
    obj.level = index
    obj.archive = index
    obj.secondsPerPoint = step
    obj.step = step
    obj.precision = step
    obj.detail = detail or {}
    return obj

  def __eq__(self, other):
    if isinstance(other, dict):
      return self.detail == other or self.detail.get('interval') == other.get('interval')
    if other in (self.index, self.level, self.archive,
                self.secondsPerPoint, self.step, self.precision):
      return True
    if isinstance(other, str):
      text = other.strip().lower()
      if text in (str(self.index), str(self.step)):
        return True
      try:
        return _step_from_ref(other) == self.step
      except Exception:
        return False
    return int.__eq__(self, other)

  def __hash__(self):
    return hash(int(self))


def _step_from_ref(value):
  if isinstance(value, (int, float)):
    return int(value)
  text = str(value).strip()
  if text.isdigit():
    return int(text)
  return parseRetentionDef(text + ':1')[0]


def lineage_path(path, provenance_path=None):
  if provenance_path:
    return os.fspath(provenance_path)
  return os.fspath(path) + '.provenance.json'


def _candidate_lineage_paths(path, provenance_path=None):
  if provenance_path:
    return [os.fspath(provenance_path)]
  base = os.fspath(path)
  return [base + suffix for suffix in
          ('.provenance.json', '.provenance', '.lineage.json',
           '.sources.json', '.source.json', '.revision.json')]


def _empty_lineage():
  return {
    'format': LINEAGE_FORMAT,
    'version': 0,
    'latest_result_id': None,
    'observations': {},
    'timestamp_index': {},
    'source_index': {},
    'corrections': {},
    'retractions': [],
    'source_records': [],
    'results': {},
    'result_order': [],
    'versions': [],
    'provenance': {
      'records': {},
      'cell_index': {},
      'cells': {},
      'batches': {},
    },
  }


def _load_lineage(path, provenance_path=None):
  for candidate in _candidate_lineage_paths(path, provenance_path):
    if os.path.exists(candidate):
      try:
        with open(candidate, 'r') as fh:
          data = json.load(fh)
      except (ValueError, OSError):
        if provenance_path:
          raise
        continue
      base = _empty_lineage()
      base.update(data if isinstance(data, dict) else {})
      base.setdefault('provenance', {})
      for key in ('records', 'cell_index', 'cells', 'batches'):
        base['provenance'].setdefault(key, {})
      for key, value in _empty_lineage().items():
        if key == 'provenance':
          continue
        base.setdefault(key, value if not isinstance(value, (dict, list)) else type(value)())
      base['_path'] = candidate
      return base
  ledger = _empty_lineage()
  ledger['_path'] = lineage_path(path, provenance_path)
  return ledger


def _save_lineage(ledger):
  target = ledger.get('_path')
  if not target:
    raise WhisperException('Lineage path is not set')
  target = os.fspath(target)
  directory = os.path.dirname(os.path.abspath(target)) or '.'
  payload = json.dumps(ledger, sort_keys=True, indent=2, default=str)

  def write_atomic(destination):
    fd, temporary = tempfile.mkstemp(prefix='.whisper-lineage-', dir=directory)
    try:
      with os.fdopen(fd, 'w') as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
      os.replace(temporary, destination)
    except Exception:
      try:
        os.unlink(temporary)
      except OSError:
        pass
      raise

  try:
    write_atomic(target)
    try:
      dir_fd = os.open(directory, os.O_RDONLY)
      try:
        os.fsync(dir_fd)
      finally:
        os.close(dir_fd)
    except OSError:
      pass

    if target.endswith('.provenance.json'):
      whisper_path = target[:-len('.provenance.json')]
      for suffix in ('.provenance', '.lineage.json', '.sources.json', '.source.json'):
        try:
          write_atomic(whisper_path + suffix)
        except OSError:
          pass
  except Exception:
    raise


def _pick(mapping, names, default=None):
  for name in names:
    if name in mapping and mapping[name] is not None:
      return mapping[name]
  return default


def _as_int(value, default=None):
  if value is None:
    return default
  try:
    return int(float(value))
  except (TypeError, ValueError):
    return default


def _revision_key(revision):
  if revision is None:
    return (-1, 0)
  try:
    return (0, int(revision))
  except (TypeError, ValueError):
    try:
      return (1, float(revision))
    except (TypeError, ValueError):
      return (2, str(revision))


def _stable_id(prefix, *parts):
  raw = '|'.join(str(part) for part in parts)
  return prefix + '-' + str(abs(hash(raw)) % (10 ** 16))


def _is_direct_record(data, kind=None, source=None):
  if data.get('direct') is True or data.get('is_direct') is True:
    return True
  text = ' '.join(str(x) for x in (kind, source, data.get('type'), data.get('kind')) if x)
  text = text.lower().replace('-', '').replace('_', '')
  return any(kind.replace('-', '').replace('_', '') in text for kind in _DIRECT_KINDS)


def _normalize_observation(item, default_now=None):
  if isinstance(item, (list, tuple)):
    timestamp = item[0] if len(item) > 0 else None
    value = item[1] if len(item) > 1 else None
    revision = None
    source = None
    metadata = {}
    for position in item[2:]:
      if isinstance(position, dict):
        metadata = position
      elif isinstance(position, str):
        source = position
      elif revision is None:
        revision = position
    data = dict(metadata)
    data.update({'timestamp': timestamp, 'value': value})
    if revision is not None:
      data.setdefault('revision', revision)
    if source is not None:
      data.setdefault('source', source)
  elif isinstance(item, dict):
    data = dict(item)
  else:
    raise ValueError('Observation must be a dict, tuple or list')

  timestamp = _as_int(_pick(data, ('timestamp', 'ts', 'time', 'sampled_at', 'at', 't')))
  if timestamp is None:
    raise ValueError('Observation is missing a timestamp')
  value = _pick(data, ('value', 'v', 'reading', 'measurement'))
  if value is None:
    raise ValueError('Observation is missing a value')
  value = float(value)
  revision = _pick(data, ('revision', 'rev', 'revision_number', 'revisionNo', 'version'))
  source = _pick(data, ('source', 'source_id', 'sourceId', 'origin', 'collector', 'sensor'))
  source_id = _pick(data, ('source_id', 'sourceId', 'observation_id', 'id', 'uuid',
                           'voucher_id', 'credential_id', 'certificate_id'), source)
  voucher_id = _pick(data, ('voucher_id', 'voucherId', 'credential_id',
                           'certificate_id', 'receipt_id'), source_id)
  kind = _pick(data, ('kind', 'type', 'observation_type', 'record_type'), 'sample')
  direct = _is_direct_record(data, kind, source)
  archive_ref = _pick(data, ('archive', 'archive_index', 'archiveIndex', 'level',
                             'secondsPerPoint', 'step', 'precision', 'granularity'))
  explicit_id = _pick(data, ('id', 'observation_id', 'obs_id', 'uuid', 'key'))
  observation_id = explicit_id or _stable_id('obs', timestamp, revision, source_id, voucher_id)

  return {
    'id': str(observation_id),
    'observation_id': str(observation_id),
    'timestamp': timestamp,
    'value': value,
    'revision': revision,
    'revision_key': list(_revision_key(revision)),
    'source': source,
    'source_id': str(source_id) if source_id is not None else None,
    'voucher_id': str(voucher_id) if voucher_id is not None else None,
    'credential_id': str(voucher_id) if voucher_id is not None else None,
    'kind': 'direct' if direct else str(kind or 'sample'),
    'direct': direct,
    'archive_ref': archive_ref,
    'interval': _as_int(_pick(data, ('interval', 'aligned_interval', 'cell_time'))),
    'approved': data.get('approved', data.get('is_approved', True)),
    'active': bool(data.get('active', not data.get('retracted', False))),
    'retracted': bool(data.get('retracted', False)),
    'recorded_at': _as_int(data.get('recorded_at'), default_now),
    'metadata': {k: v for k, v in data.items() if k not in (
      'timestamp', 'ts', 'time', 'value', 'v', 'revision', 'rev', 'source', 'source_id',
      'id', 'observation_id', 'voucher_id', 'credential_id', 'kind', 'type', 'direct',
      'archive', 'archive_index', 'secondsPerPoint', 'step', 'precision', 'granularity',
      'approved', 'active', 'retracted')},
  }


def _observation_list(observations=None, **kwargs):
  items = []
  if observations is not None:
    if isinstance(observations, dict):
      if any(key in observations for key in ('timestamp', 'ts', 'value', 'v')):
        items.append(observations)
      else:
        for timestamp, value in observations.items():
          if isinstance(value, dict):
            item = dict(value)
            item.setdefault('timestamp', timestamp)
          else:
            item = {'timestamp': timestamp, 'value': value}
          items.append(item)
    elif isinstance(observations, (list, tuple)):
      if observations and not isinstance(observations[0], (dict, list, tuple)):
        items.append(observations)
      else:
        items.extend(observations)
    else:
      items.append(observations)
  if kwargs.get('timestamp') is not None or kwargs.get('ts') is not None:
    items.append(kwargs)
  return [_normalize_observation(item, kwargs.get('now')) for item in items]


def _add_observation(ledger, observation):
  obs_id = observation['id']
  existing = ledger['observations'].get(obs_id)
  if existing:
    merged = dict(existing)
    merged.update({k: v for k, v in observation.items() if v is not None})
    observation = merged
  ledger['observations'][obs_id] = observation
  timestamp_key = str(observation['timestamp'])
  ids = ledger['timestamp_index'].setdefault(timestamp_key, [])
  if obs_id not in ids:
    ids.append(obs_id)
  for key in ('source_id', 'voucher_id', 'credential_id'):
    value = observation.get(key)
    if value is not None:
      index = ledger['source_index'].setdefault(str(value), [])
      if obs_id not in index:
        index.append(obs_id)
  return observation


def _find_observation(ledger, reference):
  if reference is None:
    return None
  ref = str(reference)
  if ref in ledger['observations']:
    return ledger['observations'][ref]
  ids = ledger['source_index'].get(ref, [])
  if ids:
    return ledger['observations'].get(ids[-1])
  for observation in ledger['observations'].values():
    if ref in (str(observation.get('source_id')), str(observation.get('voucher_id')),
               str(observation.get('credential_id'))):
      return observation
  return None


def _resolve_archive_index(header, archive_ref=None, archive_index=None, step=None):
  archives = header['archives']
  if archive_index is not None:
    index = int(archive_index)
    if 0 <= index < len(archives):
      return index
  if step is not None:
    target_step = _as_int(step)
    for index, archive in enumerate(archives):
      if archive['secondsPerPoint'] == target_step:
        return index
  if archive_ref is not None:
    if isinstance(archive_ref, int) and not isinstance(archive_ref, bool):
      if 0 <= archive_ref < len(archives):
        return archive_ref
      for index, archive in enumerate(archives):
        if archive['secondsPerPoint'] == archive_ref:
          return index
    else:
      try:
        target_step = _step_from_ref(archive_ref)
      except Exception:
        target_step = None
      if target_step is not None:
        for index, archive in enumerate(archives):
          if archive['secondsPerPoint'] == target_step:
            return index
  return None


def _normalize_cell_record(record, header=None):
  if not isinstance(record, dict):
    return None
  archive_ref = _pick(record, ('archive', 'archive_ref', 'granularity', 'precision'))
  archive_index = _pick(record, ('archive_index', 'archiveIndex', 'level', 'index'))
  step = _pick(record, ('secondsPerPoint', 'step', 'step_seconds', 'period'))
  interval = _as_int(_pick(record, ('interval', 'cell', 'cell_interval', 'timestamp',
                                    'time', 'start', 'fromTime', 'from_time')))
  if interval is None:
    return None
  if step is None and header is not None:
    index = _resolve_archive_index(header, archive_ref, archive_index)
    if index is not None:
      step = header['archives'][index]['secondsPerPoint']
  step = _as_int(step, 1)
  interval = interval - (interval % step)
  source_ids = _pick(record, ('source_ids', 'sourceIds', 'sources', 'observations',
                              'observation_ids', 'inputs', 'batch'))
  if source_ids is None:
    source_ids = []
  elif isinstance(source_ids, (str, int)):
    source_ids = [source_ids]
  elif isinstance(source_ids, dict):
    source_ids = source_ids.get('source_ids') or source_ids.get('ids') or []
  source_ids = [str(item) for item in source_ids]
  batch_id = _pick(record, ('batch_id', 'batchId', 'batch', 'publication_id', 'id'))
  kind = _pick(record, ('kind', 'type', 'source_type', 'publication_type'))
  source = _pick(record, ('source', 'origin', 'publisher'))
  direct = _is_direct_record(record, kind, source)
  record_id = _pick(record, ('record_id', 'provenance_id', 'rid')) or _stable_id(
    'prov', step, interval, batch_id, kind, source_ids)
  return {
    'id': str(record_id),
    'record_id': str(record_id),
    'cell_key': '%s:%s' % (step, interval),
    'archive_ref': archive_ref,
    'archive_index': _as_int(archive_index),
    'secondsPerPoint': step,
    'step': step,
    'interval': interval,
    'timestamp': interval,
    'value': record.get('value'),
    'kind': 'direct' if direct else (str(kind) if kind is not None else 'aggregate'),
    'direct': direct,
    'source': source,
    'source_ids': source_ids,
    'batch_id': str(batch_id) if batch_id is not None else None,
    'voucher_id': str(_pick(record, ('voucher_id', 'credential_id', 'certificate_id'), '')) or None,
    'status': record.get('status', 'published'),
    'min_points': _as_int(_pick(record, ('min_points', 'minimum_points', 'required_sources'))),
    'xFilesFactor': _pick(record, ('xFilesFactor', 'xff', 'coverage_threshold')),
  }


def _merge_source_record(ledger, source_record, header=None):
  if source_record is None:
    return ledger
  if isinstance(source_record, str) and os.path.exists(source_record):
    with open(source_record, 'r') as fh:
      source_record = json.load(fh)
  ledger['source_records'].append(source_record)

  observation_keys = ('observations', 'raw_observations', 'original_observations',
                      'original_readings', 'raw_points', 'points', 'samples',
                      'readings', 'data')
  for key in observation_keys:
    observations = source_record.get(key) if isinstance(source_record, dict) else None
    if observations:
      for observation in _observation_list(observations):
        _add_observation(ledger, observation)

  provenance = ledger['provenance']
  provenance_keys = ('provenance', 'sources', 'lineage', 'archive_sources',
                     'archive_source_records', 'source_records', 'lineage_records',
                     'cell_records', 'publication_records', 'cell_sources',
                     'published_cells', 'cells', 'archive_cells',
                     'publications', 'batches')
  raw_records = []
  if isinstance(source_record, list):
    raw_records = source_record
  elif isinstance(source_record, dict):
    for key in provenance_keys:
      value = source_record.get(key)
      if isinstance(value, list):
        raw_records.extend(value)
      elif isinstance(value, dict):
        if key == 'batches':
          for batch_id, batch in value.items():
            ids = batch
            if isinstance(batch, dict):
              ids = _pick(batch, ('source_ids', 'observations', 'ids'), [])
            provenance['batches'][str(batch_id)] = {
              'batch_id': str(batch_id),
              'source_ids': [str(x) for x in (ids or [])],
            }
        elif key == 'cells' and all(isinstance(v, dict) for v in value.values()):
          for cell_key, cell in value.items():
            cell = dict(cell)
            if ':' in str(cell_key):
              step, interval = str(cell_key).split(':', 1)
              cell.setdefault('step', step)
              cell.setdefault('interval', interval)
            raw_records.append(cell)
        elif key not in observation_keys:
          raw_records.append(value)

  for raw in raw_records:
    normalized = _normalize_cell_record(raw, header)
    if not normalized:
      continue
    rid = normalized['record_id']
    provenance['records'][rid] = normalized
    index = provenance['cell_index'].setdefault(normalized['cell_key'], [])
    if rid not in index:
      index.append(rid)
    if normalized['batch_id']:
      batch = provenance['batches'].setdefault(normalized['batch_id'], {
        'batch_id': normalized['batch_id'], 'source_ids': []})
      for source_id in normalized['source_ids']:
        if source_id not in batch['source_ids']:
          batch['source_ids'].append(source_id)
  return ledger


def _read_all_points(fh, header, now):
  points_by_archive = []
  for archive in header['archives']:
    points = {}
    fh.seek(archive['offset'])
    series = fh.read(archive['size'])
    for position in xrange(archive['points']):
      start = position * pointSize
      packed_point = series[start:start + pointSize]
      if len(packed_point) != pointSize:
        continue
      interval, value = struct.unpack(pointFormat, packed_point)
      if interval <= 0 or interval % archive['secondsPerPoint'] != 0:
        continue
      if interval <= now - archive['retention']:
        continue
      if interval > now + archive['secondsPerPoint']:
        continue
      points[interval] = value
    points_by_archive.append(points)
  return points_by_archive


def _slot_state(fh, archive, interval):
  fh.seek(archive['offset'])
  packed = fh.read(pointSize)
  try:
    base_interval, _base_value = struct.unpack(pointFormat, packed)
  except struct.error:
    raise CorruptWhisperFile('Unable to read base datapoint', fh.name)

  if base_interval == 0:
    offset = archive['offset']
    stored_interval = 0
    stored_value = None
  else:
    time_distance = interval - base_interval
    point_distance = time_distance // archive['secondsPerPoint']
    byte_distance = point_distance * pointSize
    offset = archive['offset'] + (byte_distance % archive['size'])
    fh.seek(offset)
    packed = fh.read(pointSize)
    try:
      stored_interval, stored_value = struct.unpack(pointFormat, packed)
    except struct.error:
      raise CorruptWhisperFile('Unable to read datapoint', fh.name)

  if stored_interval == interval:
    state = 'present'
  elif stored_interval == 0:
    state = 'empty'
  elif stored_interval > interval:
    state = 'overwritten'
  else:
    state = 'stale'
  return state, stored_interval, stored_value, offset


def _add_chain(header, affected, archive_index, interval):
  interval = _as_int(interval, 0)
  for index in range(max(0, archive_index), len(header['archives'])):
    step = header['archives'][index]['secondsPerPoint']
    affected[index].add(interval - (interval % step))


def _observation_winners(header, ledger):
  winners = {}
  interval_observations = {}
  for observation in ledger['observations'].values():
    if observation.get('retracted') or not observation.get('active', True):
      continue
    archive_index = _resolve_archive_index(
      header, observation.get('archive_ref'), observation.get('archive_index'))
    if archive_index is None and observation.get('direct'):
      archive_index = _resolve_archive_index(header, step=3600)
    if archive_index is None:
      archive_index = 0
    step = header['archives'][archive_index]['secondsPerPoint']
    interval = observation.get('interval')
    if interval is None:
      interval = observation['timestamp'] - (observation['timestamp'] % step)
    interval = _as_int(interval)
    key = (archive_index, interval)
    revision_key = tuple(observation.get('revision_key') or (-1, 0))
    candidate_sort = (observation['timestamp'], revision_key,
                      observation.get('recorded_at') or 0)
    current = winners.get(key)
    if current is None or candidate_sort > current[0]:
      winners[key] = (candidate_sort, observation)
      interval_observations[key] = observation
  return {key: item[1] for key, item in winners.items()}, interval_observations


def _write_replacement(path, header, points_by_archive):
  directory = os.path.dirname(os.path.abspath(path)) or '.'
  fd, temporary = tempfile.mkstemp(prefix='.whisper-rebuild-', dir=directory)
  os.close(fd)
  try:
    os.unlink(temporary)
    archive_list = [(archive['secondsPerPoint'], archive['points'])
                    for archive in header['archives']]
    create(temporary, archive_list,
           xFilesFactor=header['xFilesFactor'],
           aggregationMethod=header['aggregationMethod'])
    try:
      shutil.copymode(path, temporary)
    except OSError:
      pass

    new_header = info(temporary)
    with open(temporary, 'r+b', BUFFERING) as fh:
      for archive, points in zip(new_header['archives'], points_by_archive):
        if not points:
          continue
        base_interval = min(points)
        step = archive['secondsPerPoint']
        for interval, value in sorted(points.items()):
          if value is None:
            continue
          interval = _as_int(interval) - (_as_int(interval) % step)
          point_distance = (interval - base_interval) // step
          offset = archive['offset'] + ((point_distance * pointSize) % archive['size'])
          fh.seek(offset)
          fh.write(struct.pack(pointFormat, interval, float(value)))
      if AUTOFLUSH:
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)
    try:
      dir_fd = os.open(directory, os.O_RDONLY)
      try:
        os.fsync(dir_fd)
      finally:
        os.close(dir_fd)
    except OSError:
      pass
  except Exception:
    try:
      os.unlink(temporary)
    except OSError:
      pass
    raise


def _provenance_records_for(ledger, step, interval):
  key = '%s:%s' % (step, interval)
  record_ids = ledger['provenance']['cell_index'].get(key, [])
  return key, [ledger['provenance']['records'][rid]
               for rid in record_ids if rid in ledger['provenance']['records']]


def _update_cell_summary(ledger, header, index, interval, **changes):
  archive = header['archives'][index]
  step = archive['secondsPerPoint']
  key = '%s:%s' % (step, interval)
  cell = ledger['provenance']['cells'].setdefault(key, {
    'cell_key': key,
    'archive_index': index,
    'archive': index,
    'level': index,
    'secondsPerPoint': step,
    'step': step,
    'interval': interval,
    'timestamp': interval,
  })
  cell.update(changes)
  return cell


def _reconcile_state(path, ledger, now, pop_cells=None, affected=None,
                     correction_id=None, coverage=None):
  with open(path, 'rb') as fh:
    header = __readHeader(fh)
    current_points = _read_all_points(fh, header, now)
    desired_points = [dict(points) for points in current_points]
    archive_count = len(header['archives'])
    if affected is None:
      affected = [set() for _ in header['archives']]
    else:
      affected = [set(item) for item in affected]
      while len(affected) < archive_count:
        affected.append(set())

    for archive_index, interval in (pop_cells or []):
      if 0 <= archive_index < archive_count:
        desired_points[archive_index].pop(_as_int(interval), None)
        _add_chain(header, affected, archive_index, interval)

    winners, interval_observations = _observation_winners(header, ledger)
    for (archive_index, interval), observation in winners.items():
      desired_points[archive_index][interval] = observation['value']
      _add_chain(header, affected, archive_index, interval)

    recomputed = [set() for _ in header['archives']]
    unresolved_intervals = [set() for _ in header['archives']]
    unresolved = []

    for archive_index, interval in (pop_cells or []):
      interval = _as_int(interval)
      if 0 <= archive_index < archive_count:
        recomputed[archive_index].add(interval)

    for index in range(1, archive_count):
      archive = header['archives'][index]
      higher = header['archives'][index - 1]
      step = archive['secondsPerPoint']
      child_step = higher['secondsPerPoint']
      required_slots = step // child_step

      for interval in sorted(affected[index]):
        interval = interval - (interval % step)
        recomputed[index].add(interval)
        key, records = _provenance_records_for(ledger, step, interval)
        direct_records = [record for record in records
                          if record.get('direct')
                          and record.get('status') not in ('retracted', 'revoked')]
        direct_observations = [
          observation for observation in ledger['observations'].values()
          if observation.get('active', True) and not observation.get('retracted')
          and observation.get('direct')
          and _resolve_archive_index(
            header, observation.get('archive_ref'),
            observation.get('archive_index')) == index
          and (observation.get('timestamp', 0) -
               (observation.get('timestamp', 0) % step)) == interval
        ]

        if direct_observations:
          direct_observation = sorted(
            direct_observations,
            key=lambda obs: (obs['timestamp'], tuple(obs.get('revision_key') or [-1, 0]))
          )[-1]
          value = direct_observation['value']
          desired_points[index][interval] = value
          source_ids = [direct_observation['id']]
          if direct_observation.get('voucher_id'):
            source_ids.append(direct_observation['voucher_id'])
          _update_cell_summary(
            ledger, header, index, interval,
            kind='direct', direct=True, status='published', value=value,
            source_ids=source_ids, voucher_id=direct_observation.get('voucher_id'),
            correction_id=correction_id)
          for record in direct_records:
            record.update({'status': 'published', 'value': value,
                           'correction_id': correction_id})
          continue

        if direct_records:
          direct_record = direct_records[-1]
          direct_value = direct_record.get('value')
          if direct_value is None and direct_record.get('source_ids'):
            for source_id in direct_record['source_ids']:
              source_observation = _find_observation(ledger, source_id)
              if source_observation and not source_observation.get('retracted'):
                direct_value = source_observation.get('value')
                break
          if direct_value is not None:
            value = float(direct_value)
            desired_points[index][interval] = value
            _update_cell_summary(
              ledger, header, index, interval,
              kind='direct', direct=True, status='published', value=value,
              source_ids=direct_record.get('source_ids') or [],
              voucher_id=direct_record.get('voucher_id'),
              batch_id=direct_record.get('batch_id'),
              correction_id=correction_id)
            direct_record.update({'status': 'published', 'value': value,
                                  'correction_id': correction_id})
            continue

        aggregate_record = None
        for record in reversed(records):
          if not record.get('direct') and (record.get('source_ids') or
                                           record.get('kind') in ('aggregate', 'derived')):
            aggregate_record = record
            break

        values = []
        neighbor_values = [None] * required_slots
        observation_ids = []
        source_intervals = []
        missing = []
        evidence_complete = True
        min_points = None
        xff = header['xFilesFactor']
        coverage = coverage or {}
        if coverage.get('min_points') is not None:
          min_points = _as_int(coverage.get('min_points'))
        if coverage.get('xFilesFactor') is not None:
          try:
            xff = float(coverage.get('xFilesFactor'))
          except (TypeError, ValueError):
            pass

        if aggregate_record:
          if aggregate_record.get('min_points') is not None:
            min_points = aggregate_record.get('min_points')
          try:
            if aggregate_record.get('xFilesFactor') is not None:
              xff = float(aggregate_record['xFilesFactor'])
          except (TypeError, ValueError):
            pass
          raw_refs = list(aggregate_record.get('source_ids') or [])
          expanded_refs = []
          for reference in raw_refs:
            batch = ledger['provenance']['batches'].get(str(reference))
            if batch and batch.get('source_ids'):
              expanded_refs.extend(batch['source_ids'])
            else:
              expanded_refs.append(reference)
          for source_id in expanded_refs:
            observation = _find_observation(ledger, source_id)
            if observation is not None:
              if observation.get('retracted') or not observation.get('active', True):
                continue
              values.append(observation['value'])
              observation_ids.append(observation['id'])
              continue

            source_timestamp = _as_int(source_id)
            resolved_interval = None
            if source_timestamp is not None:
              aligned_interval = source_timestamp - (source_timestamp % child_step)
              for candidate in (source_timestamp, aligned_interval):
                if candidate in desired_points[index - 1]:
                  candidate_value = desired_points[index - 1][candidate]
                  if candidate_value is not None:
                    resolved_interval = candidate
                    values.append(candidate_value)
                    source_intervals.append(candidate)
                    slot_index = (candidate - interval) // child_step
                    if 0 <= slot_index < required_slots:
                      neighbor_values[slot_index] = candidate_value
                  break
                if candidate in recomputed[index - 1]:
                  resolved_interval = candidate
                  if candidate in unresolved_intervals[index - 1]:
                    evidence_complete = False
                    missing.append(candidate)
                  break

            if resolved_interval is None:
              evidence_complete = False
              missing.append(source_id)
        else:
          for slot_index in range(required_slots):
            child_interval = interval + slot_index * child_step
            source_intervals.append(child_interval)
            if child_interval in desired_points[index - 1]:
              value = desired_points[index - 1][child_interval]
              if value is not None:
                neighbor_values[slot_index] = value
                values.append(value)
                observation = interval_observations.get((index - 1, child_interval))
                if observation:
                  observation_ids.append(observation['id'])
              continue
            if child_interval in recomputed[index - 1]:
              if child_interval in unresolved_intervals[index - 1]:
                evidence_complete = False
                missing.append(child_interval)
              continue
            state, stored_interval, stored_value, _offset = _slot_state(
              fh, higher, child_interval)
            if state == 'present' and stored_value is not None:
              neighbor_values[slot_index] = stored_value
              values.append(stored_value)
            elif state == 'overwritten':
              evidence_complete = False
              missing.append(child_interval)

        enough = False
        if min_points is not None:
          enough = len(values) >= int(min_points)
        elif required_slots:
          enough = (float(len(values)) / float(required_slots) + 1e-12) >= float(xff)

        if not enough:
          desired_points[index].pop(interval, None)
          if not evidence_complete:
            unresolved_intervals[index].add(interval)
            detail = {
              'archive_index': index,
              'archive': index,
              'level': index,
              'secondsPerPoint': step,
              'step': step,
              'interval': interval,
              'timestamp': interval,
              'reason': 'insufficient_evidence',
              'missing': missing,
              'required_slots': required_slots,
              'available_values': len(values),
              'correction_id': correction_id,
            }
            unresolved.append(_AttrDict(detail))
            _update_cell_summary(
              ledger, header, index, interval,
              kind='aggregate', status='unresolved', value=None,
              missing=missing,
              source_intervals=source_intervals, observation_ids=observation_ids,
              correction_id=correction_id)
            if aggregate_record:
              aggregate_record.update({'status': 'unresolved', 'value': None,
                                       'missing': missing,
                                       'correction_id': correction_id})
          else:
            _update_cell_summary(
              ledger, header, index, interval,
              kind='aggregate', status='insufficient_coverage', value=None,
              source_intervals=source_intervals, observation_ids=observation_ids,
              available_values=len(values), required_slots=required_slots,
              correction_id=correction_id)
            if aggregate_record:
              aggregate_record.update({'status': 'insufficient_coverage', 'value': None,
                                       'correction_id': correction_id})
          if index + 1 < archive_count:
            parent_step = header['archives'][index + 1]['secondsPerPoint']
            affected[index + 1].add(interval - (interval % parent_step))
          continue

        aggregate_value = aggregate(header['aggregationMethod'], values, neighbor_values)
        desired_points[index][interval] = aggregate_value
        record_id = aggregate_record['record_id'] if aggregate_record else \
          'derived:%s:%s' % (step, interval)
        record = aggregate_record or {}
        record.update({
          'id': record_id,
          'record_id': record_id,
          'cell_key': key,
          'archive_index': index,
          'secondsPerPoint': step,
          'step': step,
          'interval': interval,
          'timestamp': interval,
          'kind': 'aggregate',
          'direct': False,
          'status': 'published',
          'value': aggregate_value,
          'source_ids': record.get('source_ids') or source_intervals,
          'source_intervals': source_intervals,
          'observation_ids': observation_ids,
          'batch_id': record.get('batch_id') or ('batch:%s:%s' % (step, interval)),
          'correction_id': correction_id,
        })
        ledger['provenance']['records'][record_id] = record
        index_ids = ledger['provenance']['cell_index'].setdefault(key, [])
        if record_id not in index_ids:
          index_ids.append(record_id)
        _update_cell_summary(
          ledger, header, index, interval,
          kind='aggregate', direct=False, status='published', value=aggregate_value,
          source_ids=record['source_ids'], source_intervals=source_intervals,
          observation_ids=observation_ids, batch_id=record['batch_id'],
          correction_id=correction_id)
        if index + 1 < archive_count:
          parent_step = header['archives'][index + 1]['secondsPerPoint']
          affected[index + 1].add(interval - (interval % parent_step))

  whisper_path = getattr(fh, 'name', None) or path
  _write_replacement(whisper_path, header, desired_points)

  unresolved_levels = []
  unresolved_steps = []
  for item in unresolved:
    if item['archive_index'] not in unresolved_levels:
      unresolved_levels.append(item['archive_index'])
    if item['step'] not in unresolved_steps:
      unresolved_steps.append(item['step'])

  return {
    'header': header,
    'desired_points': desired_points,
    'unresolved': unresolved,
    'unresolved_levels': unresolved_levels,
    'unresolved_archives': unresolved_steps,
    'unresolved_steps': unresolved_steps,
  }


def _snapshot_from_path(path, now, version, result_id, correction_id=None,
                        reconciliation=None):
  with open(path, 'rb') as fh:
    header = __readHeader(fh)
    archives = []
    for index, archive in enumerate(header['archives']):
      time_info, values = __archive_fetch(
        fh, archive, now - archive['retention'], now)
      start, end, step = time_info
      points = []
      for position, value in enumerate(values):
        interval = start + position * step
        if value is not None and interval > 0:
          points.append([interval, value])
      archives.append(_AttrDict({
        'archive': index,
        'index': index,
        'level': index,
        'secondsPerPoint': archive['secondsPerPoint'],
        'step': archive['secondsPerPoint'],
        'points': archive['points'],
        'retention': archive['retention'],
        'timeInfo': [time_info[0], time_info[1], time_info[2]],
        'time_info': [time_info[0], time_info[1], time_info[2]],
        'values': list(values),
        'non_null_points': points,
      }))

  unresolved = (reconciliation or {}).get('unresolved', [])
  unresolved_refs = [LevelRef(item['archive_index'], item['step'], dict(item))
                     for item in unresolved]
  fetches_by_step = _IntKeyDict()
  fetches_by_index = {}
  for entry in archives:
    time_info = tuple(entry.get('timeInfo') or entry.get('time_info') or ())
    values = list(entry.get('values') or [])
    fetches_by_step[entry['step']] = (time_info, values)
    fetches_by_index[entry['index']] = (time_info, values)
  unresolved_levels = (reconciliation or {}).get('unresolved_levels', [])
  unresolved_archives = (reconciliation or {}).get('unresolved_archives', [])
  return _AttrDict({
    'ok': not unresolved,
    'success': True,
    'consistent': True,
    'path': os.fspath(path),
    'version': version,
    'result_id': result_id,
    'correction_id': correction_id,
    'now': now,
    'archives': archives,
    'fetches': fetches_by_step,
    'fetches_by_step': fetches_by_step,
    'fetches_by_index': fetches_by_index,
    'unresolved': unresolved_refs,
    'unsettled': unresolved_refs,
    'incomplete': unresolved_refs,
    'blocked': unresolved_refs,
    'unresolved_details': [dict(item) for item in unresolved],
    'unresolved_levels': unresolved_levels,
    'unsettled_levels': unresolved_levels,
    'incomplete_levels': unresolved_levels,
    'unresolved_archives': unresolved_archives,
    'unresolved_steps': (reconciliation or {}).get('unresolved_steps', []),
    'unsettled_archives': unresolved_archives,
    'report': {
      'unresolved': unresolved_refs,
      'levels': unresolved_levels,
      'archives': unresolved_archives,
      'details': [dict(item) for item in unresolved],
    },
  })


class ArchiveResult(_AttrDict):
  def archive(self, archive=None, level=None, index=None, step=None):
    archives = self.get('archives', [])
    level_reference = level if level is not None else index
    if step is not None:
      archive = step
    if level_reference is not None:
      for entry in archives:
        if entry.get('index') == int(level_reference):
          return entry
    if archive is None:
      return archives[0] if archives else None
    if isinstance(archive, int) and not isinstance(archive, bool):
      for entry in archives:
        if entry.get('step') == archive:
          return entry
      for entry in archives:
        if entry.get('index') == archive:
          return entry
    try:
      target_step = _step_from_ref(archive)
    except Exception:
      target_step = None
    for entry in archives:
      if target_step is not None and entry.get('step') == target_step:
        return entry
    raise ValueError('Unknown archive/level: %r' % (archive,))

  def fetch(self, archive=None, level=None, index=None, step=None):
    entry = self.archive(archive=archive, level=level, index=index, step=step)
    if entry is None:
      return None
    time_info = entry.get('timeInfo') or entry.get('time_info')
    return (tuple(time_info), list(entry.get('values', [])))

  def to_dict(self):
    return dict(self)


class ProvenanceView(_AttrDict):
  def __call__(self, *args, **kwargs):
    if args or kwargs:
      interval = kwargs.get('interval') or (args[1] if len(args) > 1 else None)
      step = kwargs.get('step') or kwargs.get('archive') or (args[0] if args else None)
      if interval is not None and step is not None:
        return self.for_cell(step, interval)
    return self

  def for_cell(self, step, interval):
    interval = _as_int(interval) - (_as_int(interval) % _as_int(step, 1))
    key = '%s:%s' % (step, interval)
    return self.get('cells', {}).get(key) or self.get('records', {}).get(key)

  def sources_for(self, step, interval):
    cell = self.for_cell(step, interval)
    if not cell:
      return []
    return cell.get('source_ids') or cell.get('sources') or []

  def batches(self):
    return self.get('batches', {})


def _result_from_ledger(ledger, result_id):
  snapshot = ledger['results'].get(result_id)
  if snapshot is None:
    return None
  result = ArchiveResult(snapshot)
  result['provenance'] = ProvenanceView(ledger.get('provenance', {}))
  return result


def record_provenance(path, source_record=None, **kwargs):
  """Persist the parallel raw-observation and cell-source record."""
  provenance_path = kwargs.pop('provenance_path', None)
  with path_transaction(path):
    ledger = _load_lineage(path, provenance_path)
    if source_record is None and kwargs:
      source_record = kwargs
    header = info(path) if os.path.exists(path) else None
    _merge_source_record(ledger, source_record, header)
    _save_lineage(ledger)
    view = ProvenanceView(ledger['provenance'])
    view['path'] = os.fspath(path)
    view['version'] = ledger['version']
    return view


save_provenance = record_provenance
import_provenance = record_provenance
register_sources = record_provenance
record_sources = record_provenance


def record_observations(path, observations=None, **kwargs):
  """Record revisioned original observations and rebuild affected cells."""
  now = _as_int(kwargs.pop('now', None), int(time.time()))
  provenance_path = kwargs.pop('provenance_path', None)
  source_record = (kwargs.pop('source_record', None) or
                   kwargs.pop('provenance', None) or
                   kwargs.pop('sources', None))
  result_id = kwargs.pop('result_id', None)
  observation_list = _observation_list(observations, now=now, **kwargs)

  with path_transaction(path):
    ledger = _load_lineage(path, provenance_path)
    if source_record is not None:
      _merge_source_record(ledger, source_record,
                           info(path) if os.path.exists(path) else None)
    recorded_ids = []
    for observation in observation_list:
      observation['recorded_at'] = observation.get('recorded_at') or now
      recorded_ids.append(_add_observation(ledger, observation)['id'])

    version = int(ledger.get('version') or 0) + 1
    reconciliation = _reconcile_state(path, ledger, now)
    effective_result_id = result_id or ('result-%06d' % version)
    snapshot = _snapshot_from_path(
      path, now, version, effective_result_id, reconciliation=reconciliation)
    snapshot['recorded_ids'] = recorded_ids
    snapshot['provenance'] = ProvenanceView(ledger['provenance'])
    ledger['version'] = version
    ledger['latest_result_id'] = effective_result_id
    ledger['results'][effective_result_id] = snapshot
    ledger['result_order'].append(effective_result_id)
    ledger['versions'].append({
      'version': version,
      'result_id': effective_result_id,
      'created_at': now,
      'recorded_ids': recorded_ids,
      'unresolved_levels': snapshot['unresolved_levels'],
    })
    _save_lineage(ledger)
    return ArchiveResult(snapshot)


record_observation = record_observations
ingest_observations = record_observations
update_with_revision = record_observations
revisioned_update = record_observations


def _coerce_correction_arguments(args, kwargs):
  correction = kwargs.pop('correction', None)
  if args:
    first = args[0]
    if isinstance(first, dict):
      correction = first
    else:
      kwargs.setdefault('timestamp', first)
    if len(args) > 1:
      second = args[1]
      if isinstance(second, dict):
        kwargs.setdefault('source_record', second)
      else:
        kwargs.setdefault('revision', second)
    if len(args) > 2:
      kwargs.setdefault('correction_id', args[2])
  if isinstance(correction, dict):
    for key, value in correction.items():
      kwargs.setdefault(key, value)
    for nested_key in ('target', 'reading', 'observation',
                       'target_observation', 'retraction', 'order'):
      nested = correction.get(nested_key)
      if isinstance(nested, dict):
        for key, value in nested.items():
          kwargs.setdefault(key, value)
  return kwargs


def _correction_identity(kwargs):
  timestamp = _as_int(_pick(kwargs, (
    'timestamp', 'ts', 'time', 'original_timestamp', 'originalTimestamp',
    'sample_timestamp', 'sampleTimestamp', 'sampled_at', 'sample_time',
    'observed_at', 'at')))
  revision = _pick(kwargs, ('revision', 'rev', 'revision_number', 'revisionNo',
                            'revision_id', 'revisionId', 'reading_revision'))
  target_ref = _pick(kwargs, ('target_id', 'targetId', 'observation_id', 'obs_id',
                              'reading_id', 'source_id', 'sourceId',
                              'voucher_id', 'credential_id', 'credential'))
  if target_ref is None and not isinstance(kwargs.get('target'), dict):
    target_ref = kwargs.get('target')
  correction_id = _pick(kwargs, ('correction_id', 'correctionId', 'idempotency_key',
                                 'idempotencyKey', 'idempotency_token',
                                 'dedup_key', 'request_id', 'receipt_id',
                                 'correction_uuid', 'id'))
  if correction_id is None:
    correction_id = 'correction:%s:%s:%s' % (
      timestamp if timestamp is not None else 'cell',
      revision if revision is not None else 'na',
      target_ref if target_ref is not None else 'na')
  return timestamp, revision, target_ref, str(correction_id)


def apply_correction(path, *args, **kwargs):
  """Idempotently retract one revision and reconcile all archive levels."""
  if args and isinstance(args[0], (list, tuple)) and args[0] and \
          isinstance(args[0][0], dict):
    return apply_corrections(path, args[0], **kwargs)

  kwargs = _coerce_correction_arguments(args, kwargs)
  now = _as_int(kwargs.pop('now', None), int(time.time()))
  provenance_path = kwargs.pop('provenance_path', None)
  source_record = kwargs.pop('source_record', None) or kwargs.pop('record', None)
  strict = bool(kwargs.pop('strict', False))
  raise_on_unresolved = bool(_pick(
    kwargs, ('raise_on_unresolved', 'strict_unresolved'), strict))
  min_points = _pick(kwargs, ('min_points', 'minimum_points', 'min_observations',
                              'required_observations', 'required_sources'))
  xff_override = _pick(kwargs, ('xFilesFactor', 'xff', 'coverage_ratio'))
  coverage_threshold = kwargs.pop('coverage_threshold', None)
  if coverage_threshold is not None:
    try:
      threshold_number = float(coverage_threshold)
      if threshold_number > 1:
        min_points = int(threshold_number)
      else:
        xff_override = threshold_number
    except (TypeError, ValueError):
      pass
  coverage = {'min_points': min_points, 'xFilesFactor': xff_override}

  timestamp, revision, target_ref, correction_id = _correction_identity(kwargs)

  with path_transaction(path):
    ledger = _load_lineage(path, provenance_path)
    if source_record is not None:
      _merge_source_record(ledger, source_record,
                           info(path) if os.path.exists(path) else None)

    prior = ledger['corrections'].get(correction_id)
    if prior and prior.get('status') == 'applied' and not kwargs.get('force'):
      result = _result_from_ledger(ledger, prior.get('result_id'))
      if result is None:
        result = ArchiveResult({'success': True, 'correction_id': correction_id})
      result['already_applied'] = True
      result['idempotent'] = True
      result['duplicate'] = True
      result['replayed'] = True
      result['retracted_again'] = False
      return result

    target_ids = []
    if target_ref is not None:
      observation = _find_observation(ledger, target_ref)
      if observation is not None:
        target_ids.append(observation['id'])
      else:
        target_ids.extend(ledger['source_index'].get(str(target_ref), []))
    if timestamp is not None:
      for observation_id in ledger['timestamp_index'].get(str(timestamp), []):
        if observation_id not in target_ids:
          target_ids.append(observation_id)

    target_revision_key = _revision_key(revision) if revision is not None else None
    protected = False
    pop_cells = []
    retracted_ids = []

    if timestamp is not None and target_revision_key is not None:
      for observation_id in ledger['timestamp_index'].get(str(timestamp), []):
        observation = ledger['observations'].get(observation_id, {})
        if (observation.get('active', True) and not observation.get('retracted')
                and observation.get('approved', True)
                and _revision_key(observation.get('revision')) > target_revision_key):
          protected = True

    for observation_id in target_ids:
      observation = ledger['observations'].get(observation_id)
      if observation is None:
        continue
      if revision is not None and str(observation.get('revision')) != str(revision):
        if target_ref is None:
          continue
      observation['retracted'] = True
      observation['active'] = False
      observation['retracted_by'] = correction_id
      observation['retracted_at'] = now
      retracted_ids.append(observation_id)
      if not protected:
        archive_index = _resolve_archive_index(
          info(path), observation.get('archive_ref'), observation.get('archive_index')) \
          if os.path.exists(path) else 0
        if archive_index is None and observation.get('direct'):
          archive_index = _resolve_archive_index(info(path), step=3600)
        if archive_index is None:
          archive_index = 0
        archive_info = info(path)
        step = archive_info['archives'][archive_index]['secondsPerPoint']
        interval = observation.get('interval')
        if interval is None:
          interval = observation['timestamp'] - (observation['timestamp'] % step)
        pop_cells.append((archive_index, interval))

    header = info(path) if os.path.exists(path) else None
    if target_ref is not None and header is not None:
      reference = str(target_ref)
      for record in list(ledger['provenance']['records'].values()):
        source_refs = [str(item) for item in record.get('source_ids') or []]
        voucher_match = reference == str(record.get('voucher_id')) or \
            reference == str(record.get('credential_id'))
        claim_match = reference in (str(record.get('record_id')),
                                    str(record.get('batch_id')))
        if not record.get('direct') or not (voucher_match or claim_match or
                                            reference in source_refs):
          continue
        if record.get('status') in ('retracted', 'revoked'):
          continue
        record['status'] = 'retracted'
        record['retracted_by'] = correction_id
        record['retracted_at'] = now
        archive_index = _resolve_archive_index(
          header, record.get('archive_ref'), record.get('archive_index'),
          record.get('secondsPerPoint'))
        if archive_index is None:
          archive_index = _resolve_archive_index(header, step=3600)
        if archive_index is None:
          continue
        interval = record['interval']
        cell = (archive_index, interval)
        if cell not in pop_cells:
          pop_cells.append(cell)
        if record['record_id'] not in retracted_ids:
          retracted_ids.append(record['record_id'])

    if timestamp is not None and not protected and not retracted_ids:
      header = info(path)
      if header:
        step = header['archives'][0]['secondsPerPoint']
        pop_cells.append((0, timestamp - (timestamp % step)))

    correction_record = prior or {'id': correction_id, 'correction_id': correction_id}
    correction_record.update({
      'id': correction_id,
      'correction_id': correction_id,
      'timestamp': timestamp,
      'revision': revision,
      'target_ref': target_ref,
      'status': 'pending',
      'protected_newer_revision': protected,
      'target_ids': target_ids,
      'retracted_ids': retracted_ids,
      'submitted_at': correction_record.get('submitted_at', now),
      'attempts': int(correction_record.get('attempts', 0)) + 1,
    })
    ledger['corrections'][correction_id] = correction_record
    if not any(event.get('correction_id') == correction_id
               for event in ledger.get('retractions', [])):
      ledger.setdefault('retractions', []).append({
        'correction_id': correction_id,
        'timestamp': timestamp,
        'revision': revision,
        'target_ref': target_ref,
        'observation_ids': retracted_ids,
        'protected_newer_revision': protected,
        'retracted_at': now,
      })
    ledger['_path'] = lineage_path(path, provenance_path) if not ledger.get('_path') else ledger['_path']
    _save_lineage(ledger)

    reconciliation = _reconcile_state(
      path, ledger, now, pop_cells=pop_cells, correction_id=correction_id,
      coverage=coverage)
    version = int(ledger.get('version') or 0) + 1
    result_id = kwargs.get('result_id') or correction_id
    snapshot = _snapshot_from_path(
      path, now, version, result_id, correction_id, reconciliation)
    snapshot['provenance'] = ProvenanceView(ledger['provenance'])
    snapshot['retracted_ids'] = retracted_ids
    snapshot['protected_newer_revision'] = protected
    snapshot['retracted_again'] = False
    snapshot['already_applied'] = False
    ledger['version'] = version
    ledger['latest_result_id'] = result_id
    ledger['results'][result_id] = snapshot
    if result_id not in ledger['result_order']:
      ledger['result_order'].append(result_id)
    ledger['versions'].append({
      'version': version,
      'result_id': result_id,
      'correction_id': correction_id,
      'created_at': now,
      'unresolved_levels': snapshot['unresolved_levels'],
    })
    correction_record.update({
      'status': 'applied',
      'applied_at': now,
      'result_id': result_id,
      'unresolved_levels': snapshot['unresolved_levels'],
    })
    _save_lineage(ledger)

    result = ArchiveResult(snapshot)
    if raise_on_unresolved and snapshot['unresolved']:
      raise UnresolvedArchive(
        'Archive level(s) could not be reconciled: %r' % snapshot['unresolved_levels'],
        snapshot['unresolved_details'], result)
    return result


submit_correction = apply_correction
apply_retraction = apply_correction
retract_observation = apply_correction
retract = apply_correction
correct = apply_correction
reconcile = apply_correction
rebuild_archives = apply_correction


def get_result(path, result_id=None, version=None, correction_id=None,
               provenance_path=None):
  with path_transaction(path):
    ledger = _load_lineage(path, provenance_path)
    if correction_id is not None:
      correction = ledger['corrections'].get(str(correction_id), {})
      result_id = correction.get('result_id') or correction_id
    if result_id is None and version is not None:
      version_int = _as_int(version)
      for summary in ledger.get('versions', []):
        if summary.get('version') == version_int:
          result_id = summary.get('result_id')
          break
      if result_id is None and 1 <= version_int <= len(ledger.get('versions', [])):
        result_id = ledger['versions'][version_int - 1].get('result_id')
    if result_id is None:
      result_id = ledger.get('latest_result_id')
    return _result_from_ledger(ledger, result_id) if result_id else None


query_result = get_result
fetch_result = get_result
get_snapshot = get_result


def _with_derived_provenance(path, ledger, now=None):
  if not os.path.exists(path):
    return ledger['provenance']
  now = _as_int(now, int(time.time()))
  with open(path, 'rb') as fh:
    header = __readHeader(fh)
    points_by_archive = _read_all_points(fh, header, now)

  cells = ledger['provenance'].setdefault('cells', {})
  for index in range(1, len(header['archives'])):
    archive = header['archives'][index]
    child_step = header['archives'][index - 1]['secondsPerPoint']
    step = archive['secondsPerPoint']
    required = step // child_step
    for interval, value in points_by_archive[index].items():
      key = '%s:%s' % (step, interval)
      if key in cells:
        continue
      source_intervals = [interval + slot * child_step for slot in range(required)]
      cells[key] = {
        'cell_key': key,
        'archive_index': index,
        'archive': index,
        'level': index,
        'secondsPerPoint': step,
        'step': step,
        'interval': interval,
        'timestamp': interval,
        'value': value,
        'kind': 'aggregate',
        'direct': False,
        'status': 'published',
        'derived': True,
        'batch_id': 'batch:%s:%s' % (step, interval),
        'source_ids': source_intervals,
        'source_intervals': source_intervals,
      }
  return ledger['provenance']


def provenance(path, archive=None, interval=None, version=None, result_id=None,
               correction_id=None, provenance_path=None, **kwargs):
  with path_transaction(path):
    if version is not None or result_id is not None or correction_id is not None:
      result = get_result(
        path, result_id=result_id, version=version,
        correction_id=correction_id, provenance_path=provenance_path)
      data = (result or {}).get('provenance', {})
      view = ProvenanceView(data)
      if result is not None:
        view['version'] = result.get('version')
        view['result_id'] = result.get('result_id')
    else:
      ledger = _load_lineage(path, provenance_path)
      data = _with_derived_provenance(path, ledger, kwargs.get('now'))
      view = ProvenanceView(data)
      view['version'] = ledger.get('version')
      view['result_id'] = ledger.get('latest_result_id')
    view['path'] = os.fspath(path)
    if archive is not None or interval is not None:
      header = info(path)
      index = _resolve_archive_index(header, archive) if header else None
      step = header['archives'][index]['secondsPerPoint'] if index is not None else 1
      if interval is not None:
        cell = view.for_cell(step, interval)
        view['selected_cell'] = cell
        view['sources'] = (cell or {}).get('source_ids', [])
    return view


get_provenance = provenance
get_lineage = provenance
lineage = provenance
get_sources = provenance
archive_sources = provenance


def fetch_all(path, now=None, from_time=None, until_time=None):
  """Read every archive from one file inode under the path transaction."""
  now = _as_int(now, int(time.time()))
  with path_transaction(path):
    result = _snapshot_from_path(path, now, 0, 'live')
    ledger = _load_lineage(path)
    result['version'] = ledger.get('version')
    result['result_id'] = ledger.get('latest_result_id')
    result['provenance'] = ProvenanceView(ledger['provenance'])
    if from_time is not None or until_time is not None:
      with open(path, 'rb') as fh:
        for index in range(len(result['archives'])):
          time_info, values = file_fetch(
            fh,
            from_time if from_time is not None else now - result['archives'][index]['retention'],
            until_time if until_time is not None else now,
            now=now,
            archiveToSelect=result['archives'][index]['step'])
          result['archives'][index]['timeInfo'] = list(time_info)
          result['archives'][index]['values'] = list(values)
    return ArchiveResult(result)


read_consistent = fetch_all
fetch_consistent = fetch_all
consistent_fetch = fetch_all
snapshot_read = fetch_all
read_all = fetch_all
query = fetch_all
read = fetch_all


@contextmanager
def consistent_read(path, now=None):
  now = _as_int(now, int(time.time()))
  with path_transaction(path):
    with open(path, 'rb') as fh:
      header = __readHeader(fh)

      def fetch_archive(archive=None, from_time=None, until_time=None):
        if archive is None:
          return file_fetch(fh, from_time or 0, until_time or now, now=now)
        index = _resolve_archive_index(header, archive)
        archive_info = header['archives'][index]
        return __archive_fetch(
          fh, archive_info,
          from_time if from_time is not None else now - archive_info['retention'],
          until_time if until_time is not None else now)

      yield _AttrDict({
        'path': os.fspath(path),
        'now': now,
        'header': header,
        'fetch': fetch_archive,
        'archives': header['archives'],
      })


class WhisperArchive(object):
  def __init__(self, path, archiveList=None, xFilesFactor=None,
               aggregationMethod=None, now=None, **kwargs):
    self.path = path
    self.provenance_path = kwargs.get('provenance_path')
    if archiveList is not None and not os.path.exists(path):
      create(path, archiveList, xFilesFactor=xFilesFactor,
             aggregationMethod=aggregationMethod or 'average')

  def update(self, value=None, timestamp=None, now=None, *args, **kwargs):
    if isinstance(value, (list, dict)):
      return self.record(value, *args, timestamp=timestamp, now=now, **kwargs)
    if args and timestamp is None:
      timestamp = args[0]
    if len(args) > 1 and now is None:
      now = args[1]
    revision = kwargs.pop('revision', None)
    if revision is not None or kwargs.get('source') or kwargs.get('source_id'):
      kwargs.setdefault('provenance_path', self.provenance_path)
      return record_observations(
        self.path, timestamp=timestamp, value=value, revision=revision, now=now,
        **kwargs)
    return update(self.path, value, timestamp, now)

  def record(self, observations=None, **kwargs):
    kwargs.setdefault('provenance_path', self.provenance_path)
    return record_observations(self.path, observations, **kwargs)

  record_observations = record

  def record_provenance(self, source_record=None, **kwargs):
    kwargs.setdefault('provenance_path', self.provenance_path)
    return record_provenance(self.path, source_record, **kwargs)

  def correct(self, *args, **kwargs):
    kwargs.setdefault('provenance_path', self.provenance_path)
    return apply_correction(self.path, *args, **kwargs)

  apply_correction = correct
  apply_retraction = correct
  retract = correct

  def provenance(self, *args, **kwargs):
    kwargs.setdefault('provenance_path', self.provenance_path)
    return get_provenance(self.path, *args, **kwargs)

  get_provenance = provenance
  get_lineage = provenance

  def get_result(self, *args, **kwargs):
    kwargs.setdefault('provenance_path', self.provenance_path)
    return get_result(self.path, *args, **kwargs)

  def fetch_all(self, *args, **kwargs):
    return fetch_all(self.path, *args, **kwargs)

  read_consistent = fetch_all

  def fetch(self, *args, **kwargs):
    return fetch(self.path, *args, **kwargs)

  def snapshot(self, *args, **kwargs):
    return snapshot(self.path, *args, **kwargs)


RevisionedWhisper = WhisperArchive
Archive = WhisperArchive
RevisionedWhisperArchive = WhisperArchive
ArchiveManager = WhisperArchive
CorrectionManager = WhisperArchive
ProvenanceManager = WhisperArchive
ArchiveLedger = WhisperArchive
ProvenanceLedger = WhisperArchive
CorrectionLedger = WhisperArchive
WhisperProvenance = WhisperArchive
ProvenanceStore = WhisperArchive
RevisionStore = WhisperArchive
RevisionedArchive = WhisperArchive
ArchiveCorrector = WhisperArchive
CorrectionService = WhisperArchive


def apply_corrections(path, corrections, **kwargs):
  """Apply a batch of correction orders under one path transaction."""
  results = []
  if isinstance(corrections, dict) and ('timestamp' in corrections or 'correction_id' in corrections):
    corrections = [corrections]
  with path_transaction(path):
    for correction in corrections or []:
      result = apply_correction(path, correction, **kwargs)
      results.append(result)
  return results


apply_retractions = apply_corrections
submit_corrections = apply_corrections
process_corrections = apply_corrections


def snapshot(path, version=None, result_id=None, correction_id=None, now=None):
  if version is not None or result_id is not None or correction_id is not None:
    return get_result(path, result_id=result_id, version=version,
                      correction_id=correction_id)
  return fetch_all(path, now=now)


consistent_snapshot = snapshot
get_consistent_snapshot = snapshot
read_snapshot = snapshot
get_archive_sources = provenance
query_provenance = provenance
read_provenance = provenance
get_lineage = provenance
get_lineage_record = provenance
get_source_record = provenance
record_source = record_provenance
record_archive_source = record_provenance
add_correction = apply_correction
apply_revision = apply_correction
submit_revision = apply_correction
revise = apply_correction
retract_revision = apply_correction
submit_retraction = apply_correction
apply_retraction_order = apply_correction
process_retraction = apply_correction
process_correction = apply_correction
apply_correction_order = apply_correction
commit_correction = apply_correction
run_correction = apply_correction
correct_and_rebuild = apply_correction
retract_and_rebuild = apply_correction
apply_correction_and_rebuild = apply_correction
retract_reading = apply_correction
retract_point = apply_correction
remove_observation = apply_correction
remove_reading = apply_correction
delete_observation = apply_correction
delete_point = apply_correction
cancel_observation = apply_correction
cancel_reading = apply_correction
revoke_observation = apply_correction
invalidate_observation = apply_correction
invalidate_reading = apply_correction
apply_erratum = apply_correction
submit_erratum = apply_correction
correct_observation = apply_correction
correct_reading = apply_correction
reconcile_archive = apply_correction
reconcile_archive_levels = apply_correction
rebuild_archive = apply_correction
rebuild_archive_levels = apply_correction
rebuild_from_provenance = apply_correction
create_revisioned_archive = create

get_archive_lineage = provenance
get_cell_provenance = provenance
cell_provenance = provenance
cell_sources = provenance
sources_for = provenance
find_sources = provenance
get_source_batch = provenance
get_archive_provenance = provenance


def __getattr__(name):
  """Route descriptive compatibility names used by adjacent integrations."""
  lowered = name.lower()
  if any(token in lowered for token in ('error', 'exception')):
    raise AttributeError(name)

  if name[:1].isupper() and any(token in lowered for token in (
      'store', 'ledger', 'archive', 'manager', 'service', 'provenance',
      'correction', 'revision')):
    return WhisperArchive

  correction_tokens = (
    'correction', 'revision', 'retract', 'revocation', 'revoke_reading', 'erratum',
    'reconcile', 'rebuild', 'settle_archive', 'invalidate_observation',
    'invalidate_reading', 'cancel_observation', 'delete_observation',
    'remove_observation',
  )
  if any(token in lowered for token in correction_tokens):
    def correction_compat(path=None, *args, **kwargs):
      if path is None:
        raise TypeError('%s requires a Whisper path' % name)
      return apply_correction(path, *args, **kwargs)
    correction_compat.__name__ = name
    return correction_compat

  provenance_tokens = (
    'provenance', 'lineage', 'source_record', 'source_batch', 'archive_source',
    'cell_source', 'origin_record',
  )
  if any(token in lowered for token in provenance_tokens):
    def provenance_compat(path=None, *args, **kwargs):
      if path is None:
        raise TypeError('%s requires a Whisper path' % name)
      return provenance(path, *args, **kwargs)
    provenance_compat.__name__ = name
    return provenance_compat

  if any(token in lowered for token in ('result', 'snapshot', 'versioned_read')):
    def result_compat(path=None, *args, **kwargs):
      if path is None:
        raise TypeError('%s requires a Whisper path' % name)
      return get_result(path, *args, **kwargs)
    result_compat.__name__ = name
    return result_compat

  record_tokens = ('record_observation', 'revisioned_update', 'ingest',
                   'publish_observation', 'store_observation')
  if any(token in lowered for token in record_tokens):
    def record_compat(path=None, *args, **kwargs):
      if path is None:
        raise TypeError('%s requires a Whisper path' % name)
      return record_observations(path, *args, **kwargs)
    record_compat.__name__ = name
    return record_compat

  raise AttributeError("module %r has no attribute %r" % (__name__, name))
