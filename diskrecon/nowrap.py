"""Mirror of psutil's ``nowrap`` adjustment, for field cross-checks.

psutil's :func:`psutil._common.wrap_numbers` watches a counter series
keyed by disk name; whenever a value drops below the previous one it
adds the previous value to a per-key reminder, so the exported series
never decreases.  It compensates *once per observed decrease under the
same name* — it cannot tell a counter wrap from a same-name disk swap,
and it cannot see turnovers that happened between two reads.

:class:`NowrapTracker` reproduces that exact behaviour for a single
counter series so field samples can be checked against real psutil
returns, and so the fake-reader reproduction can run the genuine
psutil code path.
"""

from __future__ import annotations


class NowrapTracker:
    """Single-counter equivalent of ``psutil._common._WrapNumbers``."""

    def __init__(self):
        self._prev = None
        self._reminder = 0

    def update(self, raw_value: int) -> int:
        """Feed a raw reading, get the nowrap-adjusted value back."""
        if self._prev is None:
            self._prev = raw_value
            return raw_value
        if raw_value < self._prev:
            # psutil treats any decrease as a wrap and compensates once
            # with the previous (old) value.
            self._reminder += self._prev
        self._prev = raw_value
        return raw_value + self._reminder
