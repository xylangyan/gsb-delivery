"""Budgeted multi-tenant planning for merged DDSketch minute summaries.

A delayed rollup keeps a positive/negative DDSketch pair per tenant.  For each
minute it must decide which tenant percentiles can be issued under a shared
bucket budget.  A report is issuable only when the *actual merged buckets* can
prove the tenant's relative-error contract.  DDSketch can promise such an error
only for a value whose bucket survived collapsing; once an edge bucket has
been folded, the distribution it lost cannot be recovered by allocating more
buckets later.

The planner therefore separates three failure modes:

``mapping``
    The candidate mapping is incompatible with the old summary's declared
    mapping, or its accuracy is coarser than the contract.

``collapsed``
    The rank lands in a bucket whose provenance says several source buckets
    were already folded together.  The witness records the incoming-volume
    scenario and the damaged source range.  If the damage came from the old
    summary's capability declaration, more capacity cannot repair it.

``volume``
    The requested rank can fall among newly added counts whose values are not
    in any supplied sketch.  The witness records the concrete incoming count
    and placement that moves the rank there.

The planner never mutates its inputs.  Ordinary merges are unaffected; see
:func:`merge_without_plan`.
"""
from collections import namedtuple


# Relative errors are compared with a small tolerance so bounds supplied as
# decimal literals (for example 0.01) are not rejected by binary rounding.
ERROR_TOLERANCE = 1e-12

COLLAPSE_DIRECTIONS = ("lowest", "highest")


class TenantContract(object):
    """A key tenant's reporting contract.

    Args:
        tenant (str): tenant identifier
        percentiles (iterable): percentiles the tenant must report
        error_bounds (dict): ``percentile -> maximum relative error``.  A
            report ``y`` for true value ``x`` passes when
            ``abs(y - x) <= bound * abs(x)``.
        priorities (dict): ``percentile -> business priority``.  Larger values
            are served first after the number of certified reports is fixed.
        require_zero_exact (bool): when true (the default), a true value of
            zero is only certified by reporting exactly zero.  This is enforced
            by requiring that the zero bucket contain only exact zeros.
    """

    def __init__(
        self,
        tenant,
        percentiles,
        error_bounds,
        priorities=None,
        require_zero_exact=True,
    ):
        self.tenant = tenant
        self.percentiles = tuple(percentiles)
        self.error_bounds = dict(error_bounds)
        self.priorities = dict(priorities or {})
        self.require_zero_exact = require_zero_exact

    def bound(self, percentile):
        return self.error_bounds[percentile]

    def priority(self, percentile):
        return self.priorities.get(percentile, 0)


class Capability(object):
    """What the old summary can still support.

    Args:
        mapping_accuracy (float): relative accuracy used to build the old
            summary.
        collapse_direction (str): ``"lowest"`` or ``"highest"``.
        positive_range (tuple): inclusive source-key range already folded into
            the positive edge bucket, or ``None``.
        negative_range (tuple): the analogous range for the negative edge.
    """

    def __init__(
        self,
        mapping_accuracy,
        collapse_direction="lowest",
        positive_range=None,
        negative_range=None,
    ):
        self.mapping_accuracy = mapping_accuracy
        self.collapse_direction = collapse_direction
        self.positive_range = _normalize_range(positive_range)
        self.negative_range = _normalize_range(negative_range)

    @classmethod
    def from_sketch(cls, sketch, collapse_direction="lowest"):
        """Build a capability declaration from a sketch's actual provenance."""
        return cls(
            mapping_accuracy=sketch._mapping.relative_accuracy,
            collapse_direction=collapse_direction,
            positive_range=_store_edge_range(sketch._store, collapse_direction),
            negative_range=_store_edge_range(
                sketch._negative_store, collapse_direction
            ),
        )


class CandidateConfiguration(namedtuple("CandidateConfiguration", [
    "name",
    "bin_capacity",
    "collapse_direction",
    "mapping_accuracy",
])):
    """One admissible sketch configuration.

    ``bin_capacity`` is the number of buckets retained at each edge of one
    store.  A tenant keeps a positive/negative pair, so the planner charges
    ``2 * bin_capacity`` capacity for the pair.
    """

    __slots__ = ()

    def cost(self):
        return 2 * self.bin_capacity

    def validate(self):
        if self.bin_capacity <= 0:
            raise ValueError("bin_capacity must be positive")
        if self.collapse_direction not in COLLAPSE_DIRECTIONS:
            raise ValueError(
                "collapse_direction must be one of %r" % (COLLAPSE_DIRECTIONS,)
            )
        if not (0.0 < self.mapping_accuracy < 1.0):
            raise ValueError("mapping_accuracy must be in (0, 1)")


