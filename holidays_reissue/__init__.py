# Overseas quote re-issue: booking and dispatch engine.
#
# Built on top of the holidays library. This module derives the UTC intervals
# where both the exchange side and the custody side are genuinely open
# (weekends, fixed holidays, ad-hoc suspensions and makeup workdays included),
# then computes a minimum-total-fee booking set that stays feasible under every
# explicitly permitted announcement candidate. Jobs are dispatched only as the
# governing announcements actually arrive.
#
# Work-order guarantees encoded here:
#   * Each side owns an independent HolidayBase instance even when two composite
#     calendars share the same exchange base. Querying a future year on one
#     composite calendar expands only its own source calendar; the years already
#     claimed (populated) on the other composite calendar, and the operating dates
#     derived from them, never change as a result.
#   * An announcement candidate is an immutable snapshot (frozenset of source
#     dates plus the ad-hoc suspension and makeup changes that take effect with
#     it). Once a snapshot drives a booking or dispatch decision it is never
#     rewritten.
#   * For any original UTC instant the local date on each side, the original
#     holiday source and continuous-work feasibility can be inspected on site.
#     Cross-midnight windows and DST fall-back repeated local hours use the
#     fold endpoint marked on the work order.
#   * Bookings are expressed as a continuous number segment at fixed daily
#     instants. Announcements only touch a handful of source years, so the
#     long-dated ledger never has to be rearranged minute by minute.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from holidays import HolidayBase, financial_holidays

__all__ = (
    "AnnouncementCandidate",
    "Booking",
    "BookingLedger",
    "CalendarSide",
    "DispatchDecision",
    "FeasibleInterval",
    "IndistinguishableAnnouncementsError",
    "ReissueJob",
    "ScheduleAmendment",
    "WindowSpec",
    "compatible_booking",
    "feasible_intervals",
    "inspect_moment",
    "plan_robust_bookings",
    "run_dispatch",
)


@dataclass(frozen=True)
class ScheduleAmendment:
    """Single-day ad-hoc changes that take effect together with a candidate.

    suspended_dates: ad-hoc closure days (the site is shut), retaining the
        original holiday source name for on-site verification.
    makeup_workdates: makeup workdays (a date that would otherwise be
        non-working is declared open).
    """

    suspended_dates: frozenset[date] = frozenset()
    makeup_workdates: frozenset[date] = frozenset()


@dataclass(frozen=True)
class AnnouncementCandidate:
    """An explicitly permitted announcement candidate (immutable snapshot).

    announced_dates: source closure dates this candidate predicts; used to tell
        identically named holidays apart once a distinguishing source arrives.
    amendments: ad-hoc suspensions and makeup changes effective for this
        candidate.
    """

    code: str
    announced_dates: frozenset[date] = frozenset()
    amendments: ScheduleAmendment = field(default_factory=ScheduleAmendment)

    def __post_init__(self) -> None:
        overlap = self.amendments.suspended_dates & self.amendments.makeup_workdates
        if overlap:
            raise ValueError(
                f"Candidate {self.code} both suspends and makes up the same date(s): "
                f"{sorted(overlap)}"
            )


@dataclass(frozen=True)
class WindowSpec:
    """Recurring daily opening window expressed in local wall-clock time.

    A cross-midnight window is expressed with open later than close. On a DST
    fall-back day the repeated local hour resolves through fold: 0 selects the
    first (daylight-saving) occurrence and 1 the second (standard-time) one.
    """

    open: time
    close: time
    fold: int = 0

    def __post_init__(self) -> None:
        if self.fold not in (0, 1):
            raise ValueError("fold must be either 0 or 1")
        if self.open == self.close:
            raise ValueError("window open and close must differ")


