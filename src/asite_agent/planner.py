from __future__ import annotations

from dataclasses import dataclass

from .models import PlannedAction, Ticket


@dataclass
class TicketPlan:
    summary: str
    action: PlannedAction


def _clean(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text[:limit]


def summarize_and_plan(ticket: Ticket) -> TicketPlan:
    body = f"{ticket.subject}\n{ticket.description}".lower()
    summary = f"Ticket {ticket.id}: {_clean(ticket.subject)} | {_clean(ticket.description)}"

    if any(word in body for word in ["metadata", "meta data", "attribute"]):
        action = PlannedAction(
            name="update_document_metadata",
            reason="Ticket appears to request document metadata update.",
            uri_hint="/commonapi/manageAttribute/updateAttributeValues",
            method="POST",
            requires_write=True,
        )
    elif "review comment" in body or "comment reply" in body:
        action = PlannedAction(
            name="create_review_comment",
            reason="Ticket appears to request review comment creation/update.",
            uri_hint="/commonapi/document/",
            method="POST",
            requires_write=True,
        )
    elif "task" in body:
        action = PlannedAction(
            name="task_search",
            reason="Ticket appears to be task-related and needs task lookup.",
            uri_hint="/commonapi/tasksearchapi/search",
            method="POST",
        )
    elif "form" in body:
        action = PlannedAction(
            name="form_search",
            reason="Ticket appears to be form-related and needs form lookup.",
            uri_hint="/commonapi/formsearchapi/search",
            method="POST",
        )
    elif any(word in body for word in ["document", "doc", "drawing"]):
        action = PlannedAction(
            name="document_search",
            reason="Ticket appears to be document-related and needs document lookup.",
            uri_hint="/commonapi/documentsearchapi/search",
            method="POST",
        )
    else:
        action = PlannedAction(
            name="workspace_list",
            reason="Fallback: inspect available workspaces to scope the issue.",
            uri_hint="/api/workspace/workspacelist",
            method="GET",
        )

    return TicketPlan(summary=summary, action=action)