class WindowSketch(object):
    """One tenant's state for one reporting window.

    Args:
        window: label for the reporting window (for example ``"2026-11-22T10:31"``)
        tenant (str): tenant identifier
        old: previous minute's sketch, or ``None``
        new: additional sketch already collected for this window, or ``None``
        capability: :class:`Capability` declared for ``old``, or ``None``
        incoming_range (tuple): inclusive range of additional counts that may
            still arrive.  These counts are not present in ``old``/``new``, so
            their values are unknown.  Defaults to ``(0, 0)``.
        scenarios: optional explicit iterable of :class:`VolumeScenario`.  When
            supplied it replaces the scenarios derived from
            ``incoming_range``.
    """

    def __init__(
        self,
        window,
        tenant,
        old,
        new=None,
        capability=None,
        incoming_range=(0, 0),
        scenarios=None,
    ):
        low, high = incoming_range
        if low < 0 or high < low:
            raise ValueError("incoming_range must satisfy 0 <= low <= high")
        self.window = window
        self.tenant = tenant
        self.old = old
        self.new = new
        self.capability = capability
        self.incoming_range = (int(low), int(high))
        self._scenarios = list(scenarios) if scenarios is not None else None

    @property
    def key(self):
        return (self.window, self.tenant)

    def base_sketches(self):
        return [s for s in (self.old, self.new) if s is not None and s.count]

    def mapping_accuracies(self):
        return [s._mapping.relative_accuracy for s in self.base_sketches()]

    def declared_mapping_accuracy(self):
        if self.capability is not None:
            return self.capability.mapping_accuracy
        accuracies = self.mapping_accuracies()
        return accuracies[0] if accuracies else None

    def declared_ranges(self):
        if self.capability is not None:
            return {
                "positive": self.capability.positive_range,
                "negative": self.capability.negative_range,
            }
        result = {}
        if self.old is not None:
            result["positive"] = _store_edge_range(self.old._store, "lowest")
            result["negative"] = _store_edge_range(
                self.old._negative_store, "lowest"
            )
        return result

    def scenarios_for(self, sketch, percentile):
        """Return the volume scenarios that must all be certifiable.

        Explicit scenarios are used verbatim.  Otherwise the arrival count
        range is checked once per placement over the whole ``[low, high]``
        interval, plus the no-arrival scenario.  Because collapse damage is
        confined to the edge buckets, checking the full rank interval per
        placement covers every reachable bucket.
        """
        if self._scenarios is not None:
            return list(self._scenarios)
        low, high = self.incoming_range
        scenarios = []
        if low == 0:
            scenarios.append(VolumeScenario("base", 0, "none"))
        if high > 0:
            scenarios.append(VolumeScenario("low", low, "low", high))
            scenarios.append(VolumeScenario("high", low, "high", high))
        return scenarios


class VolumeScenario(object):
    """One admissible incoming-volume scenario for a window.

    ``extra`` counts are expected to arrive outside the supplied sketches.
    ``placement`` says where they sit relative to the retained distribution:
    ``"low"`` (below every retained value), ``"high"`` (above every retained
    value), or ``"none"`` (no arrivals; only valid when ``extra`` is zero).
    """

    def __init__(self, name, extra, placement, extra_high=None, sketch=None):
        if extra_high is None:
            extra_high = extra
        if extra < 0 or extra_high < extra:
            raise ValueError("scenario extras must satisfy 0 <= extra <= extra_high")
        if placement not in ("low", "high", "none"):
            raise ValueError("placement must be low, high, or none")
        if placement == "none" and extra_high and sketch is None:
            raise ValueError("placement='none' requires extra=0")
        if placement != "none" and extra_high == 0:
            placement = "none"
        self.name = name
        self.extra = int(extra)
        self.extra_high = int(extra_high)
        self.placement = placement
        # A known additional summary.  Its bucket counts are merged into the
        # scenario, so no unknown-placement adjustment is applied.
        self.sketch = sketch

    @property
    def is_range(self):
        return self.extra != self.extra_high

    def __repr__(self):
        return "VolumeScenario(name=%r, extra=%r, placement=%r, sketch=%r)" % (
            self.name,
            self.extra,
            self.placement,
            self.sketch,
        )