class CalendarSide:
    """Opening calendar and daily window for one site (exchange or custody).

    Every site holds its own HolidayBase instance. Querying a new year on one
    composite calendar only expands that instance; another composite calendar
    sharing the same base class, and the years it already claimed, is untouched.
    """

    def __init__(
        self,
        code: str,
        tzname: str,
        window: WindowSpec,
        calendar: HolidayBase,
        *,
        label: str | None = None,
    ) -> None:
        self.code = code
        self.label = label or code
        self.tz = ZoneInfo(tzname)
        self.window = window
        self.calendar = calendar

    @classmethod
    def from_financial_market(
        cls,
        market: str,
        tzname: str,
        window: WindowSpec,
        *,
        label: str | None = None,
        language: str | None = None,
    ) -> CalendarSide:
        # Independent instance; with expand=True a query lazily adds years to
        # this instance's own source calendar. language=None keeps the entity's
        # native names and avoids depending on a compiled translation catalog.
        calendar = financial_holidays(market, language=language)
        return cls(market, tzname, window, calendar, label=label)

    def _ensure_year(self, year: int) -> None:
        if year not in self.calendar.years:
            self.calendar.years.add(year)
            self.calendar._populate(year)

    def local_date(self, moment: datetime) -> date:
        """On-site check: the actual local date for a UTC instant on this side."""
        return moment.astimezone(self.tz).date()

    def holiday_source(self, day: date) -> str | None:
        """On-site check: the closure name in the source calendar for a local date."""
        self._ensure_year(day.year)
        return self.calendar.get(day)

    def is_open_day(self, day: date, amendment: ScheduleAmendment) -> bool:
        """Whether this local date is open under the given candidate changes.

        Makeup work takes precedence (a weekend or source closure declared open
        by the governing announcement), followed by ad-hoc suspensions.
        """
        if day in amendment.makeup_workdates:
            return True
        if day in amendment.suspended_dates:
            return False
        self._ensure_year(day.year)
        return self.calendar.is_working_day(day)

    def _window_bounds(self, day: date) -> tuple[datetime, datetime]:
        start_local = datetime.combine(day, self.window.open, tzinfo=self.tz)
        end_day = day if self.window.close > self.window.open else day + timedelta(days=1)
        end_local = datetime.combine(end_day, self.window.close, tzinfo=self.tz)
        # Resolve fall-back repeated endpoints via the marked fold (the end of a
        # cross-midnight window lands on end_day).
        start_local = start_local.replace(fold=self.window.fold)
        end_local = end_local.replace(fold=self.window.fold)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def open_windows_utc(
        self, days: list[date], amendment: ScheduleAmendment
    ) -> list[tuple[datetime, datetime, date]]:
        """Real UTC opening windows for the given local dates.

        Cross-midnight spans and the marked fall-back fold are honored; closed
        days produce no window.
        """
        return [
            (*self._window_bounds(day), day) for day in days if self.is_open_day(day, amendment)
        ]


def _intersect(
    left: tuple[datetime, datetime], right: tuple[datetime, datetime]
) -> tuple[datetime, datetime] | None:
    start = max(left[0], right[0])
    end = min(left[1], right[1])
    return (start, end) if start < end else None


def inspect_moment(
    moment: datetime,
    sides: tuple[CalendarSide, CalendarSide],
    candidates: list[AnnouncementCandidate],
) -> dict:
    """On-site check for one original UTC instant.

    Reports each side's local date, original holiday source, UTC opening window
    and whether it is open under every candidate, so a dispatch snapshot can be
    reconciled manually against the two real sites.
    """
    if moment.tzinfo is None:
        raise ValueError("the UTC instant must be timezone-aware")
    moment = moment.astimezone(UTC)
    report: dict = {"moment_utc": moment.isoformat(), "sides": {}}
    for side in sides:
        day = side.local_date(moment)
        window_start, window_end = side._window_bounds(day)
        report["sides"][side.code] = {
            "local_date": day.isoformat(),
            "holiday_source": side.holiday_source(day),
            "window_utc": (window_start.isoformat(), window_end.isoformat()),
            "open_by_candidate": {
                candidate.code: side.is_open_day(day, candidate.amendments)
                for candidate in candidates
            },
        }
    return report


