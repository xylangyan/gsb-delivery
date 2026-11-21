"""
pyexcel.date_cleaning.engine
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Classification, scoped confirmation and output preparation for receiving
ledger dates.
"""

from .dates import (
    BLANK,
    INVALID_CALENDAR_DATE,
    INVALID_DATE_TEXT,
    INVALID_VALUE,
    PLAIN_NUMBER,
    VALID_DATE,
    confirmation_value_matches,
    interpret_cached_formula_value,
    interpret_excel_value,
)
from .loader import load_delivery
from .models import (
    ACCEPTED,
    BLANK_CELL,
    CONFIRMATION_APPLIED,
    CONFIRMATION_NO_CHANGE,
    CONFIRMATION_REJECTED,
    FORMULA_WITHOUT_CACHE,
    INVALID_CACHED_VALUE,
    PLAIN_NUMBER as PLAIN_NUMBER_STATUS,
    SOURCE_CSV_TEXT,
    UNCONFIRMED_FORMULA_CACHE,
    ConfirmationResult,
)


ISSUE_REASONS = {
    UNCONFIRMED_FORMULA_CACHE: "formula cache has no matching confirmation",
    FORMULA_WITHOUT_CACHE: "formula has no cached value",
    BLANK_CELL: "date cell is genuinely empty",
    PLAIN_NUMBER_STATUS: "plain number is not a calendar date",
}


