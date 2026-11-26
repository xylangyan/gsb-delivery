"""Step that appends a receipt for the current batch."""
import logging

from receipt_rules.tag import RULE_TAG

logger = logging.getLogger(__name__)


def run_step(context):
    """Append rule tag, source and batch id to the batch receipts."""
    context.assert_key_has_value('receiptSource', __name__)
    context.assert_key_has_value('batchId', __name__)

    source = context.get_formatted('receiptSource')
    batch_id = context.get_formatted('batchId')
    receipt = f'{RULE_TAG}|{source}|{batch_id}'

    context.setdefault('receipts', []).append(receipt)
    logger.info("receipt appended: %s", receipt)