class IncomingWitness(object):
    """A verifiable incoming-volume counterexample.

    ``scenario`` names the volume scenario.  ``future_rank`` is the rank used
    by ``get_quantile_value`` after the arrivals; ``known_rank`` is the same
    rank after removing arrivals at the low edge.  ``bucket_key`` is the
    retained bucket that contains ``known_rank``, or ``None`` when the rank
    falls among unknown arrivals.  ``damaged_range`` is the source range
    already folded into that bucket, if any.
    """

    def __init__(
        self,
        scenario,
        extra,
        placement,
        future_rank,
        known_rank,
        bucket_key=None,
        damaged_range=None,
        side=None,
        reason=None,
    ):
        self.scenario = scenario
        self.extra = extra
        self.placement = placement
        self.future_rank = future_rank
        self.known_rank = known_rank
        self.bucket_key = bucket_key
        self.damaged_range = damaged_range
        self.side = side
        self.reason = reason

    def as_dict(self):
        return {
            "scenario": self.scenario,
            "extra": self.extra,
            "placement": self.placement,
            "future_rank": self.future_rank,
            "known_rank": self.known_rank,
            "bucket_key": self.bucket_key,
            "damaged_range": self.damaged_range,
            "side": self.side,
            "reason": self.reason,
        }

    def __repr__(self):
        return "IncomingWitness(%r)" % (self.as_dict(),)


class ReportEvidence(object):
    """Auditable computation for one report from the actual merged buckets.

    The evidence records the merged total count, the incoming-volume range, and
    one entry per checked scenario.  Each scenario entry contains the rank
    interval and the retained bucket fields (``bucket_key``,
    ``bucket_count``, ``source_keys``, ``value_low``, ``value_high``,
    ``reported_value``) needed to recompute that scenario's verdict.
    """

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def as_dict(self):
        return dict(self.__dict__)

    def __repr__(self):
        return "ReportEvidence(%r)" % (self.as_dict(),)


class ReportDecision(object):
    """The decision for one ``(window, tenant, percentile)`` report."""

    def __init__(
        self,
        window,
        tenant,
        percentile,
        certified,
        configuration=None,
        value=None,
        evidence=None,
        reason=None,
        witnesses=None,
        inherited_collapse=False,
        values_by_scenario=None,
    ):
        self.window = window
        self.tenant = tenant
        self.percentile = percentile
        self.certified = certified
        self.configuration = configuration
        self.value = value
        self.evidence = evidence
        self.reason = reason
        self.witnesses = list(witnesses or [])
        self.inherited_collapse = inherited_collapse
        self.values_by_scenario = dict(values_by_scenario or {})

    def __repr__(self):
        return (
            "ReportDecision(window=%r, tenant=%r, percentile=%r, certified=%r)"
            % (self.window, self.tenant, self.percentile, self.certified)
        )


class TenantPlan(object):
    """Chosen configuration and decisions for one window/tenant unit."""

    def __init__(self, window_sketch, contract, configuration, decisions, cost):
        self.window_sketch = window_sketch
        self.contract = contract
        self.configuration = configuration
        self.decisions = decisions
        self.cost = cost

    @property
    def certified(self):
        return [d for d in self.decisions if d.certified]

    @property
    def priority_sum(self):
        return sum(
            self.contract.priority(d.percentile) for d in self.certified
        )

    @property
    def allocated(self):
        return self.cost > 0


class Shortfall(object):
    """A contract metric that the chosen plan cannot guarantee.

    ``candidates`` maps candidate name to that candidate's decision for the
    metric, so different configurations can show their own counterexamples.
    """

    def __init__(self, window, tenant, percentile, chosen, candidates, reason):
        self.window = window
        self.tenant = tenant
        self.percentile = percentile
        self.chosen = chosen
        self.candidates = candidates
        self.reason = reason

    def __repr__(self):
        return "Shortfall(window=%r, tenant=%r, percentile=%r, reason=%r)" % (
            self.window,
            self.tenant,
            self.percentile,
            self.reason,
        )


class RollupPlan(object):
    """Result of :func:`plan_rollup`."""

    def __init__(
        self,
        budget,
        used,
        tenant_plans,
        decisions,
        candidate_outcomes,
        shortfalls,
    ):
        self.budget = budget
        self.used = used
        self.tenant_plans = tenant_plans
        self.decisions = decisions
        self.candidate_outcomes = candidate_outcomes
        self.shortfalls = shortfalls

    @property
    def certified(self):
        return [d for d in self.decisions if d.certified]

    @property
    def issued(self):
        """Reports that may be issued, as ``(window, tenant, percentile)``."""
        return [(d.window, d.tenant, d.percentile) for d in self.certified]

    def for_tenant(self, tenant):
        return [d for d in self.decisions if d.tenant == tenant]

    def plan_for(self, window, tenant):
        for plan in self.tenant_plans:
            if (
                plan.window_sketch.window == window
                and plan.window_sketch.tenant == tenant
            ):
                return plan
        return None

    def issuance_schedule(self):
        """List every issuable report with its configuration and evidence."""
        schedule = []
        for plan in self.tenant_plans:
            for decision in plan.certified:
                schedule.append(
                    {
                        "window": decision.window,
                        "tenant": decision.tenant,
                        "percentile": decision.percentile,
                        "value": decision.value,
                        "values_by_scenario": decision.values_by_scenario,
                        "configuration": plan.configuration.name,
                        "bin_capacity": plan.configuration.bin_capacity,
                        "collapse_direction": plan.configuration.collapse_direction,
                        "evidence": decision.evidence.as_dict(),
                    }
                )
        return schedule

    def shortfall_witnesses(self):
        """Return insufficiency details keyed by tenant and percentile."""
        return {
            (shortfall.window, shortfall.tenant, shortfall.percentile): {
                "reason": shortfall.reason,
                "candidates": {
                    name: {
                        "certified": decision.certified,
                        "reason": decision.reason,
                        "witnesses": [
                            witness.as_dict() for witness in decision.witnesses
                        ],
                    }
                    for name, decision in shortfall.candidates.items()
                },
            }
            for shortfall in self.shortfalls
        }

    def __repr__(self):
        return "RollupPlan(budget=%r, used=%r, certified=%d, shortfalls=%d)" % (
            self.budget,
            self.used,
            len(self.certified),
            len(self.shortfalls),
        )


