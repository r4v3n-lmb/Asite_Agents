from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .asite_client import AsiteClient
from .config import Settings
from .pdf_catalog import load_or_build_catalog
from .ticket_sources import GendeskClient
from .workflow import AsiteSupportWorkflow


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Asite support workflow agent")
    parser.add_argument("--ticket-id", type=int, required=True, help="Gendesk ticket id")
    parser.add_argument("--post-note", action="store_true", help="Post internal note back to ticket")
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    _load_dotenv(Path(args.env_file))
    settings = Settings.from_env()

    catalog = load_or_build_catalog(settings.pdf_path)
    gendesk = GendeskClient(
        base_url=settings.gendesk_base_url,
        api_key=settings.gendesk_api_key,
        email=settings.gendesk_email,
        auth_header=settings.gendesk_auth_header,
        auth_prefix=settings.gendesk_auth_prefix,
        ticket_get_path_template=settings.gendesk_ticket_get_path_template,
        ticket_update_path_template=settings.gendesk_ticket_update_path_template,
        ticket_list_path=settings.gendesk_ticket_list_path,
    )
    asite = AsiteClient(
        login_url_template=settings.asite_login_url,
        email=settings.asite_email,
        password=settings.asite_password,
    )

    workflow = AsiteSupportWorkflow(
        gendesk=gendesk,
        asite=asite,
        catalog=catalog,
        dry_run=settings.dry_run,
    )
    outcome = workflow.run(ticket_id=args.ticket_id, post_note=args.post_note)
    print(json.dumps(outcome.__dict__, indent=2))


if __name__ == "__main__":
    main()
