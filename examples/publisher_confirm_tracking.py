"""Example: per-publication confirm and return tracking with Connection.

A Basic.Return frame carries no delivery tag, and one Basic.Ack with
multiple=True can confirm several publishes at once, so delivery tags alone
cannot tell two identical mandatory messages apart. pika.publish_tracker
stamps each publish with a unique message id in its headers, then reconciles
the three broker signals per publication:

* on_publish hands over the delivery tag the broker will confirm;
* a Basic.Return is matched by the stamped message id;
* a Basic.Ack / Basic.Nack is matched by delivery tag, covering a multiple
  frame for every earlier tag.

The scenario mirrors a recovery question: two publishes with identical bodies,
the second returned before either is confirmed, then one Ack covering both;
a channel reopened on the same channel number while the old channel's
callbacks may still be draining; and a connection lost while evidence is
incomplete, where the tracker has to hand back what it knew and where its
visibility ended rather than block forever.

Pairs with examples/basic_consumer_threaded.py if you want to consume the
routable messages afterwards.
"""

import logging

import pika
from pika import spec
from pika.exchange_type import ExchangeType
from pika.publish_tracker import PublishOutcome, PublishTracker

LOG_FORMAT = ('%(levelname) -10s %(asctime)s %(name) -30s %(funcName) '
              '-35s %(lineno) -5d: %(message)s')
LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

EXCHANGE_NAME = 'tracker_exchange'
QUEUE_NAME = 'tracker_queue'
ROUTABLE_KEY = 'tracker.routable'
UNROUTABLE_KEY = 'tracker.nowhere'
BODY = b'identical body'


def describe(record):
    text = (f'id={record.message_id} tag={record.delivery_tag} '
            f'outcome={record.outcome.name}')
    if record.returned is not None:
        text += (f' return={record.returned.method.reply_code} '
                 f'{record.returned.method.reply_text!r}')
    if record.confirm_method is not None:
        kind = ('Ack' if isinstance(record.confirm_method, spec.Basic.Ack)
                else 'Nack')
        text += f' confirm={kind}(multiple={record.confirm_method.multiple})'
    return text


def main():
    parameters = pika.ConnectionParameters(
        credentials=pika.PlainCredentials('guest', 'guest'))
    # Every channel generation gets its own tracker; a connection loss marks
    # each one's pending publications uncertain. The callback runs on the
    # IOLoop thread and only touches the trackers' conditions, so it is safe
    # there.
    trackers = []

    def on_connection_closed(_connection, _reason):
        for tracker in trackers:
            tracker.mark_disconnected()

    connection = pika.Connection(parameters,
                                 on_close_callback=on_connection_closed)

    channel = connection.channel()
    channel.exchange_declare(exchange=EXCHANGE_NAME,
                             exchange_type=ExchangeType.direct,
                             durable=True)
    channel.queue_declare(queue=QUEUE_NAME, durable=True)
    channel.queue_bind(queue=QUEUE_NAME,
                       exchange=EXCHANGE_NAME,
                       routing_key=ROUTABLE_KEY)

    tracker = PublishTracker()
    trackers.append(tracker)
    tracker.arm(channel)

    LOGGER.info('Publishing two identical bodies: routable then unroutable')
    first = tracker.publish(channel,
                            EXCHANGE_NAME,
                            ROUTABLE_KEY,
                            BODY,
                            message_id='batch-1-msg-1')
    second = tracker.publish(channel,
                             EXCHANGE_NAME,
                             UNROUTABLE_KEY,
                             BODY,
                             message_id='batch-1-msg-2')

    # The broker returns the unroutable publish and only then confirms it, so
    # the return is observable while the first publish is still waiting.
    tracker.wait('batch-1-msg-2')
    LOGGER.info('After the return, before draining confirms: %s',
                describe(first))
    LOGGER.info('After the return, before draining confirms: %s',
                describe(second))

    tracker.wait_all(timeout=30)
    LOGGER.info('Final verdicts: %s', describe(first))
    LOGGER.info('Final verdicts: %s', describe(second))
    assert first.outcome is PublishOutcome.Acked
    assert second.outcome is PublishOutcome.Returned
    assert second.confirm_method is not None

    # Reopen on the same channel number. The broker restarts delivery tags at
    # 1 on the new channel, so it gets a fresh tracker while the old one keeps
    # its generation of records and any callback still queued on the worker
    # reconciles against the tracker it was bound to.
    old_number = channel.channel_number
    old_tracker = tracker
    channel.close()
    channel = connection.channel()
    assert channel.channel_number == old_number
    LOGGER.info('Reopened channel number %s; old tracker still holds %d '
                'resolved publications', old_number,
                len(old_tracker.snapshot().resolved))

    tracker = PublishTracker()
    trackers.append(tracker)
    tracker.arm(channel)
    new_record = tracker.publish(channel,
                                 EXCHANGE_NAME,
                                 ROUTABLE_KEY,
                                 BODY,
                                 message_id='batch-2-msg-1')
    # on_publish runs later on the IOLoop thread, so wait for the tag instead
    # of reading it immediately after publish returns.
    new_tag = tracker.wait_for_delivery_tag('batch-2-msg-1', timeout=30)
    LOGGER.info('New channel assigned delivery tag %s to the new publish',
                new_tag)
    assert new_tag == 1
    tracker.wait_all(timeout=30)
    LOGGER.info('New publication verdict: %s', describe(new_record))
    assert new_record.outcome is PublishOutcome.Acked

    # Lose the connection without waiting for these two publications. The
    # close callback marks every pending one uncertain and wakes any waiter,
    # so the snapshot hands back both the per-publication states and the tag
    # boundary visibility ended at. A graceful abort drains queued callbacks
    # first, so on a healthy local broker the confirms may already be
    # resolved; either ordering is valid, and the snapshot reports which one
    # happened. The deterministic incomplete-return case is pinned in
    # tests/unit/publish_tracker_tests.py, where frames are fed by hand.
    tracker.publish(channel,
                    EXCHANGE_NAME,
                    ROUTABLE_KEY,
                    BODY,
                    message_id='batch-3-msg-1')
    tracker.publish(channel,
                    EXCHANGE_NAME,
                    UNROUTABLE_KEY,
                    BODY,
                    message_id='batch-3-msg-2')
    connection.abort()

    snapshot = tracker.snapshot()
    LOGGER.info(
        'On disconnect: last_delivery_tag=%s last_confirm_tag=%s '
        'resolved=%d pending=%d uncertain=%d unmatched_returns=%d',
        snapshot.last_delivery_tag, snapshot.last_confirm_tag,
        len(snapshot.resolved), len(snapshot.pending),
        len(snapshot.uncertain), len(snapshot.unmatched_returns))
    assert snapshot.last_delivery_tag is not None
    assert not snapshot.pending
    for record in snapshot.resolved:
        LOGGER.info('Resolved before the loss: %s', describe(record))
    for record in snapshot.uncertain:
        LOGGER.info('No broker verdict observed, handed back as uncertain: '
                    '%s', describe(record))
    for returned in snapshot.unmatched_returns:
        LOGGER.info('Return with no tracked publication: reply_code=%s body=%r',
                    returned.method.reply_code, returned.body)
    LOGGER.info('Done')


if __name__ == '__main__':
    main()