class CleaningRun:
    """A reproducible date-cleaning run over one or more deliveries."""

    def __init__(self, date_formats=None):
        self.date_formats = date_formats
        self.deliveries = []
        self.confirmation_results = []

    def add_delivery(self, path=None, delivery=None, **keywords):
        if delivery is None:
            delivery = load_delivery(path, **keywords)
        self.deliveries.append(delivery)
        self.classify(delivery)
        return delivery

    def classify(self, delivery):
        for record in delivery.records:
            self._classify_record(record)

    def _classify_record(self, record):
        if record.has_formula:
            if not record.has_cached_value:
                record.set_status(
                    FORMULA_WITHOUT_CACHE,
                    ISSUE_REASONS[FORMULA_WITHOUT_CACHE],
                    accepted=False,
                )
                return
            if record.cached_value is None:
                record.set_status(
                    FORMULA_WITHOUT_CACHE,
                    ISSUE_REASONS[FORMULA_WITHOUT_CACHE],
                    accepted=False,
                )
                return
            parsed, kind = interpret_cached_formula_value(
                record.cached_value,
                record.number_format,
                date_formats=self.date_formats,
                epoch=record.epoch,
            )
            if kind == VALID_DATE:
                record.date_value = parsed
                record.set_status(
                    UNCONFIRMED_FORMULA_CACHE,
                    ISSUE_REASONS[UNCONFIRMED_FORMULA_CACHE],
                    accepted=False,
                )
            elif kind == BLANK:
                record.set_status(
                    FORMULA_WITHOUT_CACHE,
                    ISSUE_REASONS[FORMULA_WITHOUT_CACHE],
                    accepted=False,
                )
            else:
                record.set_status(
                    INVALID_CACHED_VALUE,
                    "cached formula value is not a calendar date",
                    accepted=False,
                )
            return

        if record.source == SOURCE_CSV_TEXT:
            parsed, kind = interpret_excel_value(
                record.raw_value, None, date_formats=self.date_formats
            )
        else:
            parsed, kind = interpret_excel_value(
                record.raw_value,
                record.number_format,
                date_formats=self.date_formats,
                epoch=record.epoch,
            )

        if kind == VALID_DATE:
            record.date_value = parsed
            record.set_status(ACCEPTED, "valid literal date", accepted=True)
        elif kind == BLANK:
            record.set_status(
                BLANK_CELL, ISSUE_REASONS[BLANK_CELL], accepted=False
            )
        elif kind == PLAIN_NUMBER:
            record.set_status(
                PLAIN_NUMBER_STATUS,
                ISSUE_REASONS[PLAIN_NUMBER_STATUS],
                accepted=False,
            )
        elif kind == INVALID_CALENDAR_DATE:
            record.set_status(
                INVALID_CALENDAR_DATE,
                "text resembles a date but is not a calendar date",
                accepted=False,
            )
        elif kind == INVALID_VALUE:
            record.set_status(
                INVALID_VALUE, "value is not a date", accepted=False
            )
        else:
            record.set_status(
                INVALID_DATE_TEXT,
                "text is not a recognised calendar date",
                accepted=False,
            )

    def apply_confirmation(
        self, confirmation, target=None, target_version=None
    ):
        """Offer one confirmation to the intended delivery.

        The ledger version in the confirmation is authoritative.  A
        confirmation from one delivery therefore cannot promote a cell in
        another delivery, even when page, cell, formula and cache happen to
        look identical.

        :param confirmation: the finance confirmation
        :param target: optional :class:`Delivery` to which the confirmation
        :param str target_version: optional ledger version to target
        """

        if target is not None:
            target_version = target.version
        if target_version is None and confirmation.target_ledger_version:
            target_version = confirmation.target_ledger_version
        if target is None and confirmation.target_delivery:
            target = self._delivery_for_name(confirmation.target_delivery)
            if target is None:
                result = ConfirmationResult(
                    confirmation,
                    CONFIRMATION_REJECTED,
                    "no delivery named %s" % confirmation.target_delivery,
                )
                self.confirmation_results.append(result)
                return result
            if target_version is None:
                target_version = target.version

        if target_version is not None:
            if target_version != confirmation.ledger_version:
                target_delivery = target or self._delivery_for_version(
                    target_version
                )
                affected = []
                for record in target_delivery.records:
                    if (
                        record.sheet == confirmation.sheet
                        and record.cell == confirmation.cell
                    ):
                        reason = (
                            "confirmation is for ledger version %s, not %s"
                            % (confirmation.ledger_version, target_version)
                        )
                        record.add_history(
                            "confirmation_rejected",
                            record.status,
                            reason,
                            confirmation_id=confirmation.confirmation_id,
                        )
                        affected.append(record.record_id)
                result = ConfirmationResult(
                    confirmation,
                    CONFIRMATION_REJECTED,
                    "confirmation is for ledger version %s, not %s"
                    % (confirmation.ledger_version, target_version),
                    target_version=target_version,
                    affected_record_ids=affected,
                )
            else:
                delivery = target or self._delivery_for_version(target_version)
                result = self._apply_to_delivery(confirmation, delivery)
            self.confirmation_results.append(result)
            return result

        matching = [
            delivery
            for delivery in self.deliveries
            if delivery.version == confirmation.ledger_version
        ]
        if not matching:
            result = ConfirmationResult(
                confirmation,
                CONFIRMATION_REJECTED,
                "no delivery matches ledger version %s"
                % confirmation.ledger_version,
            )
        else:
            result = None
            for delivery in matching:
                result = self._apply_to_delivery(confirmation, delivery)
        self.confirmation_results.append(result)
        return result

    def _delivery_for_version(self, version):
        for delivery in self.deliveries:
            if delivery.version == version:
                return delivery
        raise ValueError("no delivery matches ledger version %s" % version)

    def _delivery_for_name(self, name):
        for delivery in self.deliveries:
            if delivery.name == name:
                return delivery
        return None

    def _apply_to_delivery(self, confirmation, delivery):
        candidates = [
            record
            for record in delivery.records
            if record.sheet == confirmation.sheet
            and record.cell == confirmation.cell
        ]
        if not candidates:
            return ConfirmationResult(
                confirmation,
                CONFIRMATION_REJECTED,
                "no date cell %s!%s exists in ledger version %s"
                % (
                    confirmation.sheet,
                    confirmation.cell,
                    confirmation.ledger_version,
                ),
                target_version=delivery.version,
            )

        affected = []
        outcome = None
        reason = None
        target = None

        if confirmation.shipment_no is None and len(candidates) > 1:
            for record in candidates:
                record.add_history(
                    "confirmation_rejected",
                    record.status,
                    "confirmation is ambiguous at %s!%s"
                    % (confirmation.sheet, confirmation.cell),
                    confirmation_id=confirmation.confirmation_id,
                )
                affected.append(record.record_id)
            return ConfirmationResult(
                confirmation,
                CONFIRMATION_REJECTED,
                "confirmation is ambiguous at %s!%s; include a shipment number"
                % (confirmation.sheet, confirmation.cell),
                target_version=delivery.version,
                affected_record_ids=affected,
            )

        for record in candidates:
            rejection = None
            if record.status == ACCEPTED:
                affected.append(record.record_id)
                outcome = outcome or CONFIRMATION_NO_CHANGE
                reason = reason or "record is already accepted"
                continue
            if (
                confirmation.shipment_no is not None
                and record.shipment_no != confirmation.shipment_no
            ):
                rejection = "shipment number does not match the confirmed cell"
            elif not record.has_formula:
                rejection = "confirmed cell is not a formula"
            elif not record.has_cached_value:
                rejection = "formula has no cached value to confirm"
            elif not confirmation_value_matches(
                confirmation.cached_value, record
            ):
                rejection = "confirmed cached value does not match the cell"
            elif record.date_value is None:
                rejection = "cached value is not a calendar date"

            if rejection is not None:
                record.add_history(
                    "confirmation_rejected",
                    record.status,
                    rejection,
                    confirmation_id=confirmation.confirmation_id,
                )
                affected.append(record.record_id)
                outcome = CONFIRMATION_REJECTED
                reason = rejection
                continue

            record.confirmation_id = confirmation.confirmation_id
            record.set_status(
                ACCEPTED,
                "formula cache confirmed",
                accepted=True,
                event="confirmation_applied",
                confirmation_id=confirmation.confirmation_id,
            )
            target = record
            affected.append(record.record_id)
            outcome = CONFIRMATION_APPLIED
            reason = "confirmed cached formula value accepted"

        if outcome is None:
            outcome = CONFIRMATION_REJECTED
            reason = "confirmation did not apply"
        return ConfirmationResult(
            confirmation,
            outcome,
            reason,
            target_version=delivery.version,
            record=target,
            affected_record_ids=affected,
        )

    def apply_confirmations(self, confirmations):
        return [
            self.apply_confirmation(confirmation)
            for confirmation in confirmations
        ]

    def accepted_records(self, delivery=None):
        collections = [delivery] if delivery is not None else self.deliveries
        records = []
        for item in collections:
            records.extend(
                record for record in item.records if record.accepted
            )
        return records

    def issues(self, delivery=None):
        collections = [delivery] if delivery is not None else self.deliveries
        issues = []
        for item in collections:
            issues.extend(
                record for record in item.records if not record.accepted
            )
        return issues

    def reconciliation(self):
        return [
            result.to_dict() for result in self.confirmation_results
        ]
