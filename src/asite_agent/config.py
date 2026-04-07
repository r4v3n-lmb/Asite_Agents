from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class Settings:
    gendesk_base_url: str
    gendesk_api_key: str
    gendesk_email: str
    gendesk_auth_header: str
    gendesk_auth_prefix: str
    gendesk_ticket_get_path_template: str
    gendesk_ticket_update_path_template: str
    gendesk_ticket_list_path: str
    gendesk_ticket_list_limit: int
    asite_login_url: str
    asite_email: str
    asite_password: str
    pdf_path: Path
    dry_run: bool
    slack_webhook_url: str
    slack_channel: str
    slack_notify_decisions: bool
    slack_signing_secret: str
    dashboard_public_base_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        base_dir = Path.cwd()
        return cls(
            gendesk_base_url=(
                os.getenv("GENDESK_BASE_URL", os.getenv("ZENDESK_BASE_URL", "")).rstrip("/")
            ),
            gendesk_api_key=os.getenv("GENDESK_API_KEY", os.getenv("ZENDESK_API_TOKEN", "")),
            gendesk_email=os.getenv("GENDESK_EMAIL", os.getenv("ZENDESK_EMAIL", "")),
            gendesk_auth_header=os.getenv("GENDESK_AUTH_HEADER", "Authorization"),
            gendesk_auth_prefix=os.getenv("GENDESK_AUTH_PREFIX", "Bearer "),
            gendesk_ticket_get_path_template=os.getenv(
                "GENDESK_TICKET_GET_PATH_TEMPLATE", "/api/v2/tickets/{ticket_id}.json"
            ),
            gendesk_ticket_update_path_template=os.getenv(
                "GENDESK_TICKET_UPDATE_PATH_TEMPLATE", "/api/v2/tickets/{ticket_id}.json"
            ),
            gendesk_ticket_list_path=os.getenv(
                "GENDESK_TICKET_LIST_PATH", "/api/v2/tickets.json?per_page=25&sort_by=updated_at"
            ),
            gendesk_ticket_list_limit=int(os.getenv("GENDESK_TICKET_LIST_LIMIT", "25")),
            asite_login_url=os.getenv("ASITE_LOGIN_URL", ""),
            asite_email=os.getenv("ASITE_EMAIL", ""),
            asite_password=os.getenv("ASITE_PASSWORD", ""),
            pdf_path=base_dir
            / os.getenv("ASITE_API_OVERVIEW_PDF", "Asite-API_Services_Overview.pdf"),
            dry_run=_to_bool(os.getenv("DRY_RUN"), True),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            slack_channel=os.getenv("SLACK_CHANNEL", ""),
            slack_notify_decisions=_to_bool(os.getenv("SLACK_NOTIFY_DECISIONS"), False),
            slack_signing_secret=os.getenv("SLACK_SIGNING_SECRET", ""),
            dashboard_public_base_url=os.getenv("DASHBOARD_PUBLIC_BASE_URL", ""),
        )
