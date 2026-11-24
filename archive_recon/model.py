"""Exact joint feasibility model for reconstructing a 1-second series.

One variable exists per original second.  Every legacy and target cell is
translated into constraints on that same set of seconds, so evidence from two
archives only combines when a single original series satisfies both.  Cell
presence is modelled explicitly because Whisper omits a cell when fewer than
``xFilesFactor`` of its children are known; a value of zero is an ordinary
present reading and is never used as a missing marker.

Supported aggregation rules are ``average`` and ``sum`` -- the two rules for
which a cell value is a linear function of its children.  Other Whisper rules
(``max``, ``min``, ``last``, ...) are rejected rather than approximated.
"""

import math

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

SUPPORTED_AGGREGATIONS = ("average", "sum")


class Infeasible(Exception):
    """The constraints cannot hold for any original series."""


def presence_threshold(xff, count):
    """Smallest number of known children at which Whisper stores a cell.

    ``__propagate`` writes when ``knownPercent >= xff`` with
    ``knownPercent = known / count``, so the threshold is ``ceil(xff*count)``.
    A cell is never written with zero known values, so the result is at least 1.
    """
    return max(1, int(math.ceil(float(xff) * count - 1e-12)))


class Model:
    def __init__(self):
        self.names = []
        self.index = {}
        self.lower = []
        self.upper = []
        self.integer = []
        self.rows = []

    def var(self, name, low, high, integer=False):
        if name in self.index:
            return name
        ident = len(self.names)
        self.names.append(name)
        self.index[name] = ident
        self.lower.append(float(low))
        self.upper.append(float(high))
        self.integer.append(1 if integer else 0)
        return name

    def tighten(self, name, low=None, high=None):
        ident = self.index[name]
        if low is not None:
            self.lower[ident] = max(self.lower[ident], float(low))
        if high is not None:
            self.upper[ident] = min(self.upper[ident], float(high))

    def binary(self, name):
        return self.var(name, 0, 1, integer=True)

    def row(self, coefficients, low=-np.inf, high=np.inf):
        terms = {}
        for name, value in coefficients.items():
            if value:
                terms[name] = terms.get(name, 0.0) + float(value)
        self.rows.append((terms, low, high))

    def equal(self, coefficients, rhs):
        self.row(coefficients, rhs, rhs)

    def _matrix(self, extra_rows):
        rows = self.rows + list(extra_rows)
        data, row_index, col_index = [], [], []
        lower, upper = [], []
        for r, (terms, low, high) in enumerate(rows):
            for name, value in terms.items():
                data.append(value)
                row_index.append(r)
                col_index.append(self.index[name])
            lower.append(low)
            upper.append(high)
        matrix = coo_matrix(
            (data, (row_index, col_index)), shape=(len(rows), len(self.names))
        ).tocsr()
        return matrix, np.array(lower), np.array(upper)

    def _solve_once(self, matrix, lower, upper, cost, options):
        return milp(
            cost,
            integrality=np.array(self.integer),
            bounds=Bounds(np.array(self.lower), np.array(self.upper)),
            constraints=LinearConstraint(matrix, lower, upper),
            options=options,
        )

    def solve(self, objective=None, extra_rows=(), time_limit=None):
        matrix, lower, upper = self._matrix(extra_rows)
        cost = np.zeros(len(self.names))
        if objective:
            for name, value in objective.items():
                cost[self.index[name]] += value
        base = {"mip_rel_gap": 0.0}
        if time_limit is not None:
            base["time_limit"] = float(time_limit)
        result = self._solve_once(matrix, lower, upper, cost, dict(base))
        if result.success:
            return result
        # HiGHS presolve can report a spurious infeasibility on these highly
        # degenerate one-hot/gated-product models.  Retrying with presolve off
        # is both a correctness fallback and a cheap cross-check.
        retry = dict(base)
        retry["presolve"] = False
        return self._solve_once(matrix, lower, upper, cost, retry)

    def feasible(self, extra_rows=(), time_limit=None):
        result = self.solve(extra_rows=extra_rows, time_limit=time_limit)
        return bool(result.success)

    def optimise(self, objective, extra_rows=(), time_limit=None):
        result = self.solve(objective, extra_rows=extra_rows, time_limit=time_limit)
        if not result.success:
            raise Infeasible(result.message)
        return result

    def value(self, result, name):
        return float(result.x[self.index[name]])

    def present(self, result, name):
        return self.value(result, name) >= 0.5