@dataclass(frozen=True)
class ReissueJob:
    """A re-issue job: it must be completed once, continuously, inside both real
    opening windows."""

    job_id: str
    duration: timedelta
    earliest_utc: datetime
    latest_utc: datetime

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0):
            raise ValueError(f"job {self.job_id} duration must be positive")
        if self.earliest_utc.tzinfo is None or self.latest_utc.tzinfo is None:
            raise ValueError(f"job {self.job_id} time bounds must be timezone-aware")
        if self.earliest_utc >= self.latest_utc:
            raise ValueError(f"job {self.job_id} has invalid time bounds")


@dataclass(frozen=True)
class Booking:
    """A booked concrete UTC interval (one booking serves at most one job)."""

    booking_no: int
    start_utc: datetime
    end_utc: datetime
    fee: int

    def __post_init__(self) -> None:
        if self.start_utc.tzinfo is None or self.end_utc.tzinfo is None:
            raise ValueError("booking interval must be timezone-aware")
        if self.start_utc >= self.end_utc:
            raise ValueError("booking end must be later than its start")
        if self.fee < 0:
            raise ValueError("booking fee cannot be negative")


@dataclass(frozen=True)
class FeasibleInterval:
    """Feasible start range [lower, upper] (UTC) of a job within one booking."""

    booking: Booking
    lower: datetime
    upper: datetime


def _local_days_between(
    exchange: CalendarSide,
    warehouse: CalendarSide,
    horizon_start: datetime,
    horizon_end: datetime,
) -> list[date]:
    first_day = min(exchange.local_date(horizon_start), warehouse.local_date(horizon_start))
    last_day = max(exchange.local_date(horizon_end), warehouse.local_date(horizon_end))
    span = (last_day - first_day).days
    return [first_day + timedelta(days=offset) for offset in range(span + 1)]


def feasible_intervals(
    job: ReissueJob,
    exchange: CalendarSide,
    warehouse: CalendarSide,
    candidate: AnnouncementCandidate,
    bookings: list[Booking],
) -> dict[int, list[FeasibleInterval]]:
    """Feasible start ranges of the job within each booking for one candidate.

    The job's full continuous duration must sit inside the intersection of the
    two sides' real opening windows; cross-midnight and fall-back handling comes
    from CalendarSide. A booking overlapping several disjoint window
    intersections yields several ranges that are never merged.
    """
    if not bookings:
        return {}
    horizon_start = min([job.earliest_utc, *(booking.start_utc for booking in bookings)])
    horizon_end = max([job.latest_utc, *(booking.end_utc for booking in bookings)])
    days = _local_days_between(exchange, warehouse, horizon_start, horizon_end)

    exchange_windows = exchange.open_windows_utc(days, candidate.amendments)
    warehouse_windows = warehouse.open_windows_utc(days, candidate.amendments)

    intersections: list[tuple[datetime, datetime]] = []
    for ex_start, ex_end, _ in exchange_windows:
        for wh_start, wh_end, _ in warehouse_windows:
            overlap = _intersect((ex_start, ex_end), (wh_start, wh_end))
            if overlap and overlap[1] - overlap[0] >= job.duration:
                intersections.append(overlap)

    result: dict[int, list[FeasibleInterval]] = {}
    for booking in bookings:
        intervals = [
            FeasibleInterval(
                booking,
                max(job.earliest_utc, booking.start_utc, window_start),
                min(job.latest_utc, booking.end_utc, window_end) - job.duration,
            )
            for window_start, window_end in intersections
            if max(job.earliest_utc, booking.start_utc, window_start)
            <= min(job.latest_utc, booking.end_utc, window_end) - job.duration
        ]
        if intervals:
            result[booking.booking_no] = intervals
    return result