def _normalize_range(value):
    if value is None:
        return None
    low, high = value
    if low > high:
        raise ValueError("range must be ordered, got %r" % (value,))
    return (int(low), int(high))


def _store_edge_range(store, collapse_direction):
    if getattr(store, "count", 0) == 0:
        return None
    key = store.min_key if collapse_direction == "lowest" else store.max_key
    low, high = store.represented_range(key)
    return None if low == high else (low, high)


def _accuracy_matches(left, right):
    if left is None or right is None:
        return left is right
    scale = max(1.0, abs(left), abs(right))
    return abs(left - right) <= 1e-12 * scale


def _mapping_compatible(window, configuration):
    declared = window.declared_mapping_accuracy()
    if declared is not None and not _accuracy_matches(
        declared, configuration.mapping_accuracy
    ):
        return False
    return all(
        _accuracy_matches(accuracy, configuration.mapping_accuracy)
        for accuracy in window.mapping_accuracies()
    )


def _new_sketch(configuration):
    from .ddsketch import LogCollapsingHighestDenseDDSketch
    from .ddsketch import LogCollapsingLowestDenseDDSketch

    if configuration.collapse_direction == "lowest":
        factory = LogCollapsingLowestDenseDDSketch
    else:
        factory = LogCollapsingHighestDenseDDSketch
    return factory(
        relative_accuracy=configuration.mapping_accuracy,
        bin_limit=configuration.bin_capacity,
    )


def _merged_sketch(window, configuration, scenario=None):
    """Merge ``window`` under ``configuration`` without touching the inputs."""
    merged = _new_sketch(configuration)
    for sketch in window.base_sketches():
        merged.absorb(sketch)
    if scenario is not None and scenario.sketch is not None:
        merged.absorb(scenario.sketch)
    return merged


def _side_at_rank(sketch, rank):
    if rank < sketch._negative_store.count:
        return "negative"
    if rank < sketch._zero_count + sketch._negative_store.count:
        return "zero"
    return "positive"


def _store_for_side(sketch, side):
    return sketch._negative_store if side == "negative" else sketch._store


def _key_at_rank(sketch, side, rank):
    """Return the bucket key at a rank within one side, or ``None`` for zero."""
    if side == "zero":
        return None
    if side == "negative":
        reversed_rank = sketch._negative_store.count - rank - 1
        return sketch._negative_store.key_at_rank(reversed_rank, lower=False)
    return sketch._store.key_at_rank(
        rank - sketch._zero_count - sketch._negative_store.count
    )


def _value_for_side(sketch, side, key):
    if side == "zero":
        return 0.0
    value = sketch._mapping.value(key)
    return -value if side == "negative" else value


def _scenario_rank_ranges(sketch, percentile, scenario):
    """Return possible ``(future_rank, known_rank)`` bounds for a scenario.

    ``future_rank`` is the rank ``get_quantile_value`` uses after the merge.
    ``known_rank`` removes arrivals placed below the retained distribution, so
    it indexes the buckets the planner can actually see.
    """
    count = sketch.count
    future_low = percentile * (count + scenario.extra - 1)
    future_high = percentile * (count + scenario.extra_high - 1)
    future_rank = (future_low, future_high)
    if scenario.placement == "low":
        # known(e) = q * (count + e - 1) - e is decreasing in e for q < 1.
        known_low = percentile * (count + scenario.extra_high - 1) - scenario.extra_high
        known_high = percentile * (count + scenario.extra - 1) - scenario.extra
    else:
        known_low = future_low
        known_high = future_high
    return future_rank, (min(known_low, known_high), max(known_low, known_high))


