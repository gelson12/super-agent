from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Header, HTTPException, Request

from app.config import settings

MAX_SKEW_S = 30


def _expected_sig(secret: str, ts: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(ts.encode())
    mac.update(b"\n")
    mac.update(body)
    return mac.hexdigest()


async def require_hmac(
    request: Request,
    x_legion_ts: str = Header(...),
    x_legion_sig: str = Header(...),
) -> None:
    secret = settings.LEGION_API_SHARED_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="server not configured")
    try:
        ts_int = int(x_legion_ts)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="bad ts") from exc
    if abs(time.time() - ts_int) > MAX_SKEW_S:
        raise HTTPException(status_code=401, detail="ts skew")
    body = await request.body()
    expected = _expected_sig(secret, x_legion_ts, body)
    if not hmac.compare_digest(expected, x_legion_sig):
        raise HTTPException(status_code=401, detail="bad sig")


def sign(secret: str, body: bytes, ts: int | None = None) -> tuple[str, str]:
    if ts is None:
        ts = int(time.time())
    ts_str = str(ts)
    return ts_str, _expected_sig(secret, ts_str, body)
