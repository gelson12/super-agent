"""
SambaNova Cloud agent — OpenAI-compatible /v1/chat/completions.

Why SambaNova: ~500-1000 tok/s inference on Llama-3.3-70B (faster than
Cerebras for the same family) on a free tier with no credit card.
Their hardware is custom RDU silicon, so latency profile is genuinely
different from GPU-backed providers — useful for early-termination
wins in the hive engine.

Verified free-tier model IDs (late 2025):
  Meta-Llama-3.3-70B-Instruct        (default)
  Meta-Llama-3.1-405B-Instruct       (huge, slower but very strong)
  Meta-Llama-3.1-70B-Instruct
  Meta-Llama-3.1-8B-Instruct         (lightweight)
  Llama-3.2-1B/3B-Instruct           (tiny, very fast)
  Llama-3.2-11B/90B-Vision-Instruct  (multimodal)
  Qwen2.5-72B-Instruct
  DeepSeek-R1-Distill-Llama-70B      (reasoning)

Override the default with SAMBANOVA_MODEL.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from app.models import AgentResponse

log = logging.getLogger("legion.agent.sambanova")


class SambaNovaAgent:
    agent_id = "sambanova"

    def __init__(self) -> None:
        self.enabled = os.environ.get("SAMBANOVA_ENABLED", "false").lower() == "true"
        self.api_key = os.environ.get("SAMBANOVA_API_KEY", "")
        self.model = os.environ.get(
            "SAMBANOVA_MODEL", "Meta-Llama-3.3-70B-Instruct",
        )
        self.base_url = os.environ.get(
            "SAMBANOVA_BASE_URL", "https://api.sambanova.ai/v1",
        )

    async def respond(self, query: str, deadline_ms: int) -> AgentResponse:
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
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "max_tokens": 1024,
            "temperature": 0.7,
        }
        try:
            async with httpx.AsyncClient(timeout=deadline_ms / 1000) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="timeout",
            )
        except httpx.HTTPStatusError as exc:
            body_snippet = exc.response.text[:300].replace("\n", " ")
            log.warning(
                "sambanova HTTP %s on model=%s body=%s",
                exc.response.status_code, self.model, body_snippet,
            )
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=f"http_{exc.response.status_code}",
            )
        except Exception as exc:
            log.warning("sambanova error: %s", type(exc).__name__)
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class=type(exc).__name__,
            )
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError):
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="malformed_response",
            )
        if not text:
            return AgentResponse(
                agent_id=self.agent_id, content=None, success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                self_confidence=0.0, error_class="empty_output",
            )
        return AgentResponse(
            agent_id=self.agent_id, content=text, success=True,
            latency_ms=int((time.monotonic() - start) * 1000),
            self_confidence=0.65, cost_cents=0.0,
        )
