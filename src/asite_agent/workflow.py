from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .asite_client import AsiteClient
from .models import PermissionDecision, PlannedAction, Ticket
from .pdf_catalog import AsiteApiCatalog
from .planner import TicketPlan, summarize_and_plan
from .ticket_sources import GendeskClient


@dataclass
class WorkflowOutcome:
    ticket_id: int
    summary: str
    action_name: str
    approved: bool
    executed: bool
    notes: str
    response_snippet: str = ""


@dataclass
class PendingApprovalRequest:
    request_id: str
    ticket_id: int
    ticket_subject: str
    ticket_description: str
    summary: str
    action_name: str
    action_reason: str
    method: str
    uri_hint: str
    requires_write: bool
    target_uri: str | None
    available: bool
    payload_preview: dict[str, Any]


class AsiteSupportWorkflow:
    def __init__(
        self,
        gendesk: GendeskClient,
        asite: AsiteClient,
        catalog: AsiteApiCatalog,
        dry_run: bool = True,
    ) -> None:
        self.gendesk = gendesk
        self.asite = asite
        self.catalog = catalog
        self.dry_run = dry_run

    def run(self, ticket_id: int, post_note: bool = False) -> WorkflowOutcome:
        pending = self.build_pending_request(ticket_id=ticket_id)
        if not pending.available or not pending.target_uri:
            outcome = WorkflowOutcome(
                ticket_id=pending.ticket_id,
                summary=pending.summary,
                action_name=pending.action_name,
                approved=False,
                executed=False,
                notes=(
                    f"Action `{pending.action_name}` unavailable. "
                    f"URI hint `{pending.uri_hint}` not present in Asite login profile."
                ),
            )
            if post_note:
                self._post_ticket_note(pending.ticket_id, outcome)
            return outcome

        decision = self._step4_request_admin_permission(pending)
        if not decision.approved:
            outcome = WorkflowOutcome(
                ticket_id=pending.ticket_id,
                summary=pending.summary,
                action_name=pending.action_name,
                approved=False,
                executed=False,
                notes=f"Admin denied action. Note: {decision.note}",
            )
            if post_note:
                self._post_ticket_note(pending.ticket_id, outcome)
            return outcome

        result = self.execute_pending_request(
            pending=pending, approved=True, denial_note="", post_note=post_note
        )
        return result

    def build_pending_request(self, ticket_id: int, request_id: str = "") -> PendingApprovalRequest:
        ticket = self._step1_read_ticket(ticket_id)
        plan = self._step2_create_summary_and_action(ticket)
        can_call, uri = self._step3_check_access_and_availability(plan)
        payload_preview: dict[str, Any] = {
            "ticketId": ticket.id,
            "subject": ticket.subject,
            "action": plan.action.name,
            "reason": plan.action.reason,
        }
        return PendingApprovalRequest(
            request_id=request_id,
            ticket_id=ticket.id,
            ticket_subject=ticket.subject,
            ticket_description=ticket.description,
            summary=plan.summary,
            action_name=plan.action.name,
            action_reason=plan.action.reason,
            method=plan.action.method,
            uri_hint=plan.action.uri_hint,
            requires_write=plan.action.requires_write,
            target_uri=uri,
            available=can_call and bool(uri),
            payload_preview=payload_preview,
        )

    def execute_pending_request(
        self,
        pending: PendingApprovalRequest,
        approved: bool,
        denial_note: str = "",
        post_note: bool = False,
    ) -> WorkflowOutcome:
        if not pending.available or not pending.target_uri:
            outcome = WorkflowOutcome(
                ticket_id=pending.ticket_id,
                summary=pending.summary,
                action_name=pending.action_name,
                approved=False,
                executed=False,
                notes=(
                    f"Action `{pending.action_name}` unavailable. "
                    f"URI hint `{pending.uri_hint}` not present in Asite login profile."
                ),
            )
            if post_note:
                self._post_ticket_note(pending.ticket_id, outcome)
            return outcome

        if not approved:
            outcome = WorkflowOutcome(
                ticket_id=pending.ticket_id,
                summary=pending.summary,
                action_name=pending.action_name,
                approved=False,
                executed=False,
                notes=f"Admin denied action. Note: {denial_note}",
            )
            if post_note:
                self._post_ticket_note(pending.ticket_id, outcome)
            return outcome

        ticket = Ticket(
            id=pending.ticket_id,
            subject=pending.ticket_subject,
            description=pending.ticket_description,
        )
        plan = TicketPlan(
            summary=pending.summary,
            action=PlannedAction(
                name=pending.action_name,
                reason=pending.action_reason,
                uri_hint=pending.uri_hint,
                method=pending.method,
                requires_write=pending.requires_write,
            ),
        )
        result = self._step5_execute_and_prepare_response(ticket, plan, pending.target_uri)
        if post_note:
            self._post_ticket_note(pending.ticket_id, result)
        return result

    def _step1_read_ticket(self, ticket_id: int) -> Ticket:
        return self.gendesk.get_ticket(ticket_id)

    def _step2_create_summary_and_action(self, ticket: Ticket) -> TicketPlan:
        return summarize_and_plan(ticket)

    def _step3_check_access_and_availability(self, plan: TicketPlan) -> tuple[bool, str | None]:
        session = self.asite.login()
        uri_from_profile = self.asite.pick_uri(plan.action.uri_hint)
        if uri_from_profile:
            return True, uri_from_profile

        if self.catalog.has_uri_hint(plan.action.uri_hint):
            # Catalog knows about this operation but user session does not expose it.
            return False, None

        return False, None

    def _step4_request_admin_permission(self, pending: PendingApprovalRequest) -> PermissionDecision:
        print("\n--- ADMIN PERMISSION REQUIRED ---")
        print(f"Ticket: {pending.ticket_id}")
        print(f"Action: {pending.action_name}")
        print(f"Method: {pending.method}")
        print(f"Target URI: {pending.target_uri}")
        print(f"Write action: {pending.requires_write}")
        print("Payload preview:")
        print(json.dumps(pending.payload_preview, indent=2))
        answer = input("Approve action? (yes/no): ").strip().lower()
        if answer in {"yes", "y"}:
            return PermissionDecision(approved=True)
        note = input("Optional denial note: ").strip()
        return PermissionDecision(approved=False, note=note)

    def _step5_execute_and_prepare_response(
        self, ticket: Ticket, plan: TicketPlan, uri: str
    ) -> WorkflowOutcome:
        payload = {
            "ticketId": ticket.id,
            "subject": ticket.subject,
            "description": ticket.description[:1000],
            "agentSummary": plan.summary,
            "plannedAction": plan.action.name,
        }

        if self.dry_run:
            return WorkflowOutcome(
                ticket_id=ticket.id,
                summary=plan.summary,
                action_name=plan.action.name,
                approved=True,
                executed=False,
                notes="Dry run enabled. No Asite API side effect executed.",
                response_snippet="Action approved and validated; execution deferred due to dry-run mode.",
            )

        try:
            resp = self.asite.call_uri(url=uri, method=plan.action.method, payload=payload)
            snippet = json.dumps(resp)[:500]
            return WorkflowOutcome(
                ticket_id=ticket.id,
                summary=plan.summary,
                action_name=plan.action.name,
                approved=True,
                executed=True,
                notes="Action executed successfully.",
                response_snippet=snippet,
            )
        except Exception as exc:  # noqa: BLE001
            return WorkflowOutcome(
                ticket_id=ticket.id,
                summary=plan.summary,
                action_name=plan.action.name,
                approved=True,
                executed=False,
                notes=f"Execution failed: {exc}",
            )

    def _post_ticket_note(self, ticket_id: int, outcome: WorkflowOutcome) -> None:
        message = (
            "Asite Agent Workflow Result\n"
            f"- Ticket: {outcome.ticket_id}\n"
            f"- Summary: {outcome.summary}\n"
            f"- Planned action: {outcome.action_name}\n"
            f"- Admin approved: {outcome.approved}\n"
            f"- Executed: {outcome.executed}\n"
            f"- Notes: {outcome.notes}\n"
        )
        if outcome.response_snippet:
            message += f"- Response snippet: {outcome.response_snippet}\n"
        self.gendesk.add_internal_note(ticket_id=ticket_id, message=message)
