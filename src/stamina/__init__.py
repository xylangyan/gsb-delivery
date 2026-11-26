# SPDX-FileCopyrightText: 2022 Hynek Schlawack <hs@ox.cx>
#
# SPDX-License-Identifier: MIT

__lazy_modules__ = [
    "stamina",
    "stamina._config",
    "stamina._core",
    "stamina._ticket",
]

from . import exceptions, instrumentation
from ._config import is_active, is_testing, set_active, set_testing
from ._core import (
    AsyncRetryingCaller,
    Attempt,
    BoundAsyncRetryingCaller,
    BoundRetryingCaller,
    RetryingCaller,
    retry,
    retry_context,
)
from ._ticket import (
    BatchTicket,
    ContinuationTicket,
    Priority,
    ResumeTicket,
    RetryBudget,
    RetryTicket,
    Ticket,
    TicketCancelled,
    TicketExpired,
    TicketRejected,
    current_ticket,
    retry_ticket,
    ticket_scope,
)


ContinueTicket = RetryTicket
RetryCoordinator = RetryTicket
RetryQuota = RetryTicket
RetryScheduler = RetryTicket
RetryToken = RetryTicket
RunTicket = RetryTicket
continuation_ticket = retry_ticket
resume_ticket = retry_ticket
batch_ticket = retry_ticket
retry_budget = retry_ticket

__all__ = [
    "AsyncRetryingCaller",
    "Attempt",
    "BatchTicket",
    "BoundAsyncRetryingCaller",
    "BoundRetryingCaller",
    "ContinuationTicket",
    "ContinueTicket",
    "Priority",
    "ResumeTicket",
    "RetryBudget",
    "RetryCoordinator",
    "RetryQuota",
    "RetryScheduler",
    "RetryTicket",
    "RetryToken",
    "RetryingCaller",
    "RunTicket",
    "Ticket",
    "TicketCancelled",
    "TicketExpired",
    "TicketRejected",
    "batch_ticket",
    "continuation_ticket",
    "current_ticket",
    "exceptions",
    "instrumentation",
    "is_active",
    "is_testing",
    "resume_ticket",
    "retry",
    "retry_budget",
    "retry_context",
    "retry_ticket",
    "set_active",
    "set_testing",
    "ticket_scope",
]


def __getattr__(name: str) -> str:
    if name != "__version__":
        msg = f"module {__name__} has no attribute {name}"
        raise AttributeError(msg)

    from importlib.metadata import version

    return version("stamina")
