# Asite Agentic Support Workflow

This repository now contains a first working version of your requested agent workflow:

1. Read support ticket from Gendesk API.
2. Create summary + proposed Asite action.
3. Check Asite API access and URI availability.
4. Ask admin permission before any action.
5. If approved, execute (or dry-run) and optionally respond to ticket.

It is intentionally safe-by-default (`DRY_RUN=true`) and designed for incremental expansion.

## Implemented Components

- `src/asite_agent/ticket_sources.py`
  - Fetches ticket data from Gendesk.
  - Can post internal ticket notes.
- `src/asite_agent/planner.py`
  - Generates a compact ticket summary.
  - Selects an initial action type using ticket content.
- `src/asite_agent/pdf_catalog.py`
  - Reads `Asite-API_Services_Overview.pdf`.
  - Extracts section metadata and sample URIs to build a local catalog cache.
- `src/asite_agent/asite_client.py`
  - Logs into Asite using documented login URL pattern.
  - Captures `Sessionid` and available URI list.
  - Executes approved calls with `ASessionID` cookie.
- `src/asite_agent/workflow.py`
  - Orchestrates your 5-step workflow with explicit admin approval.
- `src/asite_agent/main.py`
  - CLI entrypoint.

## Setup

1. Install dependencies:

```powershell
python -m pip install -e .
```

2. Create `.env` from template:

```powershell
Copy-Item .env.example .env
```

3. Fill values in `.env`:
- `GENDESK_BASE_URL`
- `GENDESK_API_KEY`
- `GENDESK_EMAIL`
- `GENDESK_AUTH_HEADER`
- `GENDESK_AUTH_PREFIX`
- `GENDESK_TICKET_GET_PATH_TEMPLATE`
- `GENDESK_TICKET_UPDATE_PATH_TEMPLATE`
- `GENDESK_TICKET_LIST_PATH`
- `GENDESK_TICKET_LIST_LIMIT`
- `ASITE_LOGIN_URL`
- `ASITE_EMAIL`
- `ASITE_PASSWORD`
- optionally `DRY_RUN=false` once validated

## Run

```powershell
python -m asite_agent.main --ticket-id 123456
```

Post back an internal note to Gendesk:

```powershell
python -m asite_agent.main --ticket-id 123456 --post-note
```

## Dashboard

Run the local approval dashboard:

```powershell
python -m asite_agent.dashboard --host 127.0.0.1 --port 8787
```

Then open:

`http://127.0.0.1:8787`

Dashboard features:
- Auto-poll Gendesk inbox (default every 20 seconds in UI)
- One-click "Analyze" from inbox tickets
- Analyze ticket by ID (steps 1-3)
- See proposed action + target Asite URI
- Approve or deny from UI (step 4)
- Execute and record result (step 5)
- Audit history stored in `.cache/dashboard_audit.jsonl`
- Login/session protection
- Admin-only user creation (`admin` and `operator` roles)

### Auth Setup

Set these in `.env` before starting dashboard:
- `DASHBOARD_SECRET_KEY` (long random value)
- `DASHBOARD_ADMIN_USERNAME` (default `admin`)
- `DASHBOARD_ADMIN_PASSWORD` (required for first bootstrap when no users exist)

After first login as admin, create additional users in the **User Access** section.

## Render Deployment

This repo includes `render.yaml` for a Python Web Service.

1. Push repo to GitHub.
2. In Render, create a new Web Service from the repo.
3. Render should detect `render.yaml`; keep start command:
   `python -m asite_agent.dashboard --host 0.0.0.0 --port $PORT`
4. Add required environment variables in Render:
- `GENDESK_BASE_URL`
- `GENDESK_API_KEY`
- `GENDESK_EMAIL`
- `GENDESK_AUTH_HEADER`
- `GENDESK_AUTH_PREFIX`
- `GENDESK_TICKET_GET_PATH_TEMPLATE`
- `GENDESK_TICKET_UPDATE_PATH_TEMPLATE`
- `GENDESK_TICKET_LIST_PATH`
- `GENDESK_TICKET_LIST_LIMIT`
- `ASITE_LOGIN_URL`
- `ASITE_EMAIL`
- `ASITE_PASSWORD`
- `ASITE_API_OVERVIEW_PDF` (optional, default set)
- `DRY_RUN`
- `DASHBOARD_SECRET_KEY`
- `DASHBOARD_ADMIN_USERNAME`
- `DASHBOARD_ADMIN_PASSWORD`

Optional persistence paths (recommended):
- `DASHBOARD_USER_DB=/var/data/users.db`
- `DASHBOARD_AUDIT_LOG=/var/data/dashboard_audit.jsonl`

Slack notifications (optional):
- `SLACK_WEBHOOK_URL` (Incoming Webhook URL from Slack app)
- `SLACK_CHANNEL` (optional override, e.g. `#asite-agent-approvals`)
- `SLACK_NOTIFY_DECISIONS` (`true` to also notify approve/deny outcomes)
- `DASHBOARD_PUBLIC_BASE_URL` (used in Slack message review link)
- `SLACK_SIGNING_SECRET` (required for secure Slack button callbacks)

Slack Approve/Decline buttons:
1. In your Slack app, enable **Interactivity & Shortcuts**.
2. Set Request URL to:
   `https://<your-render-domain>/slack/actions`
3. Use the same app to create Incoming Webhook and set `SLACK_WEBHOOK_URL`.
4. Set `SLACK_SIGNING_SECRET` from Slack app **Basic Information**.

When a new action request is created, Slack message buttons can approve/decline directly and continue the workflow.

If you want persistent user accounts and audit across deploys, attach a persistent disk and use `/var/data/...` paths.

## Behavior Notes

- Admin permission is always requested interactively before execution.
- With `DRY_RUN=true`, the workflow validates and plans but does not execute Asite side effects.
- If a planned action URI is not present in the logged-in Asite URI list, execution is blocked.

## Immediate Next Enhancements

- Replace keyword planning with an LLM policy planner for richer action decisions.
- Add a strict action registry with Asite payload schemas per operation.
- Add audit logging (JSONL) for every approval and API invocation.
- Add retry/rate-limit handling for Asite 429 responses.
- Add a non-interactive admin approval channel (Slack/Teams/email) with signed approval tokens.
