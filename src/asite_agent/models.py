from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Ticket:
    id: int
    subject: str
    description: str
    requester_email: str | None = None
    status: str | None = None
    priority: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlannedAction:
    name: str
    reason: str
    uri_hint: str
    method: str = "GET"
    requires_write: bool = False


@dataclass
class PermissionDecision:
    approved: bool
    note: str = ""

