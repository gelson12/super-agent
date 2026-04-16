"""
Secretary Tools — gives agents the ability to trigger the Secretary n8n workflow
for Microsoft Outlook email and calendar operations.

The Secretary workflow is an n8n webhook at {N8N_BASE_URL}/webhook/secretary
that handles: list, search, get, send, reply, forward, create_draft, delete,
move, mark, flag, list_folders, get_attachments, calendar_list, calendar_create.

Any source (Super Agent chat, WhatsApp, other n8n workflows) can POST to that
webhook and n8n handles all the Outlook/Microsoft Graph API operations.

This module is the Super Agent side: a single LangChain tool that constructs the
POST body and calls the webhook, returning the structured response.

Requirements:
  - N8N_BASE_URL env var set (same as n8n_tools.py)
  - Microsoft Outlook credentials configured in n8n (done in the workflow)
"""
import json
import os
import httpx
from langchain_core.tools import tool

_TIMEOUT = 30  # seconds — Outlook Graph API is usually fast
_SECRETARY_PATH = "/webhook/secretary"

_VALID_ACTIONS = {
    "list", "search", "get", "send", "reply", "forward",
    "create_draft", "delete", "move", "mark", "flag",
    "list_folders", "get_attachments", "calendar_list", "calendar_create",
}


def _secretary_url() -> str:
    base = os.environ.get("N8N_BASE_URL", "").rstrip("/")
    if not base:
        try:
            from ..config import settings
            base = (settings.n8n_base_url or "").rstrip("/")
        except Exception:
            pass
    return base + _SECRETARY_PATH if base else ""


@tool
def secretary_email(action: str, params_json: str = "{}") -> str:
    """
    Trigger the Secretary n8n workflow to perform Outlook email or calendar operations.

    This is the bridge between Super Agent and Microsoft Outlook — the n8n Secretary
    workflow handles all Microsoft Graph API calls. Use this whenever the user asks
    to read, send, search, reply to, forward, or manage emails, or to check / create
    calendar events.

    Args:
        action: One of:
          Email actions:
            list          — list emails in a folder (params: folder, limit)
            search        — search emails by keyword (params: query, limit)
            get           — get a single email (params: message_id)
            send          — send a new email (params: to, subject, body, cc, body_type)
            reply         — reply to an email (params: message_id, body)
            forward       — forward an email (params: message_id, to, comment)
            create_draft  — save a draft without sending (params: to, subject, body, cc)
            delete        — delete an email (params: message_id)
            move          — move to another folder (params: message_id, destination)
            mark          — mark as read/unread (params: message_id, is_read)
            flag          — flag for follow-up (params: message_id, flag_status: "flagged"|"complete"|"notFlagged")
            list_folders  — list all mailbox folders
            get_attachments — list attachments of an email (params: message_id)
          Calendar actions:
            calendar_list   — list upcoming events (params: days, limit)
            calendar_create — create a meeting (params: subject, start, end, attendees, body, location)

        params_json: JSON string of parameters for the chosen action.
          Examples:
            '{"to": "alice@example.com", "subject": "Hello", "body": "Hi there"}'
            '{"query": "invoice", "limit": 10}'
            '{"message_id": "AAMkADFi...", "body": "Thanks for reaching out!"}'
            '{"subject": "Standup", "start": "2026-04-16T10:00:00", "end": "2026-04-16T10:30:00", "attendees": "alice@example.com,bob@example.com"}'

    Returns:
        JSON string with {success, action, data/count/message} from n8n.
        On error returns a descriptive string starting with '[secretary error:'.
    """
    url = _secretary_url()
    if not url:
        return "[secretary error: N8N_BASE_URL not set — configure it in Railway Variables]"

    action = action.strip().lower()
    if action not in _VALID_ACTIONS:
        valid = ", ".join(sorted(_VALID_ACTIONS))
        return f"[secretary error: unknown action '{action}'. Valid actions: {valid}]"

    try:
        params = json.loads(params_json) if params_json.strip() else {}
    except json.JSONDecodeError as e:
        return f"[secretary error: invalid params_json — {e}]"

    payload = {"action": action, "params": params}

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(url, json=payload)
        if resp.status_code >= 400:
            return f"[secretary error: HTTP {resp.status_code} — {resp.text[:300]}]"
        return resp.text
    except httpx.TimeoutException:
        return "[secretary error: request timed out — Outlook/n8n may be slow, retry once]"
    except Exception as exc:
        return f"[secretary error: {type(exc).__name__}: {exc}]"


# ── Convenience list for agent tool registration ──────────────────────────────

SECRETARY_TOOLS = [secretary_email]
