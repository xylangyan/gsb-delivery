# SPDX-FileCopyrightText: 2022 Hynek Schlawack <hs@ox.cx>
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from . import _ticket as _ticket_module
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
    TicketRequest,
    TicketUsage,
    current_ticket,
    retry_ticket,
    ticket_scope,
)


def __getattr__(name: str) -> object:
    return getattr(_ticket_module, name)


__all__ = [
    "BatchTicket",
    "ContinuationTicket",
    "Priority",
    "ResumeTicket",
    "RetryBudget",
    "RetryTicket",
    "Ticket",
    "TicketCancelled",
    "TicketExpired",
    "TicketRejected",
    "TicketRequest",
    "TicketUsage",
    "current_ticket",
    "retry_ticket",
    "ticket_scope",
]
