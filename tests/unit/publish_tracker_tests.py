"""Tests for pika.publish_tracker."""

import threading
import unittest
from unittest import mock

from pika import frame, spec
from pika.publish_tracker import (
    DEFAULT_MESSAGE_ID_HEADER,
    Publication,
    PublishOutcome,
    PublishTracker,
    ReturnedMessage,
    TrackerSnapshot,
    message_id_of,
    with_message_id,
)


class FakeChannel:
    """Stand-in for the thread-safe connection's Channel.

    Captures the callbacks `arm` registers and the `on_publish` closure each
    publish passes, so a test can fire the frames the broker would send in
    whatever order it likes.
    """

    def __init__(self):
        self.confirm_callback = None
        self.return_callback = None
        self.publishes = []

    def confirm_delivery(self, ack_nack_callback):
        self.confirm_callback = ack_nack_callback

    def add_on_return_callback(self, callback):
        self.return_callback = callback

    def basic_publish(self,
                      exchange,
                      routing_key,
                      body,
                      properties=None,
                      mandatory=True,
                      on_publish=None):
        self.publishes.append(
            (exchange, routing_key, body, properties, mandatory, on_publish))


def ack_frame(tag, multiple=False):
    return frame.Method(1, spec.Basic.Ack(tag, multiple))


def nack_frame(tag, multiple=False):
    return frame.Method(1, spec.Basic.Nack(tag, multiple))


def return_frame(message_id, body, header_name=DEFAULT_MESSAGE_ID_HEADER):
    properties = spec.BasicProperties(
        headers={header_name: message_id}, content_type='text/plain')
    method = spec.Basic.Return(312, 'NO_ROUTE', '', 'routing.key')
    return method, properties, body


class WithMessageIdTests(unittest.TestCase):

    def test_stamps_id_into_new_properties_without_mutating_source(self):
        source = spec.BasicProperties(content_type='text/plain',
                                      headers={'existing': 'value'})
        stamped = with_message_id(source, 'id-1')

        self.assertEqual(stamped.headers[DEFAULT_MESSAGE_ID_HEADER], 'id-1')
        self.assertEqual(stamped.headers['existing'], 'value')
        self.assertEqual(stamped.content_type, 'text/plain')
        self.assertNotIn(DEFAULT_MESSAGE_ID_HEADER, source.headers)

    def test_accepts_none_properties(self):
        stamped = with_message_id(None, 'id-1')
        self.assertEqual(stamped.headers,
                         {DEFAULT_MESSAGE_ID_HEADER: 'id-1'})

    def test_preserves_every_field(self):
        source = spec.BasicProperties(
            content_type='text/plain',
            content_encoding='utf-8',
            headers={'a': 1},
            delivery_mode=2,
            priority=5,
            correlation_id='corr',
            reply_to='me',
            expiration='1000',
            message_id='amqp-message-id',
            timestamp=123,
            type='event',
            user_id='user',
            app_id='app',
            cluster_id='cluster')
        stamped = with_message_id(source, 'id-1')
        for name in (
                'content_type',
                'content_encoding',
                'delivery_mode',
                'priority',
                'correlation_id',
                'reply_to',
                'expiration',
                'message_id',
                'timestamp',
                'type',
                'user_id',
                'app_id',
                'cluster_id',
        ):
            self.assertEqual(getattr(stamped, name), getattr(source, name), name)
        self.assertEqual(stamped.headers['a'], 1)

    def test_rejects_reserved_header_already_set(self):
        source = spec.BasicProperties(
            headers={DEFAULT_MESSAGE_ID_HEADER: 'foreign'})
        with self.assertRaises(ValueError):
            with_message_id(source, 'id-1')

    def test_custom_header_name(self):
        stamped = with_message_id(None, 'id-1', header_name='x-correlation')
        self.assertEqual(stamped.headers['x-correlation'], 'id-1')
        self.assertEqual(
            message_id_of(stamped, header_name='x-correlation'), 'id-1')

    def test_message_id_of_missing_or_non_string(self):
        self.assertIsNone(message_id_of(spec.BasicProperties()))
        self.assertIsNone(
            message_id_of(spec.BasicProperties(headers={
                DEFAULT_MESSAGE_ID_HEADER: 7
            })))