def _within_bound(reported, low, high, bound):
    if low <= 0 <= high:
        return low == high == 0 and reported == 0
    low_error = abs(reported - low) / abs(low)
    high_error = abs(reported - high) / abs(high)
    return (
        low_error <= bound + ERROR_TOLERANCE
        and high_error <= bound + ERROR_TOLERANCE
    )


def _value_bounds(sketch, side, key):
    """Bound the true value represented by a verified, intact bucket."""
    alpha = sketch._mapping.relative_accuracy
    if side == "zero":
        return 0.0, 0.0
    value = sketch._mapping.value(key)
    low = value / (1.0 + alpha)
    high = value / (1.0 - alpha)
    if side == "negative":
        return -high, -low
    return low, high


def _bucket_check(sketch, percentile, contract, side, key_low, key_high):
    """Check every bucket a rank interval can reach.

    Returns ``(ok, reason, fields)``.  ``fields`` is evidence read from the
    actual merged store; on success it names the bucket whose value is emitted
    and the full key range that value was checked against.
    """
    if side == "zero":
        fields = {
            "side": "zero",
            "bucket_key": None,
            "bucket_count": sketch._zero_count,
            "exact_zero_count": sketch.exact_zero_count,
            "source_keys": None,
            "value_low": 0.0,
            "value_high": 0.0,
            "reported_value": 0.0,
        }
        if (
            contract.require_zero_exact
            and sketch.exact_zero_count + ERROR_TOLERANCE < sketch._zero_count
        ):
            return False, "zero-bucket-contains-nonzero-values", fields
        return True, None, fields

    alpha = sketch._mapping.relative_accuracy
    bound = contract.bound(percentile)
    store = _store_for_side(sketch, side)
    low_key = min(key_low, key_high)
    high_key = max(key_low, key_high)
    low_key = max(low_key, store.min_key)
    high_key = min(high_key, store.max_key)
    if low_key > high_key:
        return False, "rank-outside-retained-buckets", {
            "side": side,
            "key_low": low_key,
            "key_high": high_key,
        }

    for key in range(low_key, high_key + 1):
        low, high = store.represented_range(key)
        if low < high:
            return False, "rank-fell-in-collapsed-bucket", {
                "side": side,
                "bucket_key": key,
                "bucket_count": store.bins[key - store.offset],
                "source_keys": (low, high),
                "damaged_range": (low, high),
                "key_low": low_key,
                "key_high": high_key,
            }
    if bound + ERROR_TOLERANCE < alpha:
        return False, "mapping-accuracy-exceeds-bound", {
            "side": side,
            "mapping_accuracy": alpha,
            "bound": bound,
            "key_low": low_key,
            "key_high": high_key,
        }

    populated = [
        key
        for key in range(low_key, high_key + 1)
        if store.bins[key - store.offset]
    ]
    if not populated:
        populated = [low_key, high_key]

    value_lows = []
    value_highs = []
    for key in populated:
        value_low, value_high = _value_bounds(sketch, side, key)
        value_lows.append(value_low)
        value_highs.append(value_high)
    interval_low = min(value_lows)
    interval_high = max(value_highs)

    best = None
    for key in populated:
        reported = _value_for_side(sketch, side, key)
        if not _within_bound(reported, interval_low, interval_high, bound):
            continue
        error = max(
            abs(reported - interval_low) / abs(interval_low),
            abs(reported - interval_high) / abs(interval_high),
        )
        if best is None or error < best[0]:
            best = (error, key, reported)
    if best is None:
        return False, "no-retained-bucket-value-meets-bound", {
            "side": side,
            "value_low": interval_low,
            "value_high": interval_high,
            "bound": bound,
            "mapping_accuracy": alpha,
            "key_low": low_key,
            "key_high": high_key,
            "populated_keys": tuple(populated),
        }

    error, key, reported = best
    low, high = store.represented_range(key)
    return True, None, {
        "side": side,
        "bucket_key": key,
        "bucket_count": store.bins[key - store.offset],
        "source_keys": (low, high),
        "value_low": interval_low,
        "value_high": interval_high,
        "reported_value": reported,
        "reported_error": error,
        "mapping_accuracy": alpha,
        "bound": bound,
        "key_low": low_key,
        "key_high": high_key,
        "populated_keys": tuple(populated),
    }


