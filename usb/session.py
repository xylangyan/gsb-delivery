# Copyright 2009-2026 PyUSB contributors
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the conditions are met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

r"""usb.session - Multi-job acquisition sessions on a shared Device.

A measurement board may expose several jobs on the same
:class:`usb.core.Device`: for example, an interface with two gears
(low/high speed alternate settings) that share one endpoint address but
describe different transfer types and sample lengths, plus an independent
calibration interface.

This module lets every job hold its own reader while sharing one device
handle.  Every completed read returns a :class:`ReadResult` carrying the
gear, interface, endpoint address, transfer type and raw bytes that were
true when the request was submitted, so decoders never have to infer the
provenance of a sample from the gear the device happens to be in when the
packet arrives.

Typical use::

    with AcquisitionSession(dev) as session:
        measurement = session.open_job('measurement', 1, 0x81, gear=0)
        calibration = session.open_job('calibration', 2, 0x82)

        result = measurement.read(64)
        session.switch_gear(1, 1, timeout=5000)
        result = measurement.read(64)

        calibration.close()
        measurement.close()

The device handle is released only after the last job (or the session
itself) is closed.  Code that keeps using ``Device.read()`` directly is
unaffected.
"""

__all__ = [
    'AcquisitionSession',
    'ReadJob',
    'ReadResult',
    'SwitchResult',
    'ReadCancelled',
    'SwitchTimeout',
]

import threading
import time

import usb.util as util
from usb.core import USBError


class ReadCancelled(USBError):
    r"""Raised when a read request is cancelled before reaching the driver.

    The request had selected its endpoint and gear, but the transfer had
    not started when a gear switch (or job shutdown) cancelled it.  No
    backend transfer was issued for it.
    """

    def __init__(self, request_id, gear, interface, endpoint):
        USBError.__init__(
            self,
            'read request %d on interface %d endpoint %#x was cancelled '
            'before reaching the driver' % (request_id, interface, endpoint))
        self.request_id = request_id
        self.gear = gear
        self.interface = interface
        self.endpoint = endpoint


class SwitchTimeout(Exception):
    r"""Raised when a gear switch cannot finish before its timeout.

    The previous gear stays active and acquisition may continue.  The
    timed-out switch never applies itself later: once the in-flight
    transfers finish, no surprise alternate-setting call happens.

    If the backend can report the real alternate setting,
    ``verified_gear`` holds it; otherwise it is ``None``.
    """

    def __init__(self, interface, gear, verified_gear):
        if verified_gear is None:
            msg = ('timed out switching interface %d to gear %d; the '
                   'previous gear remains active' % (interface, gear))
        else:
            msg = ('timed out switching interface %d to gear %d; the '
                   'driver currently reports gear %d'
                   % (interface, gear, verified_gear))
        Exception.__init__(self, msg)
        self.interface = interface
        self.gear = gear
        self.verified_gear = verified_gear


class ReadResult(object):
    r"""The provenance and payload of one completed read.

    Attributes:
        request_id: per-session identifier of the read request.
        gear: gear (alternate setting value) the request was submitted for.
        interface: bInterfaceNumber the transfer was claimed on.
        alternate_setting: bAlternateSetting in effect when the endpoint
            was resolved.
        endpoint: bEndpointAddress actually used.
        transfer_type: transfer type (see the ``ENDPOINT_TYPE_*``
            constants in :mod:`usb.util`) resolved from the descriptor.
        data: ``array.array('B')`` with the raw bytes actually received.
        length: number of valid bytes in ``data`` (short reads included).
    """

    __slots__ = (
        'request_id', 'gear', 'interface', 'alternate_setting',
        'endpoint', 'transfer_type', 'data', 'length',
    )

    def __init__(self, request_id, gear, interface, alternate_setting,
                 endpoint, transfer_type, data, length):
        self.request_id = request_id
        self.gear = gear
        self.interface = interface
        self.alternate_setting = alternate_setting
        self.endpoint = endpoint
        self.transfer_type = transfer_type
        self.data = data
        self.length = length

    def __repr__(self):
        return ('ReadResult(request_id=%d, gear=%d, interface=%d, '
                'endpoint=%#x, transfer_type=%d, length=%d)'
                % (self.request_id, self.gear, self.interface,
                   self.endpoint, self.transfer_type, self.length))