class PublishTrackerTests(unittest.TestCase):

    def setUp(self):
        self.channel = FakeChannel()
        self.tracker = PublishTracker()
        self.tracker.arm(self.channel)

    def _publish(self, body=b'same body', message_id=None):
        return self.tracker.publish(self.channel,
                                    'exchange',
                                    'routing.key',
                                    body,
                                    message_id=message_id)

    def _fire_on_publish(self, index, delivery_tag):
        on_publish = self.channel.publishes[index][5]
        on_publish(delivery_tag)

    def _fire_return(self, message_id, body=b'same body'):
        method, properties, returned_body = return_frame(message_id, body)
        self.channel.return_callback(self.channel, method, properties,
                                     returned_body)

    def test_arm_registers_confirm_and_return_listeners(self):
        # Bound methods are new objects on each attribute access, so compare
        # the underlying functions.
        self.assertIs(self.channel.confirm_callback.__func__,
                      PublishTracker.on_confirm)
        self.assertIs(self.channel.return_callback.__func__,
                      PublishTracker.on_return)

    def test_publish_stamps_unique_ids_on_identical_bodies(self):
        first = self._publish(message_id='id-1')
        second = self._publish(message_id='id-2')

        self.assertEqual(first.message_id, 'id-1')
        self.assertEqual(second.message_id, 'id-2')
        first_properties = self.channel.publishes[0][3]
        second_properties = self.channel.publishes[1][3]
        self.assertIsNot(first_properties, second_properties)
        self.assertEqual(first_properties.headers[DEFAULT_MESSAGE_ID_HEADER],
                         'id-1')
        self.assertEqual(second_properties.headers[DEFAULT_MESSAGE_ID_HEADER],
                         'id-2')
        # The bodies are identical; only the stamped header distinguishes them.
        self.assertEqual(self.channel.publishes[0][2],
                         self.channel.publishes[1][2])

    def test_generates_id_when_none_given(self):
        record = self._publish()
        self.assertTrue(record.message_id)

    def test_duplicate_message_id_rejected(self):
        self._publish(message_id='id-1')
        with self.assertRaises(ValueError):
            self._publish(message_id='id-1')

    def test_publish_failure_discards_registration(self):
        channel = mock.Mock()
        channel.basic_publish.side_effect = RuntimeError('closed')
        with self.assertRaises(RuntimeError):
            self.tracker.publish(channel, '', '', b'x', message_id='id-x')
        snapshot = self.tracker.snapshot()
        self.assertEqual(snapshot.resolved, ())
        self.assertEqual(snapshot.pending, ())

    def test_on_publish_binds_delivery_tag_and_records_boundary(self):
        record = self._publish(message_id='id-1')
        self.assertIsNone(record.delivery_tag)
        self._fire_on_publish(0, 1)
        self.assertEqual(record.delivery_tag, 1)
        self.assertEqual(self.tracker.last_delivery_tag, 1)
        self.assertIs(self.tracker.get('id-1'), record)

    def test_return_then_single_ack_covering_both_publishes(self):
        # The acceptance scenario: identical bodies, the second is returned
        # before any confirm, then one Ack with multiple=True covers both tags.
        first = self._publish(message_id='id-1')
        second = self._publish(message_id='id-2')
        self._fire_on_publish(0, 1)
        self._fire_on_publish(1, 2)

        self._fire_return('id-2')
        self.assertEqual(second.outcome, PublishOutcome.Returned)
        self.assertIsInstance(second.returned, ReturnedMessage)
        self.assertEqual(first.outcome, PublishOutcome.Pending)

        self.channel.confirm_callback(ack_frame(2, multiple=True))

        self.assertEqual(first.outcome, PublishOutcome.Acked)
        self.assertIsInstance(first.confirm_method, spec.Basic.Ack)
        # The return stays the routing verdict; the ack is still recorded.
        self.assertEqual(second.outcome, PublishOutcome.Returned)
        self.assertIsInstance(second.confirm_method, spec.Basic.Ack)
        self.assertEqual(self.tracker.last_confirm_tag, 2)

    def test_single_ack_resolves_only_named_tag(self):
        first = self._publish(message_id='id-1')
        second = self._publish(message_id='id-2')
        self._fire_on_publish(0, 1)
        self._fire_on_publish(1, 2)

        self.channel.confirm_callback(ack_frame(1))

        self.assertEqual(first.outcome, PublishOutcome.Acked)
        self.assertEqual(second.outcome, PublishOutcome.Pending)
        self.assertEqual(self.tracker.last_confirm_tag, 1)

    def test_nack_marks_nacked_and_multiple_covers_earlier(self):
        first = self._publish(message_id='id-1')
        second = self._publish(message_id='id-2')
        self._fire_on_publish(0, 1)
        self._fire_on_publish(1, 2)

        self.channel.confirm_callback(nack_frame(2, multiple=True))

        self.assertEqual(first.outcome, PublishOutcome.Nacked)
        self.assertEqual(second.outcome, PublishOutcome.Nacked)
        self.assertIsInstance(first.confirm_method, spec.Basic.Nack)

    def test_confirm_for_unknown_tag_is_ignored(self):
        self._publish(message_id='id-1')
        # No on_publish fired, so nothing is bound to tag 9.
        self.channel.confirm_callback(ack_frame(9))
        self.assertEqual(self.tracker.get('id-1').outcome,
                         PublishOutcome.Pending)
        self.assertEqual(self.tracker.last_confirm_tag, 9)

    def test_unmatched_return_with_unknown_id_is_retained(self):
        self._fire_return('never-published')
        snapshot = self.tracker.snapshot()
        self.assertEqual(len(snapshot.unmatched_returns), 1)
        self.assertEqual(
            snapshot.unmatched_returns[0].method.reply_code, 312)

    def test_return_without_id_header_is_retained(self):
        method = spec.Basic.Return(312, 'NO_ROUTE', '', 'routing.key')
        properties = spec.BasicProperties()
        self.channel.return_callback(self.channel, method, properties, b'x')
        snapshot = self.tracker.snapshot()
        self.assertEqual(len(snapshot.unmatched_returns), 1)

    def test_wait_returns_when_confirm_arrives_on_another_thread(self):
        record = self._publish(message_id='id-1')
        self._fire_on_publish(0, 1)

        def confirm_later():
            self.channel.confirm_callback(ack_frame(1))

        thread = threading.Thread(target=confirm_later)
        thread.start()
        resolved = self.tracker.wait('id-1', timeout=5)
        thread.join()
        self.assertIs(resolved, record)
        self.assertEqual(resolved.outcome, PublishOutcome.Acked)

    def test_wait_times_out_while_still_pending(self):
        self._publish(message_id='id-1')
        record = self.tracker.wait('id-1', timeout=0.01)
        self.assertEqual(record.outcome, PublishOutcome.Pending)

    def test_wait_unknown_id_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.tracker.wait('missing')

    def test_wait_for_delivery_tag_blocks_until_on_publish(self):
        record = self._publish(message_id='id-1')
        result = []

        def bind_later():
            result.append(
                self.tracker.wait_for_delivery_tag('id-1', timeout=5))

        thread = threading.Thread(target=bind_later)
        thread.start()
        self._fire_on_publish(0, 7)
        thread.join()
        self.assertEqual(result, [7])
        self.assertEqual(record.delivery_tag, 7)

    def test_wait_for_delivery_tag_times_out(self):
        self._publish(message_id='id-1')
        with self.assertRaises(TimeoutError):
            self.tracker.wait_for_delivery_tag('id-1', timeout=0.01)

    def test_wait_for_delivery_tag_unknown_id_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.tracker.wait_for_delivery_tag('missing')

    def test_wait_all_reports_timeout_then_completion(self):
        self._publish(message_id='id-1')
        self._publish(message_id='id-2')
        self._fire_on_publish(0, 1)
        self._fire_on_publish(1, 2)

        self.assertFalse(self.tracker.wait_all(timeout=0.01))

        self.channel.confirm_callback(ack_frame(2, multiple=True))
        self.assertTrue(self.tracker.wait_all(timeout=0.01))

    def test_mark_disconnected_turns_pending_into_uncertain_and_wakes(self):
        first = self._publish(message_id='id-1')
        second = self._publish(message_id='id-2')
        self._fire_on_publish(0, 1)
        self._fire_on_publish(1, 2)
        self.channel.confirm_callback(ack_frame(1))

        self.tracker.mark_disconnected()

        self.assertEqual(first.outcome, PublishOutcome.Acked)
        self.assertEqual(second.outcome, PublishOutcome.Uncertain)
        # Uncertain is terminal for waiting purposes; the wait must not block.
        self.assertTrue(self.tracker.wait_all(timeout=0.01))

    def test_snapshot_reports_known_boundary_on_incomplete_disconnect(self):
        first = self._publish(message_id='id-1')
        second = self._publish(message_id='id-2')
        third = self._publish(message_id='id-3')
        self._fire_on_publish(0, 1)
        self._fire_on_publish(1, 2)
        self._fire_on_publish(2, 3)

        # Only the second publication's return frame made it back; its ack and
        # every other confirm are still missing when the connection drops.
        self._fire_return('id-2')
        self.tracker.mark_disconnected()

        snapshot = self.tracker.snapshot()
        self.assertIsInstance(snapshot, TrackerSnapshot)
        self.assertEqual(snapshot.last_delivery_tag, 3)
        self.assertIsNone(snapshot.last_confirm_tag)
        self.assertEqual([r.message_id for r in snapshot.resolved], ['id-2'])
        self.assertEqual(snapshot.pending, ())
        self.assertEqual(
            sorted(r.message_id for r in snapshot.uncertain),
            ['id-1', 'id-3'])
        self.assertEqual(second.outcome, PublishOutcome.Returned)
        self.assertEqual(first.outcome, PublishOutcome.Uncertain)
        self.assertEqual(third.outcome, PublishOutcome.Uncertain)

    def test_late_return_after_disconnect_still_matches_old_tracker(self):
        record = self._publish(message_id='id-1')
        self._fire_on_publish(0, 1)
        self.tracker.mark_disconnected()
        self.assertEqual(record.outcome, PublishOutcome.Uncertain)

        # A frame already in flight when the channel dropped is dispatched
        # afterwards; the old tracker still reconciles it by message id.
        self._fire_return('id-1')
        self.assertEqual(record.outcome, PublishOutcome.Returned)
        self.assertIsNotNone(record.returned)

    def test_late_ack_after_disconnect_upgrades_uncertain_publication(self):
        record = self._publish(message_id='id-1')
        self._fire_on_publish(0, 1)
        self.tracker.mark_disconnected()
        self.assertEqual(record.outcome, PublishOutcome.Uncertain)

        self.channel.confirm_callback(ack_frame(1))

        self.assertEqual(record.outcome, PublishOutcome.Acked)
        self.assertIsInstance(record.confirm_method, spec.Basic.Ack)
        self.assertEqual(self.tracker.last_confirm_tag, 1)

    def test_return_arriving_before_delivery_tag_is_still_matched_by_id(self):
        # The return needs no tag, so an on_publish callback that has not run
        # yet does not block correlation by message id.
        record = self._publish(message_id='id-1')
        self._fire_return('id-1')
        self.assertEqual(record.outcome, PublishOutcome.Returned)
        self.assertIsNone(record.delivery_tag)

        self._fire_on_publish(0, 1)
        self.channel.confirm_callback(ack_frame(1))
        # The return stays primary even when the confirm lands afterwards.
        self.assertEqual(record.outcome, PublishOutcome.Returned)
        self.assertIsNotNone(record.confirm_method)


