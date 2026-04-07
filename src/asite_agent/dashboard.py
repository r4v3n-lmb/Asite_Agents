from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .asite_client import AsiteClient
from .config import Settings
from .pdf_catalog import load_or_build_catalog
from .slack_notifier import SlackNotifier
from .ticket_sources import GendeskClient
from .workflow import AsiteSupportWorkflow, PendingApprovalRequest


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


@dataclass
class RequestRecord:
    request_id: str
    created_at: str
    status: str
    pending: dict[str, Any]
    outcome: dict[str, Any] | None = None
    decision_note: str = ""


class UserStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  username TEXT PRIMARY KEY,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('admin','operator')),
                  created_at TEXT NOT NULL
                )
                """
            )
            con.commit()

    def count_users(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])

    def bootstrap_admin(self, username: str, password: str) -> None:
        if self.count_users() > 0:
            return
        self.create_user(username=username, password=password, role="admin")

    def create_user(self, username: str, password: str, role: str = "operator") -> None:
        if role not in {"admin", "operator"}:
            raise ValueError("Invalid role")
        if len(username.strip()) < 3:
            raise ValueError("Username too short")
        if len(password) < 10:
            raise ValueError("Password must be at least 10 characters")
        with self._connect() as con:
            con.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (
                    username.strip(),
                    generate_password_hash(password),
                    role,
                    datetime.now(UTC).isoformat(),
                ),
            )
            con.commit()

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT username, role, created_at FROM users ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def authenticate(self, username: str, password: str) -> dict[str, str] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT username, password_hash, role FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not row:
            return None
        if not check_password_hash(str(row["password_hash"]), password):
            return None
        return {"username": str(row["username"]), "role": str(row["role"])}


class DashboardState:
    def __init__(
        self,
        workflow: AsiteSupportWorkflow,
        audit_path: Path,
        inbox_limit: int = 25,
        slack: SlackNotifier | None = None,
        notify_decisions: bool = False,
        public_base_url: str = "",
    ) -> None:
        self.workflow = workflow
        self.audit_path = audit_path
        self.inbox_limit = inbox_limit
        self.slack = slack or SlackNotifier("")
        self.notify_decisions = notify_decisions
        self.public_base_url = public_base_url.rstrip("/")
        self.requests: dict[str, RequestRecord] = {}

    def _append_audit(self, event: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def create_request(self, ticket_id: int) -> RequestRecord:
        request_id = str(uuid.uuid4())
        pending = self.workflow.build_pending_request(ticket_id=ticket_id, request_id=request_id)
        status = "pending_approval" if pending.available else "blocked"
        record = RequestRecord(
            request_id=request_id,
            created_at=datetime.now(UTC).isoformat(),
            status=status,
            pending=asdict(pending),
        )
        self.requests[request_id] = record
        self._append_audit(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "request_created",
                "request_id": request_id,
                "ticket_id": ticket_id,
                "status": status,
            }
        )
        self._notify_request_created(record)
        return record

    def decide(self, request_id: str, approved: bool, note: str, post_note: bool) -> RequestRecord:
        if request_id not in self.requests:
            raise KeyError("Request not found")
        record = self.requests[request_id]
        if record.status not in {"pending_approval", "blocked"}:
            raise ValueError("Request already decided")
        pending = PendingApprovalRequest(**record.pending)
        outcome = self.workflow.execute_pending_request(
            pending=pending, approved=approved, denial_note=note, post_note=post_note
        )
        record.outcome = asdict(outcome)
        record.decision_note = note
        if not outcome.approved:
            record.status = "blocked" if pending.available is False else "denied"
        elif outcome.executed:
            record.status = "approved_executed"
        else:
            record.status = "approved_dry_run"
        self._append_audit(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "request_decided",
                "request_id": request_id,
                "approved": approved,
                "status": record.status,
                "note": note,
            }
        )
        if self.notify_decisions:
            self._notify_request_decided(record=record, approved=approved)
        return record

    def _notify_request_created(self, record: RequestRecord) -> None:
        if not self.slack.enabled:
            return
        p = record.pending
        review_link = f"{self.public_base_url}/" if self.public_base_url else "https://example.com"
        try:
            self.slack.send_action_request(
                request_id=record.request_id,
                ticket_id=int(p.get("ticket_id") or 0),
                subject=str(p.get("ticket_subject", "")),
                action_name=str(p.get("action_name", "")),
                method=str(p.get("method", "")),
                status=record.status,
                review_url=review_link,
            )
        except Exception:
            return

    def _notify_request_decided(self, record: RequestRecord, approved: bool) -> None:
        if not self.slack.enabled:
            return
        p = record.pending
        status_word = "APPROVED" if approved else "DENIED"
        text = (
            f":white_check_mark: *Asite Action {status_word}*\n"
            f"- Ticket: #{p.get('ticket_id')}\n"
            f"- Action: `{p.get('action_name')}`\n"
            f"- Final Status: {record.status}\n"
            f"- Request ID: `{record.request_id}`"
        )
        try:
            self.slack.send_text(text)
        except Exception:
            return

    def list_requests(self) -> list[dict[str, Any]]:
        return [asdict(r) for r in sorted(self.requests.values(), key=lambda x: x.created_at, reverse=True)]

    def inbox(self) -> list[dict[str, Any]]:
        tickets = self.workflow.gendesk.list_tickets(limit=self.inbox_limit)
        return [asdict(t) for t in tickets]

    def inbox_debug(self) -> dict[str, Any]:
        return self.workflow.gendesk.inbox_debug(limit=5)

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines if line.strip()]
        return events[-limit:]


LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Asite Agent Login</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;background:#f7f6f2;margin:0}
.box{max-width:380px;margin:10vh auto;background:#fff;padding:20px;border:1px solid #ddd;border-radius:10px}
input,button{width:100%;padding:10px;margin-top:8px}
button{background:#2f4858;color:#fff;border:0;border-radius:6px}
.err{color:#9b1c1c}
</style></head><body>
<div class="box">
<h2>Asite Agent Dashboard</h2>
{% if error %}<p class="err">{{error}}</p>{% endif %}
<form method="post" action="/login">
<input name="username" placeholder="Username" required />
<input name="password" type="password" placeholder="Password" required />
<button type="submit">Sign In</button>
</form>
</div></body></html>"""


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Asite Agent Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0e0e0e;--surface-low:#131313;--surface:#191a1a;--surface-hi:#252626;--on:#e7e5e5;--muted:#acabaa;--primary:#7bd0ff;--primary-container:#004c69;--error:#bb5551;--ghost:#484848}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1200px 600px at 100% -20%,#113244 0%,transparent 60%),var(--bg);color:var(--on);font-family:"Inter",sans-serif}
.top{height:64px;display:flex;align-items:center;gap:14px;padding:0 22px;background:rgba(14,14,14,.94);position:sticky;top:0;z-index:10}
.brand{font-weight:800;letter-spacing:-.03em;font-size:22px}.brand span{color:var(--primary)}
.layout{display:grid;grid-template-columns:250px 1fr;min-height:calc(100vh - 64px)}aside{background:var(--surface-low);padding:18px 14px}
.nav{display:grid;gap:8px;margin-top:12px}.nav a{color:var(--muted);text-decoration:none;padding:10px 12px;border-radius:12px;background:transparent}.nav a.active{color:var(--on);background:var(--surface)}
main{padding:20px;display:grid;gap:16px}.grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}.card{background:var(--surface);padding:16px;border-radius:16px}
.card.soft{background:var(--surface-low)}.glass{background:rgba(37,38,38,.7);backdrop-filter:blur(12px)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.sp{flex:1}.muted{color:var(--muted);font-size:12px}
input,select,button{font:inherit;padding:10px 12px;border-radius:10px;border:0;outline:none}
input,select{background:var(--surface-hi);color:var(--on);min-width:120px}
button{cursor:pointer;background:var(--surface-hi);color:var(--on)}
.primary{background:linear-gradient(135deg,var(--primary) 0%,var(--primary-container) 100%);color:#d8f2ff;font-weight:700}
.ok{background:#1b4f3c;color:#d9f3e7}.no{background:#6d2523;color:#ffd8d6}.warn{background:#3d341d;color:#ffdc9a}
.item{background:var(--surface-hi);border-radius:12px;padding:12px;margin-top:10px}
.status{display:inline-block;padding:2px 9px;border-radius:999px;background:rgba(72,72,72,.3);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.err{padding:10px;border-radius:10px;background:rgba(127,41,39,.45);color:#ffb5af;margin-top:8px}
pre{background:#0b0b0b;color:#b8d8ea;padding:10px;border-radius:10px;max-height:210px;overflow:auto;font-size:12px}
a{color:var(--primary)}
@media (max-width:1060px){.layout{grid-template-columns:1fr}aside{display:none}.grid{grid-template-columns:1fr}}
</style></head><body>
<header class="top"><div class="brand">Midnight <span>Architect</span></div><div class="muted">Asite Control Plane</div><div class="sp"></div><div class="muted">User: {{username}} ({{role}})</div><a href="/logout">Logout</a></header>
<div class="layout">
<aside><div class="muted" style="letter-spacing:.2em;text-transform:uppercase">Admin Console</div><nav class="nav"><a class="active" href="#">Overview</a><a href="#">Helpdesk</a><a href="#">Actions</a><a href="#">History</a></nav></aside>
<main>
  <section class="grid">
    <div class="card soft">
      <div class="row"><h2 style="margin:0">Helpdesk Tickets</h2><span class="sp"></span><button class="primary" onclick="loadInbox()">Refresh</button><button class="warn" onclick="loadInboxDebug()">Debug Pull</button><label class="muted"><input type="checkbox" id="postNote" checked/> Post note back</label></div>
      <div class="muted">If the list is empty, click <b>Debug Pull</b> to inspect auth/path/response shape.</div>
      <div id="inboxErr"></div><div id="inbox"></div>
    </div>
    <div class="card glass">
      <h2 style="margin:0 0 10px">Run Analysis</h2>
      <div class="row"><input id="ticketId" type="number" placeholder="Ticket ID"/><button class="primary" onclick="analyze()">Analyze</button></div>
      <div id="actionErr"></div>
      <h3 style="margin:14px 0 6px">Request History</h3><div id="history"></div>
    </div>
  </section>
  {% if role == "admin" %}
  <section class="card">
    <h2 style="margin:0 0 8px">User Access</h2>
    <div class="row"><input id="newUsername" placeholder="New username"/><input id="newPassword" type="password" placeholder="Temp password (min 10 chars)"/><select id="newRole"><option value="operator">operator</option><option value="admin">admin</option></select><button class="primary" onclick="createUser()">Create User</button></div>
    <div id="userErr"></div><div id="users"></div>
  </section>
  {% endif %}
  <section class="card soft">
    <div class="row"><h2 style="margin:0">Action Requests</h2><span class="sp"></span><button class="primary" onclick="loadRequests()">Refresh</button></div>
    <div id="reqErr"></div><div id="requests"></div>
  </section>
</main></div>
<script>
async function api(path, method="GET", body=null){
  const resp=await fetch(path,{method,headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):null});
  const raw=await resp.text();
  let data={}; try{ data=raw?JSON.parse(raw):{} }catch(_){ data={raw} }
  if(!resp.ok){ throw new Error((data.error||raw||("HTTP "+resp.status)).toString()) }
  return data;
}
function esc(s){return (s||"").toString().replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]))}
function showErr(id,msg){document.getElementById(id).innerHTML=msg?`<div class="err">${esc(msg)}</div>`:""}
async function analyze(){try{showErr("actionErr","");const ticketId=parseInt(document.getElementById("ticketId").value,10); if(!ticketId){throw new Error("Enter ticket id")} const post_note=document.getElementById("postNote").checked; await api("/api/requests","POST",{ticket_id:ticketId,post_note}); await loadRequests();}catch(e){showErr("actionErr",e.message)}}
async function analyzeFromInbox(ticketId){document.getElementById("ticketId").value=ticketId; await analyze();}
async function decide(id,approved){try{showErr("reqErr","");const note=prompt(approved?"Optional approval note":"Denial note","")||""; const post_note=document.getElementById("postNote").checked; await api(`/api/requests/${id}/decision`,"POST",{approved,note,post_note}); await loadRequests(); await loadHistory();}catch(e){showErr("reqErr",e.message)}}
async function loadInbox(){try{showErr("inboxErr","");const data=await api("/api/inbox"); const c=document.getElementById("inbox"); c.innerHTML=""; if(!data.items||!data.items.length){c.innerHTML="<div class='muted'>No tickets returned from list endpoint.</div>"} for(const t of data.items){const d=document.createElement("div"); d.className="item"; d.innerHTML=`<div class="row"><strong>#${esc(t.id)}</strong><span class="status">${esc(t.status||"unknown")}</span><span class="muted">${esc(t.priority||"no-priority")}</span><span class="sp"></span><button class="primary" onclick="analyzeFromInbox(${esc(t.id)})">Analyze</button></div><div><strong>${esc(t.subject||"(no subject)")}</strong></div><div class="muted">${esc((t.description||"").slice(0,240))}</div>`; c.appendChild(d);} }catch(e){showErr("inboxErr",e.message)}}
async function loadInboxDebug(){try{const d=await api("/api/inbox/debug"); showErr("inboxErr",""); document.getElementById("inboxErr").innerHTML=`<pre>${esc(JSON.stringify(d,null,2))}</pre>`;}catch(e){showErr("inboxErr",e.message)}}
async function loadRequests(){try{showErr("reqErr","");const data=await api("/api/requests"); const c=document.getElementById("requests"); c.innerHTML=""; for(const r of data.items){const p=r.pending||{}; const o=r.outcome||null; const d=document.createElement("div"); d.className="item"; d.innerHTML=`<div class="row"><strong>#${esc(p.ticket_id)}</strong><span class="status">${esc(r.status)}</span><span class="muted">${esc(r.request_id)}</span></div><div><strong>Action:</strong> ${esc(p.action_name)} (${esc(p.method)})</div><div class="muted">${esc(p.summary||"")}</div><div class="muted">URI: ${esc(p.target_uri||"N/A")}</div>${o?`<pre>${esc(JSON.stringify(o,null,2))}</pre>`:""}<div class="row">${r.status==="pending_approval"?`<button class="ok" onclick="decide('${esc(r.request_id)}',true)">Approve</button><button class="no" onclick="decide('${esc(r.request_id)}',false)">Deny</button>`:""}</div>`; c.appendChild(d);} }catch(e){showErr("reqErr",e.message)}}
async function loadHistory(){try{const data=await api("/api/history"); document.getElementById("history").innerHTML=`<pre>${esc(JSON.stringify(data.items,null,2))}</pre>`;}catch(e){showErr("actionErr",e.message)}}
async function createUser(){try{showErr("userErr","");const username=document.getElementById("newUsername").value; const password=document.getElementById("newPassword").value; const role=document.getElementById("newRole").value; await api("/api/admin/users","POST",{username,password,role}); await loadUsers(); alert("User created")}catch(e){showErr("userErr",e.message)}}
async function loadUsers(){const el=document.getElementById("users"); if(!el) return; try{const data=await api("/api/admin/users"); el.innerHTML=`<pre>${esc(JSON.stringify(data.items,null,2))}</pre>`;}catch(e){showErr("userErr",e.message)}}
loadInbox(); loadRequests(); loadHistory(); loadUsers(); setInterval(loadInbox,20000); setInterval(loadRequests,20000);
</script></body></html>"""


def _json_error(msg: str, code: int = 400) -> tuple[Response, int]:
    return jsonify({"error": msg}), code


def _login_required(func: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if "username" not in session:
            return _json_error("Unauthorized", 401)
        return func(*args, **kwargs)

    return wrapper


def _admin_required(func: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if "username" not in session:
            return _json_error("Unauthorized", 401)
        if session.get("role") != "admin":
            return _json_error("Forbidden", 403)
        return func(*args, **kwargs)

    return wrapper


def create_app(
    workflow: AsiteSupportWorkflow,
    user_store: UserStore,
    state: DashboardState,
    secret_key: str,
    slack_signing_secret: str = "",
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secret_key

    @app.get("/")
    def root() -> Response | str:
        if "username" not in session:
            return redirect("/login")
        return render_template_string(
            DASHBOARD_HTML, username=session.get("username"), role=session.get("role")
        )

    @app.get("/login")
    def login_form() -> str:
        return render_template_string(LOGIN_HTML, error="")

    @app.post("/login")
    def login_submit() -> str | Response:
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = user_store.authenticate(username=username, password=password)
        if not user:
            return render_template_string(LOGIN_HTML, error="Invalid credentials")
        session["username"] = user["username"]
        session["role"] = user["role"]
        return redirect("/")

    @app.get("/logout")
    def logout() -> Response:
        session.clear()
        return redirect("/login")

    @app.get("/api/inbox")
    @_login_required
    def api_inbox() -> Response:
        return jsonify({"items": state.inbox()})

    @app.get("/api/inbox/debug")
    @_login_required
    def api_inbox_debug() -> tuple[Response, int] | Response:
        try:
            return jsonify(state.inbox_debug())
        except Exception as exc:  # noqa: BLE001
            return _json_error(str(exc))

    @app.get("/api/requests")
    @_login_required
    def api_requests() -> Response:
        return jsonify({"items": state.list_requests()})

    @app.post("/api/requests")
    @_login_required
    def api_create_request() -> tuple[Response, int] | Response:
        try:
            data = request.get_json(force=True, silent=False) or {}
            ticket_id = int(data["ticket_id"])
            rec = state.create_request(ticket_id=ticket_id)
            return jsonify(asdict(rec))
        except Exception as exc:  # noqa: BLE001
            return _json_error(str(exc))

    @app.post("/api/requests/<request_id>/decision")
    @_login_required
    def api_decide(request_id: str) -> tuple[Response, int] | Response:
        try:
            data = request.get_json(force=True, silent=False) or {}
            rec = state.decide(
                request_id=request_id,
                approved=bool(data.get("approved")),
                note=str(data.get("note", "")),
                post_note=bool(data.get("post_note", False)),
            )
            return jsonify(asdict(rec))
        except KeyError as exc:
            return _json_error(str(exc), 404)
        except Exception as exc:  # noqa: BLE001
            return _json_error(str(exc))

    @app.get("/api/history")
    @_login_required
    def api_history() -> Response:
        return jsonify({"items": state.history()})

    @app.get("/api/admin/users")
    @_admin_required
    def api_users() -> Response:
        return jsonify({"items": user_store.list_users()})

    @app.post("/api/admin/users")
    @_admin_required
    def api_create_user() -> tuple[Response, int] | Response:
        try:
            data = request.get_json(force=True, silent=False) or {}
            user_store.create_user(
                username=str(data.get("username", "")),
                password=str(data.get("password", "")),
                role=str(data.get("role", "operator")),
            )
            return jsonify({"ok": True})
        except sqlite3.IntegrityError:
            return _json_error("Username already exists")
        except Exception as exc:  # noqa: BLE001
            return _json_error(str(exc))

    @app.post("/slack/actions")
    def slack_actions() -> tuple[Response, int] | Response:
        if not slack_signing_secret:
            return _json_error("Slack signing secret not configured", 400)
        raw_body = request.get_data(cache=False) or b""
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not timestamp or not signature:
            return _json_error("Missing Slack signature headers", 401)
        try:
            ts = int(timestamp)
        except ValueError:
            return _json_error("Invalid timestamp", 401)
        if abs(int(time.time()) - ts) > 60 * 5:
            return _json_error("Stale Slack request", 401)

        base = f"v0:{timestamp}:{raw_body.decode('utf-8')}".encode("utf-8")
        expected = "v0=" + hmac.new(
            slack_signing_secret.encode("utf-8"), base, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return _json_error("Invalid Slack signature", 401)

        form = parse_qs(raw_body.decode("utf-8"))
        payload_raw = (form.get("payload") or ["{}"])[0]
        payload = json.loads(payload_raw)
        actions = payload.get("actions") or []
        if not actions:
            return jsonify({"text": "No action found"})
        action = actions[0]
        value_raw = action.get("value", "{}")
        value = json.loads(value_raw)
        request_id = str(value.get("request_id", ""))
        approved = bool(value.get("approved", False))
        post_note = bool(value.get("post_note", False))

        user = (payload.get("user") or {}).get("username") or (payload.get("user") or {}).get("name") or "slack-user"
        note = f"Decision via Slack by {user}"
        try:
            rec = state.decide(
                request_id=request_id, approved=approved, note=note, post_note=post_note
            )
            return jsonify(
                {
                    "response_type": "ephemeral",
                    "text": (
                        f"Request `{request_id}` processed. "
                        f"Status: `{rec.status}`."
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify(
                {
                    "response_type": "ephemeral",
                    "text": f"Unable to process request `{request_id}`: {exc}",
                }
            )

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Asite agent approval dashboard")
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8787")), help="Bind port")
    parser.add_argument(
        "--audit-log",
        default=os.getenv("DASHBOARD_AUDIT_LOG", ".cache/dashboard_audit.jsonl"),
        help="Audit log path (JSONL)",
    )
    parser.add_argument(
        "--user-db",
        default=os.getenv("DASHBOARD_USER_DB", ".cache/users.db"),
        help="SQLite user database file",
    )
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
    state = DashboardState(
        workflow=workflow,
        audit_path=Path(args.audit_log),
        inbox_limit=settings.gendesk_ticket_list_limit,
        slack=SlackNotifier(settings.slack_webhook_url, settings.slack_channel),
        notify_decisions=settings.slack_notify_decisions,
        public_base_url=settings.dashboard_public_base_url,
    )

    user_store = UserStore(Path(args.user_db))
    bootstrap_user = os.getenv("DASHBOARD_ADMIN_USERNAME", "admin")
    bootstrap_pass = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")
    if user_store.count_users() == 0 and not bootstrap_pass:
        raise RuntimeError(
            "No users found. Set DASHBOARD_ADMIN_PASSWORD in .env to bootstrap the first admin user."
        )
    if bootstrap_pass:
        user_store.bootstrap_admin(username=bootstrap_user, password=bootstrap_pass)

    secret_key = os.getenv("DASHBOARD_SECRET_KEY", "")
    if not secret_key:
        raise RuntimeError("DASHBOARD_SECRET_KEY is required")
    app = create_app(
        workflow=workflow,
        user_store=user_store,
        state=state,
        secret_key=secret_key,
        slack_signing_secret=settings.slack_signing_secret,
    )
    print(f"Dashboard running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