def compatible_booking(
    jobs: list[ReissueJob],
    exchange: CalendarSide,
    warehouse: CalendarSide,
    candidate: AnnouncementCandidate,
    bookings: list[Booking],
) -> dict[str, FeasibleInterval] | None:
    """Whether the bookings let every job run once under one candidate.

    Solves the job-to-booking bipartite matching with Kuhn augmenting paths;
    returns None when infeasible. Each matched job gets its earliest feasible
    start range in the matched booking.
    """
    intervals_by_job: dict[str, dict[int, list[FeasibleInterval]]] = {}
    for job in jobs:
        intervals = feasible_intervals(job, exchange, warehouse, candidate, bookings)
        if not intervals:
            return None
        intervals_by_job[job.job_id] = intervals

    matched_booking: dict[int, str] = {}

    def augment(job_id: str, visited: set[int]) -> bool:
        for booking_no in sorted(intervals_by_job[job_id]):
            if booking_no in visited:
                continue
            visited.add(booking_no)
            holder = matched_booking.get(booking_no)
            if holder is None or augment(holder, visited):
                matched_booking[booking_no] = job_id
                return True
        return False

    for job in sorted(jobs, key=lambda item: item.job_id):
        if not augment(job.job_id, set()):
            return None

    return {
        job_id: min(intervals_by_job[job_id][booking_no], key=lambda item: item.lower)
        for booking_no, job_id in matched_booking.items()
    }


def _validate_common_bookings(
    candidates: list[AnnouncementCandidate],
    candidate_bookings: dict[str, list[Booking]],
) -> list[Booking]:
    """A booking number must denote the same UTC interval and fee for every
    candidate."""
    reference = {b.booking_no: b for b in candidate_bookings[candidates[0].code]}
    common_ids = set(reference)
    for candidate in candidates[1:]:
        view = {b.booking_no: b for b in candidate_bookings[candidate.code]}
        common_ids &= set(view)
        for booking_no in common_ids:
            if view[booking_no] != reference[booking_no]:
                raise ValueError(
                    f"booking {booking_no} differs under candidate {candidate.code} "
                    "from the first candidate's interval/fee"
                )
    return [reference[no] for no in sorted(common_ids)]


