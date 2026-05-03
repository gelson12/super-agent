"""
Outbound lead generation tools.

Uses the Companies House free public API to find recently incorporated UK
companies that may not yet have a website, then queues them as BizDev memos
for one-click approval.

No API key needed for Companies House — it's a public REST API.
"""
from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.request
from typing import Any

from langchain_core.tools import tool


_CH_BASE = "https://api.company-information.service.gov.uk"
_CH_KEY = os.environ.get("COMPANIES_HOUSE_API_KEY", "")


def _ch_get(path: str) -> dict:
    """GET from Companies House API. Returns parsed JSON or {}."""
    if not _CH_KEY:
        return {}
    req = urllib.request.Request(
        f"{_CH_BASE}{path}",
        headers={"Authorization": f"Basic {__import__('base64').b64encode(f'{_CH_KEY}:'.encode()).decode()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def find_new_leads(max_leads: int = 20) -> list[dict]:
    """
    Find recently incorporated UK companies that might need a web presence.
    Falls back to a curated search if Companies House key is not set.
    Returns list of lead dicts.
    """
    leads = []
    if not _CH_KEY:
        return leads

    # Search for companies incorporated in last 30 days in target sectors
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y%m%d")
    sectors = ["marketing", "consulting", "retail", "services", "solutions"]
    seen: set[str] = set()

    for sector in sectors:
        if len(leads) >= max_leads:
            break
        data = _ch_get(
            f"/search/companies?q={sector}&incorporated_from={cutoff}&items_per_page=10"
        )
        for item in data.get("items", []):
            company_no = item.get("company_number", "")
            if company_no in seen:
                continue
            seen.add(company_no)
            name = item.get("title", "")
            address = item.get("address_snippet", "")
            incorporated = item.get("date_of_creation", "")
            # Skip if has registered website hint in description
            snippet = (item.get("snippet") or "").lower()
            if "website" in snippet or ".com" in snippet or ".co.uk" in snippet:
                continue
            leads.append({
                "company_name": name,
                "source_ref": company_no,
                "source": "companies_house",
                "notes": f"Incorporated: {incorporated}. Address: {address}",
                "contact_email": None,
                "contact_name": None,
                "phone": None,
                "website": None,
            })
            if len(leads) >= max_leads:
                break

    return leads


def queue_leads_as_memos(leads: list[dict]) -> int:
    """
    Insert leads into bridge.leads table and create BizDev memos for review.
    Returns count of successfully queued leads.
    """
    if not leads:
        return 0

    try:
        from ..memory.vector_memory import _get_pg_conn
        conn = _get_pg_conn()
        if not conn:
            return 0
        queued = 0
        try:
            with conn.cursor() as cur:
                for lead in leads:
                    # Skip if already exists by source_ref
                    cur.execute(
                        "SELECT 1 FROM bridge.leads WHERE source_ref = %s LIMIT 1",
                        (lead.get("source_ref"),),
                    )
                    if cur.fetchone():
                        continue
                    cur.execute("""
                        INSERT INTO bridge.leads
                          (company_name, contact_name, contact_email, phone,
                           website, source, source_ref, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        lead["company_name"], lead.get("contact_name"),
                        lead.get("contact_email"), lead.get("phone"),
                        lead.get("website"), lead.get("source", "companies_house"),
                        lead.get("source_ref"), lead.get("notes"),
                    ))
                    lead_id = str(cur.fetchone()[0])
                    # Create BizDev approval memo
                    cur.execute("""
                        INSERT INTO bridge.agent_memos
                          (from_agent, to_agent, memo_type, priority, subject, body_json)
                        VALUES ('lead_engine', 'bizdev', 'lead_review_request', 'normal',
                                %s, jsonb_build_object(
                                    'lead_id', %s,
                                    'company_name', %s,
                                    'source', %s,
                                    'notes', %s,
                                    'action', 'Review lead and draft personalised outreach email'
                                ))
                    """, (
                        f"New lead: {lead['company_name']}",
                        lead_id, lead["company_name"],
                        lead.get("source"), lead.get("notes"),
                    ))
                    queued += 1
            conn.commit()
        except Exception:
            try: conn.rollback()
            except Exception: pass
        finally:
            try: conn.close()
            except Exception: pass
        return queued
    except Exception:
        return 0


@tool
def add_lead(company_name: str, contact_email: str = "", contact_name: str = "",
             phone: str = "", website: str = "", notes: str = "") -> str:
    """Add a new outbound sales lead to the pipeline for BizDev bot follow-up."""
    queued = queue_leads_as_memos([{
        "company_name": company_name,
        "contact_email": contact_email or None,
        "contact_name": contact_name or None,
        "phone": phone or None,
        "website": website or None,
        "source": "manual",
        "source_ref": None,
        "notes": notes or None,
    }])
    if queued:
        return f"Lead '{company_name}' added to pipeline. BizDev bot will draft outreach."
    return f"Lead '{company_name}' already exists or DB unavailable."


@tool
def list_leads(status: str = "queued") -> str:
    """List leads in the pipeline. Status: queued, contacted, responded, closed."""
    try:
        from ..memory.vector_memory import _get_pg_conn
        conn = _get_pg_conn()
        if not conn:
            return "DB unavailable"
        with conn.cursor() as cur:
            cur.execute("""
                SELECT company_name, contact_email, status, created_at::date
                FROM bridge.leads
                WHERE status = %s
                ORDER BY created_at DESC LIMIT 20
            """, (status,))
            rows = cur.fetchall()
        conn.close()
        if not rows:
            return f"No leads with status '{status}'."
        lines = [f"**{r[0]}** ({r[1] or 'no email'}) — {r[2]} — added {r[3]}" for r in rows]
        return f"Leads ({status}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error listing leads: {e}"


LEAD_TOOLS = [add_lead, list_leads]