class SwitchResult(object):
    r"""Outcome of a gear switch.

    Attributes:
        interface: interface whose gear changed.
        gear: gear active after the switch.
        previous_gear: gear active before the switch.
        switched: ``False`` when the requested gear was already active.
        cancelled: number of pending requests cancelled before they
            reached the driver.
        drained: number of in-flight, driver-visible transfers that had
            to finish before the switch could be applied.
    """

    __slots__ = (
        'interface', 'gear', 'previous_gear', 'switched',
        'cancelled', 'drained',
    )

    def __init__(self, interface, gear, previous_gear, switched,
                 cancelled, drained):
        self.interface = interface
        self.gear = gear
        self.previous_gear = previous_gear
        self.switched = switched
        self.cancelled = cancelled
        self.drained = drained

    def __repr__(self):
        return ('SwitchResult(interface=%d, gear=%d, previous_gear=%d, '
                'switched=%r, cancelled=%d, drained=%d)'
                % (self.interface, self.gear, self.previous_gear,
                   self.switched, self.cancelled, self.drained))


class _Request(object):
    __slots__ = (
        'request_id', 'job', 'gear', 'alternate_setting', 'interface',
        'endpoint', 'transfer_type', 'started', 'cancelled', 'event',
    )

    def __init__(self, request_id, job, gear, alternate_setting,
                 endpoint, transfer_type):
        self.request_id = request_id
        self.job = job
        self.gear = gear
        self.alternate_setting = alternate_setting
        self.interface = job.interface
        self.endpoint = endpoint
        self.transfer_type = transfer_type
        self.started = False
        self.cancelled = False
        self.event = threading.Event()


_READ_FUNCTIONS = {
    util.ENDPOINT_TYPE_BULK: 'bulk_read',
    util.ENDPOINT_TYPE_INTR: 'intr_read',
    util.ENDPOINT_TYPE_ISO: 'iso_read',
}


class ReadJob(object):
    r"""A reader bound to one interface of a shared device.

    Jobs are created through :meth:`AcquisitionSession.open_job`.  A
    dynamic job (created with an initial ``gear``) follows gear switches
    of its interface.  A fixed job always reads the descriptor that was
    active when the job was created and is never touched by switches of
    other interfaces.
    """

    def __init__(self, session, name, interface, endpoint, gear):
        self._session = session
        self.name = name
        self.interface = interface
        self.endpoint = endpoint
        self.dynamic = gear is not None
        self.fixed_gear = gear
        self.paused = False
        self.closed = False
        self._inflight = {}

    def read(self, size, timeout=None):
        r"""Read one sample, returning a provenance-carrying ReadResult."""
        return self._session._read(self, size, timeout)

    def switch_gear(self, gear, timeout=None):
        r"""Switch the gear of this job's dynamic interface."""
        if not self.dynamic:
            raise ValueError('job %r is not attached to a switchable '
                             'interface' % self.name)
        return self._session.switch_gear(self.interface, gear, timeout)

    def pause(self):
        r"""Reject new reads until :meth:`resume` is called.

        Pending requests that have not reached the driver are cancelled.
        Requests already in the driver are left to finish normally.
        """
        self._session._pause_job(self)

    def resume(self):
        r"""Allow reads again after a :meth:`pause`."""
        self._session._resume_job(self)

    def close(self):
        r"""Release this job; the device handle closes with the last user."""
        self._session._close_job(self)

    def __repr__(self):
        kind = 'dynamic' if self.dynamic else 'fixed'
        return ('ReadJob(name=%r, interface=%d, endpoint=%#x, %s)'
                % (self.name, self.interface, self.endpoint, kind))