def plan_robust_bookings(
    jobs: list[ReissueJob],
    exchange: CalendarSide,
    warehouse: CalendarSide,
    candidates: list[AnnouncementCandidate],
    candidate_bookings: dict[str, list[Booking]],
) -> tuple[int, list[Booking]]:
    """Minimum total fee booking set that completes every job under all
    permitted announcements.

    Only booking numbers present under every candidate enter the robust set, and
    a number must keep the same UTC interval and fee everywhere. On an equal fee
    the earliest number segment in the given booking order wins. A selected
    booking is treated as signed and its UTC interval can never be altered by a
    later announcement.
    """
    if not candidates:
        raise ValueError("at least one announcement candidate is required")
    codes = {c.code for c in candidates}
    if unknown := set(candidate_bookings) - codes:
        raise ValueError(f"bookings supplied for unregistered candidates: {sorted(unknown)}")
    missing = codes - set(candidate_bookings)
    if missing:
        raise ValueError(f"candidates missing booking data: {sorted(missing)}")

    ordered = sorted(candidates, key=lambda candidate: candidate.code)
    common = _validate_common_bookings(ordered, candidate_bookings)
    min_count = len(jobs)

    # Keep only bookings that are feasible for at least one job under every
    # candidate. Announcements touch a few source years only, so most entries in
    # a long-dated ledger are irrelevant; localizing first avoids reshuffling it.
    relevant_ids: set[int] = set()
    for job in jobs:
        ids: set[int] | None = None
        for candidate in ordered:
            candidate_ids = set(feasible_intervals(job, exchange, warehouse, candidate, common))
            ids = candidate_ids if ids is None else ids & candidate_ids
        if ids:
            relevant_ids |= ids
    pool = sorted(
        (booking for booking in common if booking.booking_no in relevant_ids),
        key=lambda booking: booking.booking_no,
    )
    if len(pool) < min_count:
        raise IndistinguishableAnnouncementsError.build(
            ordered, jobs, exchange, warehouse, candidate_bookings
        )

    best_fee: int | None = None
    best_set: tuple[int, ...] | None = None
    pool_by_no = {booking.booking_no: booking for booking in pool}
    suffix_fees = [sorted(booking.fee for booking in pool[index:]) for index in range(len(pool))]

    def search(index: int, chosen: list[int], partial_fee: int) -> None:
        nonlocal best_fee, best_set
        if len(chosen) + (len(pool) - index) < min_count:
            return
        needed = min_count - len(chosen)
        if needed > 0:
            fee_lower_bound = sum(suffix_fees[index][:needed])
            if best_fee is not None and partial_fee + fee_lower_bound >= best_fee:
                return
        if len(chosen) >= min_count:
            selected = [pool_by_no[no] for no in chosen]
            if all(
                compatible_booking(jobs, exchange, warehouse, candidate, selected) is not None
                for candidate in ordered
            ):
                candidate_set = tuple(sorted(chosen))
                if (
                    best_fee is None
                    or partial_fee < best_fee
                    or (partial_fee == best_fee and candidate_set < (best_set or ()))
                ):
                    best_fee = partial_fee
                    best_set = candidate_set
            # Adding more bookings only raises the fee, so never extend here.
            return
        for next_index in range(index, len(pool)):
            booking = pool[next_index]
            search(
                next_index + 1,
                [*chosen, booking.booking_no],
                partial_fee + booking.fee,
            )

    search(0, [], 0)
    if best_set is None:
        raise IndistinguishableAnnouncementsError.build(
            ordered, jobs, exchange, warehouse, candidate_bookings
        )
    selected = [pool_by_no[no] for no in best_set]
    return sum(booking.fee for booking in selected), selected


class IndistinguishableAnnouncementsError(RuntimeError):
    """Raised when no uniform booking exists; names the announcements that
    cannot be told apart and the real jobs that cannot be reconciled."""

    def __init__(self, message: str, details: dict) -> None:
        super().__init__(message)
        self.details = details

    @classmethod
    def build(
        cls,
        ordered_candidates: list[AnnouncementCandidate],
        jobs: list[ReissueJob],
        exchange: CalendarSide,
        warehouse: CalendarSide,
        candidate_bookings: dict[str, list[Booking]],
    ) -> IndistinguishableAnnouncementsError:
        per_candidate: dict[str, dict[str, dict[int, list[FeasibleInterval]]]] = {}
        for candidate in ordered_candidates:
            per_candidate[candidate.code] = {
                job.job_id: feasible_intervals(
                    job,
                    exchange,
                    warehouse,
                    candidate,
                    candidate_bookings[candidate.code],
                )
                for job in jobs
            }

        undifferentiated: list[tuple[str, str, date]] = []
        for index, candidate in enumerate(ordered_candidates):
            for other in ordered_candidates[index + 1 :]:
                undifferentiated.extend(
                    (candidate.code, other.code, day)
                    for day in sorted(candidate.announced_dates & other.announced_dates)
                )

        # A job is uniformly bookable only if one booking number works under
        # every candidate; otherwise it is reported as unreconcilable.
        unbookable: list[str] = []
        for job in jobs:
            common_booking_ids: set[int] | None = None
            for candidate in ordered_candidates:
                ids = set(per_candidate[candidate.code][job.job_id])
                if common_booking_ids is None:
                    common_booking_ids = ids
                else:
                    common_booking_ids &= ids
            if not common_booking_ids:
                unbookable.append(job.job_id)
        unbookable.sort()
        details = {
            "candidate_codes": [candidate.code for candidate in ordered_candidates],
            "undifferentiated_pairs": undifferentiated,
            "unbookable_jobs": unbookable,
            "per_candidate_intervals": per_candidate,
        }
        message = (
            "No uniform booking completes every job under all permitted "
            f"announcements; undifferentiated announcement pairs and identically "
            f"named closure dates: {undifferentiated or 'none'}; jobs without a "
            f"feasible interval under at least one announcement: "
            f"{unbookable or 'none'}."
        )
        return cls(message, details)


