"""
Stripe payment tools — create payment links and retrieve payment status.

Prerequisites
-------------
Set Railway env var STRIPE_SECRET_KEY (Stripe dashboard → Developers → API Keys → Secret key).

Tools
-----
  stripe_create_payment_link(amount_gbp, description, client_name, client_email) → URL
  stripe_get_payment_status(payment_link_id_or_url) → status dict as string
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from langchain_core.tools import tool


def _stripe(method: str, path: str, data: dict | None = None, timeout: int = 30) -> dict:
    """Make a Stripe REST API call (form-encoded). Returns parsed JSON or raises."""
    api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not api_key:
        raise ValueError("STRIPE_SECRET_KEY not configured — set it as a Railway env var")

    url = f"https://api.stripe.com{path}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"Stripe HTTP {e.code}: {body_text}") from e


@tool
def stripe_create_payment_link(
    amount_gbp: float,
    description: str,
    client_name: str = "",
    client_email: str = "",
) -> str:
    """
    Create a Stripe payment link for a client invoice or project deposit.

    Use this when a client has agreed to pay for a website, service package, or project.
    The link is live immediately and can be shared via WhatsApp, email, or SMS.

    Args:
        amount_gbp:   Amount in GBP (e.g. 500.00 for £500). Minimum £0.30.
        description:  What the client is paying for (e.g. "Website design — Smith Plumbing").
        client_name:  Optional. Pre-fills the Stripe checkout with the client's name.
        client_email: Optional. Pre-fills email; Stripe will send them a receipt automatically.

    Returns:
        The Stripe payment link URL (e.g. "https://buy.stripe.com/xxxxx")
        or an error string starting with "[".

    Example:
        url = stripe_create_payment_link(
            amount_gbp=750.0,
            description="Landing page — Jones Electrical Birmingham",
            client_email="jones@example.com"
        )
        # → "https://buy.stripe.com/test_abc123"
    """
    if amount_gbp < 0.30:
        return "[stripe_tools error: minimum amount is £0.30]"
    if not description.strip():
        return "[stripe_tools error: description is required]"

    amount_pence = int(round(amount_gbp * 100))

    try:
        # Create a Price (one-time, GBP)
        price_data: dict = {
            "unit_amount": str(amount_pence),
            "currency": "gbp",
            "product_data[name]": description[:500],
        }
        if client_name:
            price_data["product_data[metadata][client_name]"] = client_name[:100]

        price = _stripe("POST", "/v1/prices", price_data)
        price_id = price["id"]

        # Create the payment link
        link_data: dict = {
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
        }
        if client_email:
            link_data["customer_email_collection"] = "auto"

        link = _stripe("POST", "/v1/payment_links", link_data)
        url = link.get("url", "")
        link_id = link.get("id", "")
        if not url:
            return f"[stripe_tools error: no URL returned — id={link_id}]"
        return f"{url}  (id: {link_id})"

    except (RuntimeError, ValueError, KeyError) as e:
        return f"[stripe_tools error: {e}]"
    except Exception as e:
        return f"[stripe_tools error: {e}]"


@tool
def stripe_get_payment_status(payment_link_id: str) -> str:
    """
    Check whether a Stripe payment link has been paid.

    Args:
        payment_link_id: The payment link ID (e.g. "plink_abc123") or the full URL.
                         Extract the ID from the URL: buy.stripe.com/<id>

    Returns:
        Human-readable status string including total paid and number of checkouts.
    """
    # Extract ID from URL if full URL was passed
    pid = payment_link_id.strip().rstrip("/")
    if "/" in pid:
        pid = pid.rsplit("/", 1)[-1]
    if pid.startswith("plink_"):
        pass  # already an ID
    elif not pid:
        return "[stripe_tools error: payment_link_id is required]"

    try:
        link = _stripe("GET", f"/v1/payment_links/{pid}")
        active = link.get("active", False)
        # Fetch completed checkout sessions for this link
        sessions = _stripe("GET", f"/v1/checkout/sessions?payment_link={pid}&limit=10")
        completed = [s for s in sessions.get("data", []) if s.get("payment_status") == "paid"]
        total_collected = sum(s.get("amount_total", 0) for s in completed) / 100
        currency = (completed[0].get("currency", "gbp").upper() if completed else "GBP")
        return (
            f"Payment link {pid}: active={active}, "
            f"paid_sessions={len(completed)}, "
            f"total_collected={currency} {total_collected:.2f}"
        )
    except (RuntimeError, ValueError, KeyError) as e:
        return f"[stripe_tools error: {e}]"
    except Exception as e:
        return f"[stripe_tools error: {e}]"


STRIPE_TOOLS = [stripe_create_payment_link, stripe_get_payment_status]
