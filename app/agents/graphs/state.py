"""Shared state for the plan→execute→critique→retry workflow."""
from __future__ import annotations

from typing import TypedDict


class WorkflowState(TypedDict, total=False):
    message: str
    session_id: str
    classification: str          # one of: github | shell | n8n | self_improve | research | engineering
    plan: str
    execution: str
    critique: str
    verdict: str                 # APPROVED | RETRY
    retries: int
    final: str