@dataclass(frozen=True)
class DispatchDecision:
    """Snapshot of one dispatch decision."""

    sequence: int
    job_id: str
    booking: Booking
    start_utc: datetime
    end_utc: datetime
    used_candidate_code: str
    evidence_sources: tuple[tuple[str, str, str | None], ...]
    local_dates: tuple[tuple[str, date], ...]


class BookingLedger:
    """Long-dated booking ledger: a continuous number segment at fixed daily
    instants; a signed interval can never be altered later."""

    def __init__(self, bookings: list[Booking]) -> None:
        numbers = sorted(booking.booking_no for booking in bookings)
        if numbers and numbers != list(range(numbers[0], numbers[-1] + 1)):
            raise ValueError("booking numbers must form a continuous segment")
        self._bookings = {booking.booking_no: booking for booking in bookings}
        self._signed: dict[int, DispatchDecision] = {}

    @property
    def bookings(self) -> list[Booking]:
        return [self._bookings[no] for no in sorted(self._bookings)]

    @property
    def signed_numbers(self) -> tuple[int, ...]:
        return tuple(sorted(self._signed))

    def sign(self, decision: DispatchDecision) -> None:
        booking = self._bookings.get(decision.booking.booking_no)
        if booking is None:
            raise KeyError(f"booking {decision.booking.booking_no} is not in the ledger")
        if decision.booking != booking:
            raise ValueError("signed interval does not match the ledger; changes are forbidden")
        existing = self._signed.get(booking.booking_no)
        if existing is not None and existing != decision:
            raise ValueError(f"booking {booking.booking_no} is already signed to another job")
        self._signed[booking.booking_no] = decision

    def decision_for(self, booking_no: int) -> DispatchDecision | None:
        return self._signed.get(booking_no)


def _intersect_intervals(
    per_candidate_views: list[dict[int, list[FeasibleInterval]]],
) -> dict[int, list[FeasibleInterval]]:
    """Keep start ranges supported by every candidate for the same booking.

    Candidate ranges for one booking are intersected pairwise (bounds taken
    together); any range a candidate cannot cover is dropped. The returned
    ranges are feasible under every still-plausible candidate.
    """
    common: dict[int, list[FeasibleInterval]] = {
        booking_no: list(intervals) for booking_no, intervals in per_candidate_views[0].items()
    }
    for view in per_candidate_views[1:]:
        next_common: dict[int, list[FeasibleInterval]] = {}
        for booking_no, base_intervals in common.items():
            other_intervals = view.get(booking_no)
            if not other_intervals:
                continue
            kept: list[FeasibleInterval] = []
            for base in base_intervals:
                for other in other_intervals:
                    lower = max(base.lower, other.lower)
                    upper = min(base.upper, other.upper)
                    if lower <= upper:
                        kept.append(FeasibleInterval(base.booking, lower, upper))
            if kept:
                next_common[booking_no] = kept
        common = next_common
    return common


