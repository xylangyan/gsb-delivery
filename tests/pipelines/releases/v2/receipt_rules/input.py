"""Context parser that reads a batch id for the v2 receipt release."""
import logging

from receipt_rules.tag import RULE_TAG

logger = logging.getLogger(__name__)


def get_parsed_context(args):
    """Parse batch id and initialize the batch result context."""
    if not args:
        raise ValueError("receipt_rules.input requires a batch id.")

    batch_id = args[0]
    gate_file = args[1] if len(args) > 1 else f'{batch_id}.ready'

    return {
        'batchId': batch_id,
        'gateFile': gate_file,
        'parseResult': RULE_TAG,
        'receipts': []
    }
