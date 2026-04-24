"""
POST /webhook/verification-code — receives magic-link code/url from n8n
workflow `jxnZZwTqJ7naPKc6` for Account B recovery.

Payload contract (matches inspiring-cat's equivalent so the shared n8n
workflow can route to either container):

    Header X-Webhook-Secret: equals env LEGION_WEBHOOK_SECRET
    Header X-Account-Target: "B"
    Body   {"url": "...", "code": "123456"?, "account": "B"}

Only responses for account_target='B' are consumed; anything else is silently
ack'd and dropped (n8n occasionally misroutes during the A/B detection when
the inbox receives a link for both accounts simultaneously).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Header, HTTPException, Request

from app.verification_queue import VerificationMessage, put

log = logging.getLogger("legion.webhook")
router = APIRouter()


@router.post("/webhook/verification-code")
async def verification_code(
    request: Request,
    x_webhook_secret: str = Header(default=""),
    x_account_target: str = Header(default="B"),
) -> dict:
    expected = os.environ.get("LEGION_WEBHOOK_SECRET", "")
    if not expected:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if not x_webhook_secret or x_webhook_secret != expected:
        raise HTTPException(status_code=401, detail="bad secret")
    target = (x_account_target or "B").upper()
    if target != "B":
        log.info("webhook: ignoring non-B account target %s", target)
        return {"ok": True, "ignored": True}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    msg = VerificationMessage(
        url=payload.get("url"),
        code=payload.get("code"),
        account=target,
    )
    await put(msg)
    log.info(
        "webhook: enqueued verification message (account=%s, has_code=%s, has_url=%s)",
        target, bool(msg.code), bool(msg.url),
    )
    return {"ok": True}
