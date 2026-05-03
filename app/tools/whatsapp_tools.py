"""
WhatsApp Business messaging tools via Twilio.

Setup instructions (one-time):
1. Create a Twilio account at https://twilio.com
2. Enable WhatsApp Sandbox (or apply for WhatsApp Business API)
3. Set Railway env vars:
   - TWILIO_ACCOUNT_SID   (from Twilio Console)
   - TWILIO_AUTH_TOKEN    (from Twilio Console)
   - TWILIO_WHATSAPP_FROM (e.g. whatsapp:+14155238886 for sandbox)

Usage: Send proposals, payment links, project updates, and deposit chasers
       to clients via WhatsApp automatically.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

from langchain_core.tools import tool


def _twilio_send(to_number: str, message: str) -> dict:
    """
    Send a WhatsApp message via Twilio.
    to_number should be in format: +447XXXXXXXXX (will be prefixed with whatsapp:)
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM", "")

    if not all([account_sid, auth_token, from_number]):
        return {"error": "Twilio credentials not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM in Railway env vars."}

    # Normalise number
    if not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"
    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": from_number,
        "To": to_number,
        "Body": message,
    }).encode()
    auth = b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read().decode())
            return {"ok": True, "sid": result.get("sid"), "status": result.get("status")}
    except urllib.error.HTTPError as e:
        return {"error": f"Twilio HTTP {e.code}: {e.read().decode()}"}
    except Exception as e:
        return {"error": str(e)}


@tool
def send_whatsapp_message(phone_number: str, message: str) -> str:
    """
    Send a WhatsApp message to a client phone number.
    phone_number: UK mobile e.g. +447911123456
    message: plain text message (max 1600 chars)
    Requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM env vars.
    """
    result = _twilio_send(phone_number, message[:1600])
    if result.get("ok"):
        return f"WhatsApp message sent to {phone_number} (SID: {result.get('sid')})"
    return f"WhatsApp send failed: {result.get('error')}"


@tool
def send_whatsapp_proposal(
    phone_number: str,
    client_name: str,
    service: str,
    price_gbp: float,
    proposal_url: str = "",
) -> str:
    """
    Send a professional proposal summary via WhatsApp to a potential client.
    Includes service description, price, and link to full proposal.
    """
    msg = (
        f"Hi {client_name} 👋\n\n"
        f"Thank you for your interest in Bridge Digital Solutions!\n\n"
        f"*Your Proposal:*\n"
        f"• Service: {service}\n"
        f"• Investment: £{price_gbp:,.2f}\n"
    )
    if proposal_url:
        msg += f"• Full proposal: {proposal_url}\n"
    msg += (
        f"\nThis proposal is valid for 14 days. "
        f"Reply here or email hello@bridge-digital-solution.com to get started.\n\n"
        f"— Bridge Digital Solutions 🚀"
    )
    result = _twilio_send(phone_number, msg)
    if result.get("ok"):
        return f"Proposal sent via WhatsApp to {client_name} ({phone_number})"
    return f"WhatsApp proposal failed: {result.get('error')}"


@tool
def send_whatsapp_payment_reminder(
    phone_number: str,
    client_name: str,
    amount_gbp: float,
    payment_link: str,
    invoice_ref: str = "",
) -> str:
    """
    Send a polite payment reminder or deposit request via WhatsApp.
    """
    ref_line = f"• Reference: {invoice_ref}\n" if invoice_ref else ""
    msg = (
        f"Hi {client_name} 👋\n\n"
        f"A friendly reminder about your payment:\n\n"
        f"• Amount due: *£{amount_gbp:,.2f}*\n"
        f"{ref_line}"
        f"• Pay securely: {payment_link}\n\n"
        f"If you have any questions, just reply here.\n\n"
        f"Thanks! — Bridge Digital Solutions"
    )
    result = _twilio_send(phone_number, msg)
    if result.get("ok"):
        return f"Payment reminder sent to {client_name} ({phone_number})"
    return f"WhatsApp reminder failed: {result.get('error')}"


@tool
def send_whatsapp_status_update(
    phone_number: str,
    client_name: str,
    project_name: str,
    update_message: str,
    portal_url: str = "",
) -> str:
    """
    Send a project status update to a client via WhatsApp.
    """
    portal_line = f"\n• View full status: {portal_url}" if portal_url else ""
    msg = (
        f"Hi {client_name}! 🎉\n\n"
        f"*Update on {project_name}:*\n\n"
        f"{update_message}{portal_line}\n\n"
        f"— Bridge Digital Solutions"
    )
    result = _twilio_send(phone_number, msg)
    if result.get("ok"):
        return f"Status update sent to {client_name} ({phone_number})"
    return f"WhatsApp update failed: {result.get('error')}"


WHATSAPP_TOOLS = [
    send_whatsapp_message,
    send_whatsapp_proposal,
    send_whatsapp_payment_reminder,
    send_whatsapp_status_update,
]
