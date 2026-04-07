from __future__ import annotations

import base64
import json
import urllib.request
from typing import Any

from .models import Ticket


def _basic_auth_header(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class GendeskClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer ",
        ticket_get_path_template: str = "/api/v2/tickets/{ticket_id}.json",
        ticket_update_path_template: str = "/api/v2/tickets/{ticket_id}.json",
        ticket_list_path: str = "/api/v2/tickets.json?per_page=25&sort_by=updated_at",
        email: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.ticket_get_path_template = ticket_get_path_template
        self.ticket_update_path_template = ticket_update_path_template
        self.ticket_list_path = ticket_list_path
        self.email = email

    def _request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
        }
        if self.auth_header.lower() == "authorization-basic":
            headers["Authorization"] = _basic_auth_header(self.email, self.api_key)
        else:
            prefix = self.auth_prefix
            if prefix and not prefix.endswith(" "):
                prefix = f"{prefix} "
            headers[self.auth_header] = f"{prefix}{self.api_key}".strip()

        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url=url, method=method, headers=headers, data=body)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_ticket(self, ticket_id: int) -> Ticket:
        payload = self._request(self.ticket_get_path_template.format(ticket_id=ticket_id))
        t = payload.get("ticket", payload)
        return Ticket(
            id=int(t.get("id", ticket_id)),
            subject=t.get("subject") or t.get("title") or "",
            description=t.get("description") or t.get("message") or "",
            requester_email=t.get("requester", {}).get("email"),
            status=t.get("status"),
            priority=t.get("priority"),
            raw=t,
        )

    def list_tickets(self, limit: int = 25) -> list[Ticket]:
        payload = self._request(self.ticket_list_path)
        rows = (
            payload.get("tickets")
            or payload.get("items")
            or payload.get("data")
            or payload.get("results")
            or []
        )
        tickets: list[Ticket] = []
        for row in rows[:limit]:
            tickets.append(
                Ticket(
                    id=int(row.get("id", 0)),
                    subject=row.get("subject") or row.get("title") or "",
                    description=row.get("description") or row.get("message") or "",
                    requester_email=(row.get("requester") or {}).get("email"),
                    status=row.get("status"),
                    priority=row.get("priority"),
                    raw=row,
                )
            )
        return tickets

    def add_internal_note(self, ticket_id: int, message: str) -> None:
        # Default payload follows Zendesk-compatible format and can be changed later
        # if Gendesk uses a different update contract.
        payload = {"ticket": {"comment": {"body": message, "public": False}}}
        self._request(
            self.ticket_update_path_template.format(ticket_id=ticket_id),
            method="PUT",
            payload=payload,
        )