def _scenario_decision(sketch, percentile, contract, scenario):
    """Check one scenario and return its witness or certificate evidence."""
    if sketch.count <= 0:
        return (
            False,
            "empty-window",
            None,
            IncomingWitness(
                scenario.name,
                scenario.extra,
                scenario.placement,
                0.0,
                None,
                reason="empty-window",
            ),
        )
    future_ranks, known_ranks = _scenario_rank_ranges(sketch, percentile, scenario)
    future_rank = future_ranks[0]
    if known_ranks is None:
        # Unreachable after the interval check above; kept for safety.
        witness = IncomingWitness(
            scenario.name,
            scenario.extra_high,
            scenario.placement,
            future_ranks[1],
            None,
            reason="rank-in-unknown-incoming-values",
        )
        return False, "rank-in-unknown-incoming-values", None, witness

    known_low, known_high = known_ranks
    if known_low < 0 or known_high > sketch.count - 1:
        witness = IncomingWitness(
            scenario.name,
            scenario.extra_high,
            scenario.placement,
            future_ranks[1],
            None,
            reason="rank-in-unknown-incoming-values",
        )
        return False, "rank-in-unknown-incoming-values", None, witness
    side_low = _side_at_rank(sketch, known_low)
    side_high = _side_at_rank(sketch, known_high)
    if side_low != side_high:
        witness = IncomingWitness(
            scenario.name,
            scenario.extra,
            scenario.placement,
            future_rank,
            known_low,
            side="mixed",
            reason="rank-range-crosses-sign",
        )
        return False, "rank-range-crosses-sign", None, witness

    side = side_low
    if side == "zero":
        key_low = key_high = None
    else:
        key_low = _key_at_rank(sketch, side, known_low)
        key_high = _key_at_rank(sketch, side, known_high)
    ok, reason, fields = _bucket_check(
        sketch, percentile, contract, side, key_low, key_high
    )
    if not ok:
        witness = IncomingWitness(
            scenario.name,
            scenario.extra,
            scenario.placement,
            future_rank,
            known_low,
            bucket_key=fields.get("bucket_key"),
            damaged_range=fields.get("damaged_range"),
            side=side,
            reason=reason,
        )
        return False, reason, fields, witness

    fields = dict(fields)
    fields.update(
        {
            "known_rank_low": known_low,
            "known_rank_high": known_high,
        }
    )
    return True, None, fields, None


def _decide_report(window_sketch, sketch, configuration, contract, percentile):
    """Decide one report across all admissible volume scenarios."""
    scenarios = window_sketch.scenarios_for(sketch, percentile)
    scenario_results = []
    witnesses = []
    first_reason = None
    inherited = False

    for scenario in scenarios:
        scenario_sketch = sketch
        if scenario.sketch is not None:
            scenario_sketch = _merged_sketch(
                window_sketch, configuration, scenario
            )
        ok, reason, fields, witness = _scenario_decision(
            scenario_sketch, percentile, contract, scenario
        )
        scenario_results.append(
            {
                "scenario": scenario.name,
                "extra": scenario.extra,
                "placement": scenario.placement,
                "certified": ok,
                "reason": reason,
                "fields": fields,
                "known_sketch": scenario.sketch is not None,
            }
        )
        if not ok:
            if first_reason is None:
                first_reason = reason
            if witness is not None:
                witnesses.append(witness)
            if (
                reason == "rank-fell-in-collapsed-bucket"
                and fields is not None
                and _is_inherited(window_sketch, fields)
            ):
                inherited = True

    base_evidence = dict(
        window=window_sketch.window,
        tenant=window_sketch.tenant,
        percentile=percentile,
        configuration=configuration.name,
        bin_capacity=configuration.bin_capacity,
        collapse_direction=configuration.collapse_direction,
        total_count=sketch.count,
        incoming_range=window_sketch.incoming_range,
        scenarios=scenario_results,
    )

    if not witnesses and all(result["certified"] for result in scenario_results):
        values = {
            result["scenario"]: (result["fields"] or {}).get("reported_value")
            for result in scenario_results
            if result["certified"]
        }
        unique = []
        for value in values.values():
            if value is None:
                continue
            if not any(
                abs(value - other) <= ERROR_TOLERANCE * max(1.0, abs(value))
                for other in unique
            ):
                unique.append(value)
        # Each admissible volume scenario is within its own contract.  Emit a
        # single value only when all scenarios yielded the same bucket value;
        # otherwise the per-scenario values are the auditable result.
        value = unique[0] if len(unique) == 1 else None
        evidence = ReportEvidence(**base_evidence)
        return ReportDecision(
            window_sketch.window,
            window_sketch.tenant,
            percentile,
            True,
            configuration=configuration,
            value=value,
            values_by_scenario=values,
            evidence=evidence,
        )

    evidence = ReportEvidence(**base_evidence)
    return ReportDecision(
        window_sketch.window,
        window_sketch.tenant,
        percentile,
        False,
        configuration=configuration,
        evidence=evidence,
        reason=first_reason,
        witnesses=witnesses,
        inherited_collapse=inherited,
    )


