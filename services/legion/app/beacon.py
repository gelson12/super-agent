"""
Primary-beacon endpoint. Inspiring-cat POSTs here every 15s when its Claude
CLI (account A) is healthy. Legion uses this as a low-latency signal that A
is still ACTIVE and Legion should stay PASSIVE. Postgres remains source of
truth; this is a latency optimisation.

HMAC-verified with PRIMARY_BEACON_SECRET to prevent spoofing.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger("legion.beacon")

MAX_SKEW_S = 30


@dataclass
class PrimaryState:
    last_beacon_ts: float = 0.0
    last_health_score: float = 0.0
    consecutive_beacons: int = 0


_state = PrimaryState()


def get_primary_state() -> PrimaryState:
    return _state


def primary_healthy(min_health: float = 0.7, max_age_s: int = 45) -> bool:
    """
    Return True if we've received a healthy beacon recently. Used by the
    hive to decide whether to respond or defer back to primary.
    """
    if _state.last_beacon_ts == 0:
        return False
    age = time.time() - _state.last_beacon_ts
    return age <= max_age_s and _state.last_health_score >= min_health


router = APIRouter()


@router.post("/legion/primary-beacon")
async def primary_beacon(request: Request) -> dict:
    secret = os.environ.get("PRIMARY_BEACON_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="beacon not configured")

    ts_header = request.headers.get("X-Beacon-Ts", "")
    sig_header = request.headers.get("X-Beacon-Sig", "")
    body = await request.body()

    try:
        ts_int = int(ts_header)
    except ValueError:
        raise HTTPException(status_code=401, detail="bad ts")
    if abs(time.time() - ts_int) > MAX_SKEW_S:
        raise HTTPException(status_code=401, detail="ts skew")

    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(ts_header.encode())
    mac.update(b"\n")
    mac.update(body)
    expected = mac.hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        raise HTTPException(status_code=401, detail="bad sig")

    try:
        payload = await request.json()
        health = float(payload.get("health_score", 1.0))
    except Exception:
        health = 1.0

    _state.last_beacon_ts = time.time()
    _state.last_health_score = max(0.0, min(1.0, health))
    _state.consecutive_beacons += 1

    return {"ok": True, "received_at": _state.last_beacon_ts}