def gated_product(model, name, flag, source, low, high):
    """Return a variable exactly equal to ``flag * source``.

    ``flag`` is binary and ``source`` is bounded by ``[low, high]``.  The four
    McCormick inequalities are exact for a binary flag, so the product may be
    used inside a convex sum without admitting phantom solutions.
    """
    product = model.var(name, min(0.0, low), max(0.0, high))
    model.row({product: 1.0, flag: -low}, low=0.0)
    model.row({product: 1.0, flag: -high}, high=0.0)
    model.row({product: 1.0, source: -1.0, flag: -high}, low=-high)
    model.row({product: 1.0, source: -1.0, flag: -low}, high=-low)
    return product


class Second:
    """One original second: optional presence, bounded reading, gated moment."""

    def __init__(self, model, key, low, high):
        self.model = model
        self.key = key
        self.low = float(low)
        self.high = float(high)
        self.presence = model.binary(key + ".present")
        self.value = model.var(key + ".value", self.low, self.high)
        self.moment = gated_product(
            model, key + ".moment", self.presence, self.value, self.low, self.high
        )

    def pin(self, value):
        value = float(value)
        self.model.tighten(self.presence, 1.0, 1.0)
        self.model.tighten(self.value, value, value)

    def force_present(self):
        self.model.tighten(self.presence, 1.0, 1.0)

    def force_absent(self):
        self.model.tighten(self.presence, 0.0, 0.0)


