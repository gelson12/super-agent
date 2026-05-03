"""
Automated post-delivery client testimonial and review sequence.

Day 3:  Warm check-in — "How is everything going?"
Day 14: Google review request with direct link
Day 30: Free SEO / performance audit offer

Sequences are created via POST /webhook/bot-engine or manually via DB insert.
Runs daily at 09:15 UTC via APScheduler.
"""
from __future__ import annotations

import os
from typing import Any


_GOOGLE_REVIEW_URL = os.environ.get(
    "GOOGLE_REVIEW_URL",
    "https://g.page/r/YOUR_GOOGLE_PLACE_ID/review",
)
_COMPANY_EMAIL = "hello@bridge-digital-solution.com"


def _send_email(to: str, subject: str, body: str) -> bool:
    """Fire email via secretary tools. Returns True on success."""
    try:
        from .secretary_tools import secretary_email as _sec
        import json as _j
        _sec.invoke({
            "action": "send",
            "params_json": _j.dumps({
                "to": to,
                "subject": subject,
                "body": body,
            }),
        })
        return True
    except Exception:
        return False


def _send_whatsapp(phone: str, message: str) -> bool:
    """Send WhatsApp message if Twilio configured, else silently skip."""
    try:
        from .whatsapp_tools import _twilio_send
        result = _twilio_send(phone, message)
        return result.get("ok", False)
    except Exception:
        return False


def run_testimonial_sequences() -> int:
    """
    Process all active review sequences and send messages for due steps.
    Returns number of steps processed.
    """
    try:
        from ..memory.vector_memory import _get_pg_conn
        import datetime as _dt
        conn = _get_pg_conn()
        if not conn:
            return 0
        today = _dt.date.today()
        sent = 0
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, client_name, client_email,
                           delivery_date,
                           day3_sent_at, day14_sent_at, day30_sent_at
                    FROM bridge.review_sequences
                    WHERE review_received = FALSE
                      AND (day30_sent_at IS NULL OR day30_sent_at > NOW() - INTERVAL '200 days')
                """)
                rows = cur.fetchall()

            for row in rows:
                seq_id, client_name, client_email, delivery_date = row[:4]
                day3_sent, day14_sent, day30_sent = row[4], row[5], row[6]
                days_since = (today - delivery_date).days

                if days_since >= 3 and day3_sent is None:
                    # Day 3: warm check-in
                    subject = f"How's everything going, {client_name}?"
                    body = (
                        f"Hi {client_name},\n\n"
                        f"I hope you're enjoying your new website / project!\n\n"
                        f"Just checking in to see if you have any questions or "
                        f"if there's anything you'd like us to tweak.\n\n"
                        f"We're always here to help — just reply to this email.\n\n"
                        f"Best,\nBridge Digital Solutions\n{_COMPANY_EMAIL}"
                    )
                    if _send_email(client_email, subject, body):
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE bridge.review_sequences SET day3_sent_at = NOW() WHERE id = %s",
                                (seq_id,)
                            )
                        conn.commit()
                        sent += 1

                elif days_since >= 14 and day14_sent is None:
                    # Day 14: Google review request
                    subject = f"Would you leave us a quick review, {client_name}?"
                    body = (
                        f"Hi {client_name},\n\n"
                        f"It's been a couple of weeks since we delivered your project "
                        f"and I hope it's already making a difference!\n\n"
                        f"Would you mind taking 2 minutes to leave us a Google review?\n"
                        f"It really helps small businesses like ours:\n\n"
                        f"👉 {_GOOGLE_REVIEW_URL}\n\n"
                        f"Even a sentence or two means the world to us. Thank you!\n\n"
                        f"Best,\nBridge Digital Solutions\n{_COMPANY_EMAIL}"
                    )
                    if _send_email(client_email, subject, body):
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE bridge.review_sequences SET day14_sent_at = NOW() WHERE id = %s",
                                (seq_id,)
                            )
                        conn.commit()
                        sent += 1

                elif days_since >= 30 and day30_sent is None:
                    # Day 30: free audit offer
                    subject = f"Free website performance audit for {client_name}"
                    body = (
                        f"Hi {client_name},\n\n"
                        f"It's been a month since your launch — congratulations! 🎉\n\n"
                        f"As a thank-you for being a valued client, we'd like to offer "
                        f"you a *free website performance & SEO audit* (worth £150).\n\n"
                        f"This will show you:\n"
                        f"• How Google is ranking your pages\n"
                        f"• Page speed scores and quick wins\n"
                        f"• Any technical issues to fix\n\n"
                        f"No commitment — just reply 'YES PLEASE' and we'll get it done "
                        f"within 48 hours.\n\n"
                        f"Also, if you know anyone who needs a website, we offer a "
                        f"£50 referral bonus!\n\n"
                        f"Best,\nBridge Digital Solutions\n{_COMPANY_EMAIL}"
                    )
                    if _send_email(client_email, subject, body):
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE bridge.review_sequences SET day30_sent_at = NOW() WHERE id = %s",
                                (seq_id,)
                            )
                        conn.commit()
                        sent += 1

        except Exception:
            try: conn.rollback()
            except Exception: pass
        finally:
            try: conn.close()
            except Exception: pass
        return sent
    except Exception:
        return 0


def register_delivery(client_name: str, client_email: str,
                      delivery_date: str | None = None,
                      project_id: str | None = None,
                      google_review_url: str | None = None) -> str:
    """
    Register a newly delivered project for automated review sequencing.
    Call this when a project is marked as delivered.
    delivery_date: ISO format YYYY-MM-DD, defaults to today.
    Returns the sequence ID.
    """
    try:
        import datetime as _dt2
        from ..memory.vector_memory import _get_pg_conn
        conn = _get_pg_conn()
        if not conn:
            return "DB unavailable"
        delivery = delivery_date or str(_dt2.date.today())
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bridge.review_sequences
                  (client_name, client_email, delivery_date, project_id, google_review_url)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (client_name, client_email, delivery, project_id, google_review_url))
            row = cur.fetchone()
        conn.commit()
        conn.close()
        if row:
            return str(row[0])
        return "already registered"
    except Exception as e:
        return f"error: {e}"
