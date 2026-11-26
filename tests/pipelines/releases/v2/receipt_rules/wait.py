"""Step that waits in-process until the batch gate file exists."""
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def run_step(context):
    """Wait for context['gateFile'] to exist."""
    context.assert_key_has_value('gateFile', __name__)
    gate_file = Path(context.get_formatted('gateFile'))

    logger.info("waiting for gate file %s", gate_file)
    while not gate_file.is_file():
        time.sleep(0.01)

    logger.info("gate file ready: %s", gate_file)
