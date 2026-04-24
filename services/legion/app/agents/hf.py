from __future__ import annotations

import logging
import os
import time

import httpx

from app.hf.discovery import pick_model
from app.models import AgentResponse

log = logging.getLogger("legion.agent.hf")


class HFAgent:
    agent_id = "hf"

    def __init__(self) -> None:
        self.enabled = os.environ.get("HF_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("HF_API_KEY", "")
        self.base_url = "https://api-inference.huggingface.co/models"

    async def respond(
        self,
        query: str,
        deadline_ms: int,
        modality: str = "text",
        task: str = "chat",
    ) -> AgentResponse:
        if not self.enabled:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="disabled",
            )
        if not self.api_key:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=0, self_confidence=0.0, error_class="no_api_key",
            )
        start = time.monotonic()
        model = pick_model(task=task, modality=modality)
        if not model:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="no_model_found",
            )
        url = f"{self.base_url}/{model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=deadline_ms / 1000) as client:
                resp = await client.post(url, headers=headers, json={"inputs": query})
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="timeout",
            )
        except Exception as exc:
            log.warning("hf error on %s: %s", model, type(exc).__name__)
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=type(exc).__name__,
            )
        text = self._extract_text(data)
        if not text:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="empty_output",
            )
        return AgentResponse(
            agent_id=self.agent_id, content=text, success=True,
            latency_ms=int((time.monotonic() - start) * 1000),
            self_confidence=0.4,
        )

    @staticmethod
    def _extract_text(data) -> str:
        if isinstance(data, list) and data:
            head = data[0]
            if isinstance(head, dict):
                return (head.get("generated_text") or head.get("summary_text") or "").strip()
            if isinstance(head, str):
                return head.strip()
        if isinstance(data, dict):
            return (data.get("generated_text") or "").strip()
        return ""