def _is_inherited(window_sketch, fields):
    """Whether a damaged bucket intersects the old capability's declared range."""
    side = fields.get("side")
    damaged = fields.get("damaged_range")
    if not damaged:
        return False
    declared = window_sketch.declared_ranges().get(side)
    if declared is None:
        return False
    low, high = declared
    damaged_low, damaged_high = damaged
    return damaged_low <= high and damaged_high >= low


def _option_for(window_sketch, contract, configuration, budget):
    """Evaluate one candidate for one window/tenant.

    Returns ``(applicable, reason, decisions)``.  A candidate is inapplicable
    when it cannot merge the supplied sketches or cannot fit the whole budget
    by itself.
    """
    if not _mapping_compatible(window_sketch, configuration):
        return False, "mapping-incompatible-with-old-summary", []
    if configuration.cost() > budget:
        return False, "candidate-exceeds-budget", []
    try:
        sketch = _merged_sketch(window_sketch, configuration)
    except ValueError:
        return False, "mapping-incompatible-with-old-summary", []

    decisions = [
        _decide_report(window_sketch, sketch, configuration, contract, percentile)
        for percentile in contract.percentiles
    ]
    return True, None, decisions


def _option_key(decisions, contract):
    certified = [d for d in decisions if d.certified]
    return (
        len(certified),
        sum(contract.priority(d.percentile) for d in certified),
    )


def _allocate(units, contracts, candidates, budget):
    """Choose exactly one candidate for every contracted unit.

    Multiple-choice knapsack.  The objective is lexicographic: maximize the
    number of certified reports, then their summed business priority, then
    minimize capacity consumed.  A unit whose candidate fits the budget is
    always evaluated against the unit's contract; the solver only decides
    which candidate it receives.
    """
    options = []
    outcomes = {}
    for window_sketch in units:
        contract = contracts.get(window_sketch.tenant)
        unit_options = []
        outcome = {}
        if contract is not None:
            for configuration in candidates:
                applicable, reason, decisions = _option_for(
                    window_sketch, contract, configuration, budget
                )
                outcome[configuration.name] = {
                    "applicable": applicable,
                    "reason": reason,
                    "decisions": decisions,
                }
                if not applicable:
                    continue
                certified, priority = _option_key(decisions, contract)
                unit_options.append(
                    (
                        configuration,
                        configuration.cost(),
                        decisions,
                        certified,
                        priority,
                    )
                )
        if contract is not None:
            # A unit may also be left unallocated when the shared budget cannot
            # cover every tenant.  This is a real outcome, not an omission.
            unit_options = unit_options + [(None, 0, [], 0, 0)]
        options.append((window_sketch, contract, unit_options))
        outcomes[window_sketch.key] = outcome

    # states: cost -> (certified, priority, choices)
    states = {0: (0, 0, [])}
    for window_sketch, contract, unit_options in options:
        if contract is None:
            continue
        next_states = {}
        for cost, (certified, priority, choices) in states.items():
            for option in unit_options:
                configuration, option_cost, decisions, opt_certified, opt_priority = (
                    option
                )
                new_cost = cost + option_cost
                if new_cost > budget:
                    continue
                new_key = (
                    certified + opt_certified,
                    priority + opt_priority,
                )
                existing = next_states.get(new_cost)
                if existing is None or new_key > existing[:2]:
                    next_states[new_cost] = (
                        new_key[0],
                        new_key[1],
                        choices + [(window_sketch, configuration, decisions, option_cost)],
                    )
        states = next_states
        if not states:
            break

    def score(state):
        certified, priority, choices = state
        used = sum(choice[3] for choice in choices)
        # Earlier units win ties so that, when a choice is arbitrary, business
        # ordering (caller-supplied unit order) determines which tenant gets it.
        order = tuple(
            -(window_sketch_index(units, choice[0])) for choice in choices
        )
        return (certified, priority, -used, order)

    if states:
        best_state = max(states.values(), key=score)
    else:
        best_state = (0, 0, [])
    chosen = {
        window_sketch.key: (configuration, decisions, cost)
        for window_sketch, configuration, decisions, cost in best_state[2]
    }
    tenant_plans = []
    for window_sketch, contract, unit_options in options:
        if contract is None:
            continue
        if window_sketch.key in chosen and chosen[window_sketch.key][0] is not None:
            configuration, decisions, cost = chosen[window_sketch.key]
        else:
            configuration, decisions, cost = None, [], 0
            applicable_options = [
                option for option in unit_options if option[0] is not None
            ]
            if applicable_options:
                _cfg, _cost, decisions, _cert, _priority = min(
                    applicable_options, key=lambda option: (option[1], option[0].name)
                )
                # No capacity was reserved.  Certified decisions must be
                # withdrawn; genuine per-candidate failures keep their reason.
                # Retain the evaluated candidate's name for auditability even
                # though its capacity was not charged.
                decisions = [_unallocated(decision) for decision in decisions]
                configuration, cost = _cfg, 0
        tenant_plans.append(
            TenantPlan(window_sketch, contract, configuration, decisions, cost)
        )
    used = sum(plan.cost for plan in tenant_plans)
    return tenant_plans, outcomes, used, options


