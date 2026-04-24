from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


Modality = Literal["text", "code", "chat", "summarize", "qa", "vision", "audio", "other"]


class RespondRequest(BaseModel):
    query: str = Field(min_length=1)
    complexity: int = Field(default=3, ge=1, le=5)
    modality: Modality = "text"
    deadline_ms: int = Field(default=8000, ge=500, le=60_000)
    budget_cents: float = Field(default=2.0, ge=0.0)
    shortlist_override: list[str] | None = None


class AgentResponse(BaseModel):
    agent_id: str
    content: str | None
    success: bool
    latency_ms: int
    self_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    cost_cents: float = 0.0
    error_class: str | None = None


class RespondResponse(BaseModel):
    round_id: UUID
    winner_agent: str
    content: str
    latency_ms: int
    agents_entered: list[str]
    scores: dict[str, float]
    early_terminated: bool = False
