"""Column-major Fortran iterator of indices across multipe dimensions.

:copyright: Copyright 2015 Marshall Ward, see AUTHORS for details.
:license: Apache License, Version 2.0, see LICENSE for details.
"""


class FIndex(object):
    """Column-major multidimensional index iterator."""

    def __init__(self, bounds, first=None, default_start_index=1):
        """Initialise the index iterator."""
        self.start = [default_start_index if b[0] is None else b[0]
                      for b in bounds]
        self.end = [b[1] for b in bounds]
        self.step = [1 if b[2] is None else b[2] for b in bounds]

        self.current = self.start[:]
        self.done = any(
            e is not None and e <= s
            for s, e in zip(self.start, self.end)
        )

        # Default global starting index
        if first is not None:
            self.first = [min(first, s) for s in self.start]
        else:
            self.first = [b[0] for b in bounds]

    def __str__(self):
        return "{0}(start={1}, end={2}, step={3}, current={4}, first={5})".format(
            type(self).__name__,
            self.start,
            self.end,
            self.step,
            self.current,
            self.first,
        )

    def __iter__(self):
        """Declare object as iterator."""
        return self

    def next(self):
        """Python 2 interface to Python 3 iterator."""
        return self.__next__()

    def __next__(self):
        """Iterate to next contiguous index tuple."""
        if self.done:
            raise StopIteration

        state = self.current[:]

        # Advance the fastest-varying index first (Fortran column-major
        # order), respecting each dimension's stride.  A dimension with an
        # explicit end is exhausted once its next index would reach or pass
        # that end, at which point we carry into the next dimension.
        for rank in range(len(self.current)):
            if self.end[rank] is None:
                nxt = state[rank] + self.step[rank]
            else:
                nxt = state[rank] + self.step[rank]
                if nxt >= self.end[rank]:
                    self.current[rank] = self.start[rank]
                    continue

            self.current[rank] = nxt
            break
        else:
            # Every dimension was exhausted; this was the final element.
            self.done = True

        return state