def window_sketch_index(units, window_sketch):
    for index, unit in enumerate(units):
        if unit is window_sketch:
            return index
    return len(units)


def _unallocated(decision):
    """Withdraw a certified decision not selected by the budget solver."""
    if not decision.certified:
        return decision
    return ReportDecision(
        decision.window,
        decision.tenant,
        decision.percentile,
        False,
        configuration=decision.configuration,
        evidence=decision.evidence,
        reason="budget-not-allocated",
        witnesses=decision.witnesses,
        inherited_collapse=decision.inherited_collapse,
        values_by_scenario=decision.values_by_scenario,
    )


def _shortfalls(units, contracts, candidates, outcomes, tenant_plans, options):
    chosen = {plan.window_sketch.key: plan for plan in tenant_plans}
    applicable = {
        window_sketch.key: {
            option[0].name for option in unit_options if option[0] is not None
        }
        for window_sketch, _contract, unit_options in options
    }
    elsewhere = {
        key: cert
        for key, cert in (
            (
                window_sketch.key,
                max([option[3] for option in unit_options] or [0]),
            )
            for window_sketch, _contract, unit_options in options
        )
    }
    result = []
    for window_sketch in units:
        contract = contracts.get(window_sketch.tenant)
        if contract is None:
            continue
        plan = chosen.get(window_sketch.key)
        chosen_decisions = (
            {d.percentile: d for d in plan.decisions} if plan is not None else {}
        )
        for percentile in contract.percentiles:
            decision = chosen_decisions.get(percentile)
            if decision is not None and decision.certified:
                continue
            candidate_decisions = {}
            for configuration in candidates:
                outcome = outcomes[window_sketch.key].get(configuration.name)
                if outcome is None:
                    continue
                for candidate_decision in outcome["decisions"]:
                    if candidate_decision.percentile == percentile:
                        candidate_decisions[configuration.name] = candidate_decision
            if decision is None:
                reason = "budget-not-allocated"
            else:
                reason = decision.reason
            if not applicable.get(window_sketch.key):
                reason = "no-applicable-candidate"
            elif (
                plan is not None
                and not plan.allocated
                and elsewhere.get(window_sketch.key, 0)
                > len(plan.certified)
            ):
                reason = "budget-tradeoff"
            result.append(
                Shortfall(
                    window_sketch.window,
                    window_sketch.tenant,
                    percentile,
                    decision,
                    candidate_decisions,
                    reason,
                )
            )
    return result


def plan_rollup(windows, contracts, candidates, budget):
    """Plan a budgeted rollup.

    Args:
        windows: iterable of :class:`WindowSketch`
        contracts: iterable of :class:`TenantContract`
        candidates: iterable of :class:`CandidateConfiguration`
        budget: shared capacity, in buckets, for the positive/negative pairs

    Returns:
        :class:`RollupPlan`

    The plan maximizes the number of certified reports first, then the summed
    business priority of certified reports, then minimizes used capacity.
    Every uncertified contract metric appears in ``plan.shortfalls`` with its
    per-candidate counterexamples.
    """
    if budget < 0:
        raise ValueError("budget must be non-negative")

    units = list(windows)
    contract_map = {}
    for contract in contracts:
        if contract.tenant in contract_map:
            raise ValueError("duplicate contract for tenant %r" % contract.tenant)
        contract_map[contract.tenant] = contract

    candidate_list = list(candidates)
    if not candidate_list:
        raise ValueError("at least one candidate configuration is required")
    names = set()
    for configuration in candidate_list:
        configuration.validate()
        if configuration.name in names:
            raise ValueError("duplicate candidate name %r" % configuration.name)
        names.add(configuration.name)

    tenant_plans, outcomes, used, options = _allocate(
        units, contract_map, candidate_list, budget
    )
    decisions = [d for plan in tenant_plans for d in plan.decisions]
    shortfalls = _shortfalls(
        units, contract_map, candidate_list, outcomes, tenant_plans, options
    )
    return RollupPlan(
        budget,
        used,
        tenant_plans,
        decisions,
        outcomes,
        shortfalls,
    )


def merge_without_plan(target, *sketches):
    """Ordinary merge path used when no plan/contract is supplied.

    Delegates to the existing sketch implementation and performs no
    certification or budget accounting.
    """
    for sketch in sketches:
        target.merge(sketch)
    return target
