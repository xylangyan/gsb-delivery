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

import threading
import time
import unittest

import usb.backend
import usb.core
import usb.util
from usb import session as session_mod
from usb.core import USBError, USBTimeoutError

# Logical layout of the fake measurement board:
#
#   interface 1, alt 0 (low gear):  EP 0x81, bulk,      8 bytes/sample
#   interface 1, alt 1 (high gear): EP 0x81, interrupt, 32 bytes/sample
#   interface 2, alt 0 (calibration): EP 0x82, interrupt, 16 bytes/sample
#
# The two gears of interface 1 deliberately share the endpoint address
# while differing in transfer type and sample length, mirroring the real
# board described in the requirements.

MEASURE_INTF = 1
CALIB_INTF = 2
MEASURE_EP = 0x81
CALIB_EP = 0x82
LOW_GEAR = 0
HIGH_GEAR = 1


class _Desc(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.extra_descriptors = b''


class FakeBoardBackend(usb.backend.IBackend):
    r"""Local stand-in driver for one measurement board.

    Reads on an interface can be paused (the transfer stays inside the
    driver) and resumed again.  Every transfer and alternate-setting
    call is recorded with timestamps so tests can prove what really
    reached the driver.
    """

    def __init__(self):
        self._iface_desc = {
            (0, 0): _Desc(bLength=9, bDescriptorType=4,
                          bInterfaceNumber=1, bAlternateSetting=0,
                          bNumEndpoints=1, bInterfaceClass=0xff,
                          bInterfaceSubClass=0, bInterfaceProtocol=0,
                          iInterface=0),
            (0, 1): _Desc(bLength=9, bDescriptorType=4,
                          bInterfaceNumber=1, bAlternateSetting=1,
                          bNumEndpoints=1, bInterfaceClass=0xff,
                          bInterfaceSubClass=0, bInterfaceProtocol=0,
                          iInterface=0),
            (1, 0): _Desc(bLength=9, bDescriptorType=4,
                          bInterfaceNumber=2, bAlternateSetting=0,
                          bNumEndpoints=1, bInterfaceClass=0xff,
                          bInterfaceSubClass=0, bInterfaceProtocol=0,
                          iInterface=0),
        }
        self._ep_desc = {
            (0, 0, 0): _Desc(bLength=7, bDescriptorType=5,
                             bEndpointAddress=MEASURE_EP,
                             bmAttributes=usb.util.ENDPOINT_TYPE_BULK,
                             wMaxPacketSize=8, bInterval=0,
                             bRefresh=0, bSynchAddress=0),
            (0, 0, 1): _Desc(bLength=7, bDescriptorType=5,
                             bEndpointAddress=MEASURE_EP,
                             bmAttributes=usb.util.ENDPOINT_TYPE_INTR,
                             wMaxPacketSize=32, bInterval=1,
                             bRefresh=0, bSynchAddress=0),
            (0, 1, 0): _Desc(bLength=7, bDescriptorType=5,
                             bEndpointAddress=CALIB_EP,
                             bmAttributes=usb.util.ENDPOINT_TYPE_INTR,
                             wMaxPacketSize=16, bInterval=1,
                             bRefresh=0, bSynchAddress=0),
        }
        self.current_alt = {MEASURE_INTF: LOW_GEAR, CALIB_INTF: 0}
        self.gates = {MEASURE_INTF: threading.Event(),
                      CALIB_INTF: threading.Event()}
        self.gates[MEASURE_INTF].set()
        self.gates[CALIB_INTF].set()
        self.driver_entered = {MEASURE_INTF: threading.Event(),
                               CALIB_INTF: threading.Event()}
        self.transfers = []
        self.alt_calls = []
        self.claims = []
        self.releases = []
        self.open_count = 0
        self.close_count = 0
        self.short_bytes = {}
        self._seq = {MEASURE_INTF: 0, CALIB_INTF: 0}
        self._lock = threading.Lock()

    # -- descriptors ----------------------------------------------------

    def enumerate_devices(self):
        return [0]

    def get_device_descriptor(self, dev):
        return _Desc(bLength=18, bDescriptorType=1, bcdUSB=0x0200,
                     bDeviceClass=0, bDeviceSubClass=0, bDeviceProtocol=0,
                     bMaxPacketSize0=64, idVendor=0xfffe, idProduct=0x0001,
                     bcdDevice=1, iManufacturer=0, iProduct=0,
                     iSerialNumber=0, bNumConfigurations=1,
                     bus=1, address=2, port_number=None,
                     port_numbers=None, speed=None)

    def get_configuration_descriptor(self, dev, config):
        return _Desc(bLength=9, bDescriptorType=2, wTotalLength=50,
                     bNumInterfaces=2, bConfigurationValue=1,
                     iConfiguration=0, bmAttributes=0xc0, bMaxPower=50)

    def get_interface_descriptor(self, dev, intf, alt, config):
        try:
            return self._iface_desc[(intf, alt)]
        except KeyError:
            raise IndexError((intf, alt))

    def get_endpoint_descriptor(self, dev, ep, intf, alt, config):
        try:
            return self._ep_desc[(ep, intf, alt)]
        except KeyError:
            raise IndexError((ep, intf, alt))

    # -- handle / interface management ----------------------------------

    def open_device(self, dev):
        self.open_count += 1
        return object()

    def close_device(self, handle):
        self.close_count += 1

    def set_configuration(self, handle, config_value):
        pass

    def get_configuration(self, handle):
        return 1

    def claim_interface(self, handle, intf):
        self.claims.append(intf)

    def release_interface(self, handle, intf):
        self.releases.append(intf)

    def set_interface_altsetting(self, handle, intf, altsetting):
        self.alt_calls.append((intf, altsetting, time.monotonic()))
        self.current_alt[intf] = altsetting

    def get_interface_altsetting(self, handle, intf):
        return self.current_alt[intf]

    # -- transfer gating -------------------------------------------------

    def pause(self, intf):
        self.driver_entered[intf].clear()
        self.gates[intf].clear()

    def resume(self, intf):
        self.gates[intf].set()

    def schedule_short_read(self, intf, length):
        self.short_bytes[intf] = length

    def _wait_gate(self, intf, timeout):
        if timeout is None:
            self.gates[intf].wait()
            return
        if not self.gates[intf].wait(timeout / 1000.0):
            raise USBTimeoutError('transfer timed out', None, None)

    def _do_read(self, kind, handle, ep, intf, buff, timeout):
        t_start = time.monotonic()
        self.driver_entered[intf].set()
        self._wait_gate(intf, timeout)
        with self._lock:
            seq = self._seq[intf]
            self._seq[intf] += 1
        alt = self.current_alt[intf]
        if intf == CALIB_INTF:
            prefix = 0x30
        elif alt == HIGH_GEAR:
            prefix = 0x20
        else:
            prefix = 0x10
        payload = bytes((prefix + (i % 8)) & 0xff
                        for i in range(len(buff)))
        length = self.short_bytes.pop(intf, len(buff))
        for i in range(length):
            buff[i] = payload[i]
        t_end = time.monotonic()
        self.transfers.append(dict(kind=kind, ep=ep, intf=intf,
                                   requested=len(buff), returned=length,
                                   data=bytes(bytearray(buff[:length])),
                                   alt=alt, t_start=t_start, t_end=t_end))
        return length

    def bulk_read(self, handle, ep, intf, buff, timeout):
        return self._do_read('bulk', handle, ep, intf, buff, timeout)

    def intr_read(self, handle, ep, intf, buff, timeout):
        return self._do_read('intr', handle, ep, intf, buff, timeout)


def make_device(backend=None):
    backend = backend or FakeBoardBackend()
    device = usb.core.find(backend=backend)
    device.set_configuration(1)
    return device, backend


class SessionTest(unittest.TestCase):

    def setUp(self):
        self.device, self.backend = make_device()
        self.session = session_mod.AcquisitionSession(self.device)
        self.measure = self.session.open_job(
            'measurement', MEASURE_INTF, MEASURE_EP, gear=LOW_GEAR)
        self.calib = self.session.open_job(
            'calibration', CALIB_INTF, CALIB_EP)

    def tearDown(self):
        self.session.close()

    # -- provenance ------------------------------------------------------

    def test_jobs_read_their_own_interface_and_endpoint(self):
        result = self.measure.read(8)
        self.assertEqual(result.interface, MEASURE_INTF)
        self.assertEqual(result.endpoint, MEASURE_EP)
        self.assertEqual(result.gear, LOW_GEAR)
        self.assertEqual(result.transfer_type,
                         usb.util.ENDPOINT_TYPE_BULK)

        cal = self.calib.read(16)
        self.assertEqual(cal.interface, CALIB_INTF)
        self.assertEqual(cal.endpoint, CALIB_EP)
        self.assertEqual(cal.transfer_type,
                         usb.util.ENDPOINT_TYPE_INTR)

        kinds = {(t['intf'], t['ep'], t['kind'])
                 for t in self.backend.transfers}
        self.assertIn((MEASURE_INTF, MEASURE_EP, 'bulk'), kinds)
        self.assertIn((CALIB_INTF, CALIB_EP, 'intr'), kinds)

    def test_samples_carry_the_gear_true_at_submission(self):
        low = self.measure.read(8)
        self.assertEqual(low.gear, LOW_GEAR)
        self.assertEqual(low.transfer_type,
                         usb.util.ENDPOINT_TYPE_BULK)
        self.assertEqual(low.length, 8)
        self.assertEqual(low.data[0] & 0xf0, 0x10)

        switch = self.session.switch_gear(MEASURE_INTF, HIGH_GEAR)
        self.assertTrue(switch.switched)

        high = self.measure.read(32)
        # Same endpoint address, but the descriptor (and therefore the
        # transfer type) of the high gear must be reported truthfully.
        self.assertEqual(high.endpoint, MEASURE_EP)
        self.assertEqual(high.gear, HIGH_GEAR)
        self.assertEqual(high.transfer_type,
                         usb.util.ENDPOINT_TYPE_INTR)
        self.assertEqual(high.length, 32)
        self.assertEqual(high.data[0] & 0xf0, 0x20)

        logged = {(t['intf'], t['ep'], t['kind'])
                  for t in self.backend.transfers}
        self.assertIn((MEASURE_INTF, MEASURE_EP, 'bulk'), logged)
        self.assertIn((MEASURE_INTF, MEASURE_EP, 'intr'), logged)

    def test_short_read_keeps_the_real_byte_count(self):
        self.backend.schedule_short_read(MEASURE_INTF, 3)
        result = self.measure.read(8)
        self.assertEqual(result.length, 3)
        self.assertEqual(len(result.data), 3)
        logged = self.backend.transfers[-1]
        self.assertEqual(logged['returned'], 3)
        self.assertEqual(bytes(result.data), logged['data'])

    # -- switch vs in-flight transfers -----------------------------------

    def test_switch_waits_for_started_transfer_then_uses_old_result(self):
        self.backend.pause(MEASURE_INTF)
        reader_result = []
        reader_error = []

        def reader():
            try:
                reader_result.append(
                    self.measure.read(8, timeout=10000))
            except BaseException as exc:
                reader_error.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(self.backend.driver_entered[MEASURE_INTF].wait(5))
        self.assertEqual(len(self.backend.transfers), 0)

        switcher = []
        switch_thread = threading.Thread(
            target=lambda: switcher.append(
                self.session.switch_gear(MEASURE_INTF, HIGH_GEAR,
                                         timeout=10000)))
        switch_thread.start()
        time.sleep(0.2)
        self.assertTrue(switch_thread.is_alive(),
                        'switch must wait for the in-flight transfer')
        self.assertEqual(self.backend.alt_calls, [],
                         'altsetting must not change mid-transfer')

        self.backend.resume(MEASURE_INTF)
        thread.join(5)
        switch_thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(switch_thread.is_alive())
        self.assertEqual(reader_error, [])

        old_sample = reader_result[0]
        self.assertEqual(old_sample.gear, LOW_GEAR)
        self.assertEqual(old_sample.transfer_type,
                         usb.util.ENDPOINT_TYPE_BULK)
        self.assertEqual(switcher[0].drained, 1)
        self.assertEqual(switcher[0].cancelled, 0)

        transfer_end = self.backend.transfers[0]['t_end']
        alt_time = self.backend.alt_calls[-1][2]
        self.assertGreaterEqual(alt_time, transfer_end)
        self.assertEqual(self.backend.alt_calls[-1][:2],
                         (MEASURE_INTF, HIGH_GEAR))

    def test_pending_request_that_never_reaches_driver_is_cancelled(self):
        pending_seen = threading.Event()
        release_hook = threading.Event()

        def hook(request):
            pending_seen.set()
            release_hook.wait(5)

        self.session.on_request_pending = hook
        reader_error = []

        def reader():
            try:
                self.measure.read(8, timeout=10000)
            except session_mod.ReadCancelled as exc:
                reader_error.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(pending_seen.wait(5))

        switch = self.session.switch_gear(MEASURE_INTF, HIGH_GEAR,
                                          timeout=5000)
        release_hook.set()
        thread.join(5)

        self.assertEqual(switch.cancelled, 1)
        self.assertEqual(switch.drained, 0)
        self.assertEqual(len(reader_error), 1)
        exc = reader_error[0]
        self.assertEqual(exc.gear, LOW_GEAR)
        self.assertEqual(exc.endpoint, MEASURE_EP)
        self.assertEqual(self.backend.transfers, [],
                         'cancelled request must not reach the driver')
        self.assertEqual(self.backend.alt_calls[-1][:2],
                         (MEASURE_INTF, HIGH_GEAR))

    def test_switch_timeout_keeps_old_gear_and_never_applies_later(self):
        self.backend.pause(MEASURE_INTF)
        reader_result = []

        def reader():
            reader_result.append(self.measure.read(8, timeout=10000))

        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(self.backend.driver_entered[MEASURE_INTF].wait(5))

        with self.assertRaises(session_mod.SwitchTimeout) as ctx:
            self.session.switch_gear(MEASURE_INTF, HIGH_GEAR, timeout=100)
        self.assertEqual(ctx.exception.verified_gear, LOW_GEAR)

        # Let the old transfer finish long after the switch gave up.
        self.backend.resume(MEASURE_INTF)
        thread.join(5)
        time.sleep(0.2)
        self.assertEqual(
            [call[:2] for call in self.backend.alt_calls], [],
            'a timed-out switch must not silently succeed afterwards')

        # Acquisition on the previous gear keeps working.
        again = self.measure.read(8)
        self.assertEqual(again.gear, LOW_GEAR)
        self.assertEqual(again.transfer_type,
                         usb.util.ENDPOINT_TYPE_BULK)

        # The state machine must accept a fresh, unblocked switch.
        ok = self.session.switch_gear(MEASURE_INTF, HIGH_GEAR,
                                      timeout=5000)
        self.assertTrue(ok.switched)
        self.assertEqual(self.session.get_gear(MEASURE_INTF), HIGH_GEAR)

    def test_verified_gear_reports_the_driver_truth(self):
        self.assertEqual(
            self.session.get_gear(MEASURE_INTF, verify=True), LOW_GEAR)
        self.session.switch_gear(MEASURE_INTF, HIGH_GEAR)
        self.assertEqual(
            self.session.get_gear(MEASURE_INTF, verify=True), HIGH_GEAR)
        self.assertEqual(
            self.backend.get_interface_altsetting(
                self.device._ctx.handle, MEASURE_INTF), HIGH_GEAR)

    # -- unrelated traffic and lifecycle ---------------------------------

    def test_calibration_completes_while_measurement_switch_waits(self):
        self.backend.pause(MEASURE_INTF)
        blocked = []

        def blocked_reader():
            blocked.append(self.measure.read(8, timeout=10000))

        thread = threading.Thread(target=blocked_reader)
        thread.start()
        self.assertTrue(self.backend.driver_entered[MEASURE_INTF].wait(5))

        switching = threading.Event()

        def do_switch():
            switching.set()
            self.session.switch_gear(MEASURE_INTF, HIGH_GEAR,
                                     timeout=10000)

        switch_thread = threading.Thread(target=do_switch)
        switch_thread.start()
        self.assertTrue(switching.wait(5))
        time.sleep(0.1)

        cal = self.calib.read(16)
        self.assertEqual(cal.interface, CALIB_INTF)
        self.assertEqual(cal.endpoint, CALIB_EP)

        self.backend.resume(MEASURE_INTF)
        thread.join(5)
        switch_thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(switch_thread.is_alive())
        self.assertEqual(blocked[0].gear, LOW_GEAR)

    def test_closing_measurement_keeps_calibration_and_shared_handle(self):
        self.assertEqual(self.backend.close_count, 0)
        self.measure.close()
        self.assertEqual(self.backend.close_count, 0,
                         'handle stays open while a job remains')
        cal = self.calib.read(16)
        self.assertEqual(cal.interface, CALIB_INTF)
        self.calib.close()
        self.assertEqual(self.backend.close_count, 1,
                         'handle closes only after the last user exits')

    def test_context_manager_closes_every_job(self):
        backend = FakeBoardBackend()
        device, _ = make_device(backend)
        with session_mod.AcquisitionSession(device) as session:
            session.open_job('measurement', MEASURE_INTF, MEASURE_EP,
                             gear=LOW_GEAR)
            session.open_job('calibration', CALIB_INTF, CALIB_EP)
        self.assertEqual(backend.close_count, 1)

    def test_pause_and_resume_a_job(self):
        self.measure.pause()
        with self.assertRaises(USBError):
            self.measure.read(8)
        self.measure.resume()
        result = self.measure.read(8)
        self.assertEqual(result.length, 8)

    def test_close_waits_for_inflight_transfer_before_releasing_handle(self):
        backend = FakeBoardBackend()
        device, _ = make_device(backend)
        session = session_mod.AcquisitionSession(device)
        job = session.open_job('measurement', MEASURE_INTF, MEASURE_EP,
                               gear=LOW_GEAR)
        backend.pause(MEASURE_INTF)
        sample = []

        def reader():
            sample.append(job.read(8, timeout=10000))

        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(backend.driver_entered[MEASURE_INTF].wait(5))

        closed = threading.Event()

        def closer():
            session.close()
            closed.set()

        close_thread = threading.Thread(target=closer)
        close_thread.start()
        time.sleep(0.2)
        self.assertFalse(closed.is_set(),
                         'the last close must wait for the driver transfer')
        self.assertEqual(backend.close_count, 0)

        backend.resume(MEASURE_INTF)
        thread.join(5)
        close_thread.join(5)
        self.assertTrue(closed.is_set())
        self.assertEqual(sample[0].gear, LOW_GEAR)
        self.assertEqual(backend.close_count, 1)

    def test_double_switch_is_rejected(self):
        self.backend.pause(MEASURE_INTF)

        def reader():
            self.measure.read(8, timeout=10000)

        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(self.backend.driver_entered[MEASURE_INTF].wait(5))

        first = threading.Event()

        def first_switch():
            first.set()
            self.session.switch_gear(MEASURE_INTF, HIGH_GEAR,
                                     timeout=10000)

        switch_thread = threading.Thread(target=first_switch)
        switch_thread.start()
        self.assertTrue(first.wait(5))
        time.sleep(0.1)
        with self.assertRaises(USBError):
            self.session.switch_gear(MEASURE_INTF, HIGH_GEAR, timeout=100)

        self.backend.resume(MEASURE_INTF)
        thread.join(5)
        switch_thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(switch_thread.is_alive())


class PlainDeviceBehaviorTest(unittest.TestCase):

    def test_device_read_without_session_is_unchanged(self):
        backend = FakeBoardBackend()
        device, _ = make_device(backend)
        device._ctx.managed_claim_interface(device, MEASURE_INTF)

        data = device.read(MEASURE_EP, 8)
        self.assertEqual(len(data), 8)
        self.assertEqual(backend.transfers[0]['kind'], 'bulk')

        backend.schedule_short_read(MEASURE_INTF, 2)
        short = device.read(MEASURE_EP, 8)
        self.assertEqual(len(short), 2)

        buffer = usb.util.create_buffer(8)
        count = device.read(MEASURE_EP, buffer)
        self.assertEqual(count, 8)
        usb.util.dispose_resources(device)
        self.assertEqual(backend.close_count, 1)


def get_suite():
    loader = unittest.TestLoader()
    return unittest.TestSuite([
        loader.loadTestsFromTestCase(SessionTest),
        loader.loadTestsFromTestCase(PlainDeviceBehaviorTest),
    ])


if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(get_suite())
