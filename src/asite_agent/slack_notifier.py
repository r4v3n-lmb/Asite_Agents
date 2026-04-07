from __future__ import annotations

import json
import urllib.request
from typing import Any


class SlackNotifier:
    def __init__(self, webhook_url: str, channel: str = "") -> None:
        self.webhook_url = webhook_url.strip()
        self.channel = channel.strip()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        payload: dict[str, Any] = {"text": text}
        if self.channel:
            payload["channel"] = self.channel
        self._post(payload)

    def send_action_request(
        self,
        *,
        request_id: str,
        ticket_id: int,
        subject: str,
        action_name: str,
        method: str,
        status: str,
        review_url: str,
    ) -> None:
        if not self.enabled:
            return
        payload: dict[str, Any] = {
            "text": f"Asite action request for ticket #{ticket_id}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":rotating_light: *Asite Action Request*\n"
                            f"*Ticket:* #{ticket_id}\n"
                            f"*Subject:* {subject}\n"
                            f"*Action:* `{action_name}` ({method})\n"
                            f"*Status:* {status}\n"
                            f"*Request ID:* `{request_id}`"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "style": "primary",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "action_id": "approve_request",
                            "value": json.dumps(
                                {"request_id": request_id, "approved": True, "post_note": True}
                            ),
                        },
                        {
                            "type": "button",
                            "style": "danger",
                            "text": {"type": "plain_text", "text": "Decline"},
                            "action_id": "decline_request",
                            "value": json.dumps(
                                {"request_id": request_id, "approved": False, "post_note": False}
                            ),
                        },
                    ],
                },
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"<{review_url}|Open dashboard>"}
                    ],
                },
            ],
        }
        if self.channel:
            payload["channel"] = self.channel
        self._post(payload)

    def _post(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            method="POST",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20):
            return