class ReopenedChannelTests(unittest.TestCase):
    """Reopening the same channel number restarts broker tags at 1."""

    def test_new_tracker_keeps_generations_apart(self):
        old_channel = FakeChannel()
        old_tracker = PublishTracker()
        old_tracker.arm(old_channel)

        old_first = old_tracker.publish(old_channel,
                                        'exchange',
                                        'rk',
                                        b'same body',
                                        message_id='old-1')
        old_second = old_tracker.publish(old_channel,
                                         'exchange',
                                         'rk',
                                         b'same body',
                                         message_id='old-2')
        old_channel.publishes[0][5](1)
        old_channel.publishes[1][5](2)

        # The old channel is closed before its callbacks finish draining.
        old_tracker.mark_disconnected()

        # A fresh channel reuses channel number 1; a fresh tracker starts at
        # tag 1 again, which would collide inside one shared tracker.
        new_channel = FakeChannel()
        new_tracker = PublishTracker()
        new_tracker.arm(new_channel)
        new_record = new_tracker.publish(new_channel,
                                         'exchange',
                                         'rk',
                                         b'same body',
                                         message_id='new-1')
        new_channel.publishes[0][5](1)

        self.assertEqual(new_record.delivery_tag, 1)
        self.assertEqual(new_record.outcome, PublishOutcome.Pending)

        # Late frames from the old channel land on the old tracker only.
        old_channel.confirm_callback(ack_frame(1))
        old_channel.return_callback(old_channel,
                                    *return_frame('old-2', b'same body'))
        old_channel.confirm_callback(ack_frame(2, multiple=True))

        self.assertEqual(old_first.outcome, PublishOutcome.Acked)
        self.assertEqual(old_second.outcome, PublishOutcome.Returned)
        self.assertIsNotNone(old_second.confirm_method)

        # The new publication waits for evidence carrying its own id/tag.
        new_snapshot = new_tracker.snapshot()
        self.assertEqual([r.message_id for r in new_snapshot.pending],
                         ['new-1'])
        new_channel.confirm_callback(ack_frame(1))
        self.assertEqual(new_record.outcome, PublishOutcome.Acked)

    def test_old_tracker_keeps_uncertain_state_after_reopen(self):
        old_channel = FakeChannel()
        old_tracker = PublishTracker()
        old_tracker.arm(old_channel)
        record = old_tracker.publish(old_channel,
                                     'exchange',
                                     'rk',
                                     b'body',
                                     message_id='old-1')
        old_channel.publishes[0][5](1)
        old_tracker.mark_disconnected()

        snapshot = old_tracker.snapshot()
        self.assertEqual(snapshot.last_delivery_tag, 1)
        self.assertIsNone(snapshot.last_confirm_tag)
        self.assertEqual(
            [r.message_id for r in snapshot.uncertain], ['old-1'])
        self.assertIsInstance(record, Publication)


if __name__ == '__main__':
    unittest.main()