class Cell:
    """One archive cell over ``n`` ordered children.

    ``children`` is a list of ``(presence_name, moment_name)`` pairs.  The
    moment of a child is its stored value multiplied by its presence indicator,
    so a real zero and a missing child remain distinguishable.

    ``status`` is one of:
      * ``"free"``       -- no surviving evidence; value/existence are variables
      * ``("value", v)`` -- a surviving cell produced by aggregation
      * ``"absent"``     -- a surviving empty cell that legitimately missed xff
    """

    def __init__(
        self,
        model,
        key,
        children,
        threshold,
        low,
        high,
        status="free",
        aggregation="average",
    ):
        if aggregation not in SUPPORTED_AGGREGATIONS:
            raise NotImplementedError(
                "aggregation %r is not supported; only %s"
                % (aggregation, ", ".join(SUPPORTED_AGGREGATIONS))
            )
        self.model = model
        self.key = key
        self.children = list(children)
        self.n = len(self.children)
        self.low = float(low)
        self.high = float(high)
        self.threshold = int(threshold)
        self.status = status
        self.aggregation = aggregation
        self._build()

    def _sum_children(self, coefficients=None):
        coefficients = {} if coefficients is None else dict(coefficients)
        for _, moment in self.children:
            coefficients[moment] = coefficients.get(moment, 0.0) + 1.0
        return coefficients

    def _build(self):
        model = self.model
        n = self.n
        prefix = self.key

        self.count = model.var(prefix + ".count", 0, n)
        coefficients = {self.count: 1.0}
        for presence, _ in self.children:
            coefficients[presence] = coefficients.get(presence, 0.0) - 1.0
        model.equal(coefficients, 0.0)

        if self.aggregation == "sum":
            self._build_sum()
        else:
            self._build_average()

    # -- sum --------------------------------------------------------------
    def _build_sum(self):
        model = self.model
        n = self.n
        prefix = self.key
        self.value = model.var(
            prefix + ".value", min(0.0, self.low), max(0.0, self.high)
        )

        if self.status == "absent":
            self.presence = model.var(prefix + ".present", 0.0, 0.0)
            self.moment = model.var(prefix + ".moment", 0.0, 0.0)
            model.row({self.count: 1.0}, high=float(self.threshold - 1))
            return

        onehot = [model.binary(prefix + ".b0")]
        for j in range(1, n + 1):
            onehot.append(model.binary("%s.b%d" % (prefix, j)))
        model.equal({name: 1.0 for name in onehot}, 1.0)
        self.bins = onehot
        coefficients = {self.count: 1.0}
        for j, name in enumerate(onehot):
            coefficients[name] = -float(j)
        model.equal(coefficients, 0.0)

        self.presence = model.binary(prefix + ".present")
        coefficients = {self.presence: 1.0}
        for j in range(self.threshold, n + 1):
            coefficients[onehot[j]] = -1.0
        model.row(coefficients, high=0.0)
        for j in range(self.threshold, n + 1):
            model.row({self.presence: 1.0, onehot[j]: -1.0}, low=0.0)

        if isinstance(self.status, tuple) and self.status[0] == "value":
            value = float(self.status[1])
            if not (self.low - 1e-9 <= value <= self.high + 1e-9):
                raise Infeasible(
                    "cell %s value %.12g outside readable range [%g, %g]"
                    % (self.key, value, self.low, self.high)
                )
            model.tighten(self.value, value, value)
            model.row({self.count: 1.0}, low=float(self.threshold))
            coefficients = self._sum_children()
            coefficients[self.value] = -1.0
            model.equal(coefficients, 0.0)
            self.moment = model.var(prefix + ".moment", value, value)
            return

        # Absent cells sum to zero; present cells stay inside [low, high].
        model.row({self.value: 1.0, onehot[0]: self.low}, low=self.low)
        model.row({self.value: 1.0, onehot[0]: self.high}, high=self.high)
        coefficients = self._sum_children()
        coefficients[self.value] = -1.0
        model.equal(coefficients, 0.0)
        self.moment = gated_product(
            model, prefix + ".moment", self.presence, self.value, self.low, self.high
        )

    # -- average ----------------------------------------------------------
    def _build_average(self):
        model = self.model
        n = self.n
        prefix = self.key

        if self.status == "absent":
            self.presence = model.var(prefix + ".present", 0.0, 0.0)
            self.value = model.var(prefix + ".value", 0.0, 0.0)
            self.moment = model.var(prefix + ".moment", 0.0, 0.0)
            model.row({self.count: 1.0}, high=float(self.threshold - 1))
            return

        if isinstance(self.status, tuple) and self.status[0] == "value":
            value = float(self.status[1])
            if not (self.low - 1e-9 <= value <= self.high + 1e-9):
                raise Infeasible(
                    "cell %s value %.12g outside readable range [%g, %g]"
                    % (self.key, value, self.low, self.high)
                )
            self.presence = model.var(prefix + ".present", 1.0, 1.0)
            self.value = model.var(prefix + ".value", value, value)
            self.moment = model.var(prefix + ".moment", value, value)
            model.row({self.count: 1.0}, low=float(self.threshold))
            coefficients = self._sum_children()
            coefficients[self.count] = -value
            model.equal(coefficients, 0.0)
            return

        # Free cell.  ``count`` is an integer sum of child presences.  We write
        # ``value * count`` with one-hot count bins and exact gated products;
        # the mean equation is ``sum(child moments) == value * count``.
        onehot = [model.binary(prefix + ".b0")]
        for j in range(1, n + 1):
            onehot.append(model.binary("%s.b%d" % (prefix, j)))
        model.equal({name: 1.0 for name in onehot}, 1.0)
        coefficients = {self.count: 1.0}
        for j, name in enumerate(onehot):
            coefficients[name] = -float(j)
        model.equal(coefficients, 0.0)
        self.bins = onehot

        self.presence = model.binary(prefix + ".present")
        coefficients = {self.presence: 1.0}
        for j in range(self.threshold, n + 1):
            coefficients[onehot[j]] = -1.0
        model.row(coefficients, high=0.0)
        for j in range(self.threshold, n + 1):
            model.row({self.presence: 1.0, onehot[j]: -1.0}, low=0.0)

        self.value = model.var(
            prefix + ".value", min(0.0, self.low), max(0.0, self.high)
        )
        # Absent cells must read zero; present cells stay in [low, high].
        model.row({self.value: 1.0, onehot[0]: self.low}, low=self.low)
        model.row({self.value: 1.0, onehot[0]: self.high}, high=self.high)

        # ``weighted[j] == j * value`` when bin j is selected, else zero.  Using
        # the same one-hot bins as ``count`` keeps the mean equation consistent.
        weighted = {}
        for j in range(1, n + 1):
            weighted[j] = gated_product(
                model,
                "%s.w%d" % (prefix, j),
                onehot[j],
                self.value,
                self.low,
                self.high,
            )
        coefficients = self._sum_children()
        for j in range(1, n + 1):
            coefficients[weighted[j]] = coefficients.get(weighted[j], 0.0) - float(j)
        model.equal(coefficients, 0.0)

        self.moment = gated_product(
            model, prefix + ".moment", self.presence, self.value, self.low, self.high
        )