class AcquisitionSession(object):
    r"""Coordinates several read jobs sharing one :class:`usb.core.Device`.

    The session serializes gear switches against in-flight transfers and
    keeps the single device handle open until the last job closes.  It is
    usable as a context manager; leaving the ``with`` block closes every
    job still open and releases the device.
    """

    def __init__(self, device, on_request_pending=None):
        self._dev = device
        # Test/extension hook: invoked after a request has selected its
        # endpoint but before it enters the driver.
        self.on_request_pending = on_request_pending
        self._cond = threading.Condition(threading.RLock())
        self._jobs = {}
        self._inflight = []
        self._gears = {}
        self._dynamic_interfaces = set()
        self._switching = {}
        self._next_request_id = 0
        self._users = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def open_job(self, name, interface, endpoint, gear=None):
        r"""Open a read job on an interface/endpoint pair.

        Parameters:
            name: unique, human readable job name.
            interface: the ``bInterfaceNumber`` to read from.
            endpoint: the ``bEndpointAddress`` to read from.
            gear: initial gear (alternate setting value) for an
                interface that can be switched.  ``None`` creates a fixed
                job that is unaffected by switches.
        """
        with self._cond:
            if name in self._jobs:
                raise ValueError('job %r is already open' % name)
            job = ReadJob(self, name, interface, endpoint, gear)
            if job.dynamic:
                self._dynamic_interfaces.add(interface)
                self._gears.setdefault(interface, gear)
            else:
                intf_obj, _ = self._resolve(interface, endpoint)
                job.fixed_gear = intf_obj.bAlternateSetting
            self._jobs[name] = job
            self._users += 1
            return job

    def switch_gear(self, interface, gear, timeout=None):
        r"""Switch one dynamic interface to another gear.

        Pending requests that have not entered the driver are cancelled
        with :class:`ReadCancelled`.  Requests already visible to the
        driver are awaited: the switch is only applied once they finish,
        or once ``timeout`` (milliseconds) elapses.

        Returns a :class:`SwitchResult`; raises :class:`SwitchTimeout`
        when the wait expires, leaving the previous gear active.
        """
        with self._cond:
            if interface not in self._dynamic_interfaces:
                raise ValueError('interface %d has no dynamic job'
                                 % interface)
            if self._switching.get(interface) is not None:
                raise USBError('a gear switch on interface %d is already '
                               'in progress' % interface)
            previous_gear = self._gears[interface]
            if gear == previous_gear:
                return SwitchResult(interface, gear, previous_gear,
                                    False, 0, 0)
            self._switching[interface] = gear

            pending, started = [], []
            for request in self._inflight:
                if request.interface != interface:
                    continue
                if request.started:
                    started.append(request)
                else:
                    request.cancelled = True
                    request.event.set()
                    pending.append(request)

        deadline = None if timeout is None else \
            time.monotonic() + timeout / 1000.0

        for request in started:
            if not request.event.wait(self._remaining(deadline)):
                verified_gear = self._query_driver_gear(interface)
                with self._cond:
                    self._switching.pop(interface, None)
                    self._cond.notify_all()
                raise SwitchTimeout(interface, gear, verified_gear)

        try:
            self._dev.set_interface_altsetting(interface, gear)
        except USBError:
            with self._cond:
                self._switching.pop(interface, None)
                self._cond.notify_all()
            raise

        with self._cond:
            self._gears[interface] = gear
            self._switching.pop(interface, None)
            self._cond.notify_all()

        return SwitchResult(interface, gear, previous_gear, True,
                            len(pending), len(started))

    def get_gear(self, interface, verify=False):
        r"""Return the gear recorded for an interface.

        With ``verify=True`` the backend is asked for the real alternate
        setting (through an optional ``get_interface_altsetting`` backend
        method) and the cached gear is updated from it.  Returns ``None``
        when the backend cannot verify the setting.
        """
        with self._cond:
            cached = self._gears.get(interface)
        if not verify:
            return cached
        verified = self._query_driver_gear(interface)
        if verified is not None:
            with self._cond:
                self._gears[interface] = verified
        return verified

    def in_flight(self, job=None):
        r"""Return the requests currently registered, optionally per job."""
        with self._cond:
            requests = list(self._inflight)
        if job is not None:
            requests = [r for r in requests if r.job is job]
        return requests

    def close(self):
        r"""Close every job and release the shared device handle."""
        with self._cond:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.close()

    # ------------------------------------------------------------------
    # Internal mechanics, always called through a ReadJob.
    # ------------------------------------------------------------------

    def _read(self, job, size, timeout=None):
        with self._cond:
            self._check_usable(job)
            while self._switching.get(job.interface) is not None:
                self._cond.wait()
            self._check_usable(job)

            gear = self._current_gear(job)
            intf_obj, ep_obj = self._resolve(job.interface, job.endpoint)
            transfer_type = util.endpoint_type(ep_obj.bmAttributes)
            request = _Request(
                self._next_request_id, job, gear,
                intf_obj.bAlternateSetting,
                ep_obj.bEndpointAddress, transfer_type)
            self._next_request_id += 1
            self._inflight.append(request)
            job._inflight[request.request_id] = request
            hook = self.on_request_pending

        if hook is not None:
            hook(request)

        with self._cond:
            if request.cancelled:
                self._discard(request)
                raise ReadCancelled(request.request_id, request.gear,
                                    request.interface, request.endpoint)
            request.started = True

        self._dev._ctx.managed_claim_interface(self._dev, job.interface)
        backend = self._dev._ctx.backend
        read_fn = getattr(backend, _READ_FUNCTIONS[request.transfer_type])
        buffer = util.create_buffer(size)

        try:
            length = read_fn(
                self._dev._ctx.handle,
                request.endpoint,
                job.interface,
                buffer,
                self._timeout(timeout))
        except BaseException:
            with self._cond:
                self._discard(request)
                request.event.set()
                self._cond.notify_all()
            raise

        with self._cond:
            self._discard(request)
            request.event.set()
            self._cond.notify_all()

        return ReadResult(
            request.request_id, request.gear, request.interface,
            request.alternate_setting, request.endpoint,
            request.transfer_type, buffer[:length], length)

    def _pause_job(self, job):
        with self._cond:
            job.paused = True
            for request in list(job._inflight.values()):
                if not request.started:
                    request.cancelled = True
                    request.event.set()
            self._cond.notify_all()

    def _resume_job(self, job):
        with self._cond:
            job.paused = False
            self._cond.notify_all()

    def _close_job(self, job):
        with self._cond:
            if job.closed:
                return
            job.closed = True
            for request in list(job._inflight.values()):
                if not request.started:
                    request.cancelled = True
                    request.event.set()
            # A switch on this interface must be allowed to finish so
            # that it never touches interface state torn down below.
            while job.dynamic and \
                    self._switching.get(job.interface) is not None:
                self._cond.wait()
            self._jobs.pop(job.name, None)
            if job.dynamic and not any(
                    j.interface == job.interface and j.dynamic
                    for j in self._jobs.values()):
                self._dynamic_interfaces.discard(job.interface)
                self._switching.pop(job.interface, None)
                self._gears.pop(job.interface, None)
            self._users -= 1
            remaining = self._users
            # Snapshot every transfer still inside the driver, across
            # all jobs, so concurrent closes cannot release the shared
            # handle underneath another job's in-flight transfer.
            in_flight = [r for r in self._inflight if r.started]
            self._cond.notify_all()

        if remaining == 0:
            for request in in_flight:
                request.event.wait()
            util.dispose_resources(self._dev)

    def _check_usable(self, job):
        if job.closed:
            raise USBError('job %r is closed' % job.name)
        if job.paused:
            raise USBError('job %r is paused' % job.name)

    def _current_gear(self, job):
        if job.dynamic:
            return self._gears[job.interface]
        return job.fixed_gear

    def _resolve(self, interface, endpoint):
        intf_obj, ep_obj = \
            self._dev._ctx.get_interface_and_endpoint(self._dev, endpoint)
        if intf_obj.bInterfaceNumber != interface:
            raise ValueError(
                'endpoint %#x belongs to interface %d, not %d'
                % (endpoint, intf_obj.bInterfaceNumber, interface))
        return intf_obj, ep_obj

    def _discard(self, request):
        request.job._inflight.pop(request.request_id, None)
        try:
            self._inflight.remove(request)
        except ValueError:
            pass

    def _query_driver_gear(self, interface):
        backend = self._dev._ctx.backend
        query = getattr(backend, 'get_interface_altsetting', None)
        if query is None:
            return None
        try:
            handle = self._dev._ctx.managed_open()
            return query(handle, interface)
        except NotImplementedError:
            return None

    def _timeout(self, timeout):
        if timeout is not None:
            return timeout
        return self._dev.default_timeout

    @staticmethod
    def _remaining(deadline):
        if deadline is None:
            return None
        # threading.Event.wait uses seconds, while public timeouts are
        # expressed in milliseconds like the rest of PyUSB.
        return max(0.0, deadline - time.monotonic())