def run_dispatch(
    jobs: list[ReissueJob],
    exchange: CalendarSide,
    warehouse: CalendarSide,
    candidates: list[AnnouncementCandidate],
    ledger: BookingLedger,
    announcement_sequence: list[frozenset[date]],
) -> list[DispatchDecision]:
    """Dispatch jobs step by step according to the announcements as they are
    actually visible.

    At each step only the original sources and dates already announced can be
    used. The candidates still consistent with every announced fact form the
    admitted set; unannounced differences cannot split two usages early. Each
    pending job must be feasible under all admitted candidates, so their start
    ranges are intersected and the earliest start, then the lowest booking
    number in the given order, is chosen. Signed intervals stay unchanged.
    """
    if not announcement_sequence:
        raise ValueError("the announcement sequence is empty")

    pending = {job.job_id: job for job in jobs}
    decisions: list[DispatchDecision] = []
    seen: frozenset[date] = frozenset()

    for sequence, announced in enumerate(announcement_sequence, start=1):
        seen = seen | announced
        # Consistency: announced facts must be a subset of the dates a candidate
        # predicts. A candidate may still hold unannounced differences and remain
        # credible until a distinguishing fact it did not predict rules it out.
        plausible = [candidate for candidate in candidates if seen <= candidate.announced_dates]
        if not plausible:
            raise IndistinguishableAnnouncementsError(
                f"after announcement {sequence} no candidate is consistent with the facts",
                {
                    "sequence": sequence,
                    "seen": sorted(seen),
                    "candidate_codes": [candidate.code for candidate in candidates],
                },
            )

        free_bookings = [
            booking
            for booking in ledger.bookings
            if booking.booking_no not in ledger.signed_numbers
        ]

        def raise_dispatch_failure() -> None:
            # Decision-point report: list every remaining job that has no common
            # interval under the currently admitted candidates.
            blocked: list[str] = []
            for blocked_job in sorted(pending.values(), key=lambda item: item.job_id):
                views = [
                    feasible_intervals(blocked_job, exchange, warehouse, candidate, free_bookings)
                    for candidate in plausible
                ]
                if not views[0] or not _intersect_intervals(views):
                    blocked.append(blocked_job.job_id)
            failure = IndistinguishableAnnouncementsError.build(
                plausible,
                [job for job in jobs if job.job_id in blocked],
                exchange,
                warehouse,
                {candidate.code: free_bookings for candidate in plausible},
            )
            failure.details["sequence"] = sequence
            raise failure

        for job in sorted(pending.values(), key=lambda item: item.job_id):
            per_candidate: list[dict[int, list[FeasibleInterval]]] = []
            for candidate in plausible:
                intervals = feasible_intervals(job, exchange, warehouse, candidate, free_bookings)
                if not intervals:
                    raise_dispatch_failure()
                per_candidate.append(intervals)

            common = _intersect_intervals(per_candidate)
            if not common:
                raise_dispatch_failure()
            chosen_interval = min(
                (interval for intervals in common.values() for interval in intervals),
                key=lambda item: (item.lower, item.booking.booking_no),
            )
            start_utc = chosen_interval.lower

            evidence = tuple(
                (
                    side.code,
                    side.local_date(start_utc).isoformat(),
                    side.holiday_source(side.local_date(start_utc)),
                )
                for side in (exchange, warehouse)
            )
            local_dates = tuple(
                (side.code, side.local_date(start_utc)) for side in (exchange, warehouse)
            )
            decision = DispatchDecision(
                sequence=sequence,
                job_id=job.job_id,
                booking=chosen_interval.booking,
                start_utc=start_utc,
                end_utc=start_utc + job.duration,
                used_candidate_code=(
                    plausible[0].code
                    if len(plausible) == 1
                    else f"UNRESOLVED:{','.join(candidate.code for candidate in plausible)}"
                ),
                evidence_sources=evidence,
                local_dates=local_dates,
            )
            ledger.sign(decision)
            decisions.append(decision)
            pending.pop(job.job_id)
            # Remove the signed interval immediately so one booking serves at
            # most one job.
            free_bookings = [
                booking
                for booking in free_bookings
                if booking.booking_no != chosen_interval.booking.booking_no
            ]

    if pending:
        raise IndistinguishableAnnouncementsError(
            "jobs remain undispatched after the announcement sequence ended",
            {"pending_jobs": sorted(pending)},
        )
    return decisions
