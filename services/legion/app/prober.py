"""
Background agent health prober. Supervisord launches this as a separate
long-running process. Every PROBE_INTERVAL_S it walks every enabled
agent in the registry, sends a tiny ping prompt, and records the
outcome to the agent_health PG table. Skipped agents:

  * disabled flag false
  * agent not registered (e.g. ChatGPT without OPENAI_API_KEY)
  * recent successful real traffic in the last SKIP_RECENT_S window —
    no point burning quota on a probe when we know the path is alive

The prober is failure-safe: any single probe error is logged and moves
on to the next agent. The whole loop never raises out to supervisord.

Cost budget: 7 enabled agents × 1 probe per cycle × 96 cycles/day (15min
interval) = 672 probes/day. For Gemini's 250/day flash quota that's
~30/hour just on probes — too many. So default interval is 30 min
(48 cycles/day, ~6/hour), and the query is the shortest possible
("OK") so even the heaviest tokens-charged providers stay cheap.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress

import httpx

from app import db
from app.agents.cerebras import CerebrasAgent
from app.agents.chatgpt import ChatGPTAgent
from app.agents.claude_b import ClaudeBAgent
from app.agents.gemini_b import GeminiBAgent
from app.agents.github_models import GitHubModelsAgent
from app.agents.groq import GroqAgent
from app.agents.hf import HFAgent
from app.agents.ollama import OllamaAgent
from app.agents.openrouter import OpenRouterAgent
from app.redact import install_root_filter

log = logging.getLogger("legion.prober")

PROBE_INTERVAL_S = int(os.environ.get("LEGION_PROBE_INTERVAL_S", "1800"))  # 30 min
PROBE_QUERY = os.environ.get("LEGION_PROBE_QUERY", "Reply with the single word ok.")
PROBE_DEADLINE_MS = int(os.environ.get("LEGION_PROBE_DEADLINE_MS", "20000"))
OLLAMA_READY_TIMEOUT_S = int(os.environ.get("LEGION_OLLAMA_READY_TIMEOUT_S", "180"))


async def _wait_ollama_ready(timeout_s: int = OLLAMA_READY_TIMEOUT_S) -> bool:
    """
    Poll Ollama's /api/ps until at least one model is loaded into RAM, OR
    until timeout_s elapses. Called once at prober startup so the first
    ollama probe doesn't race against ollama-bootstrap's CPU warmup
    (which takes ~35s on the Railway VM for llama3.2:3b).

    Returns True if a model is loaded, False on timeout. Failure is
    non-fatal — the prober still walks ollama in subsequent cycles.
    """
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    base = host if host.startswith("http") else f"http://{host}"
    url = f"{base.rstrip('/')}/api/ps"
    deadline = time.monotonic() + timeout_s
    poll_interval = 5.0
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    models = (resp.json() or {}).get("models") or []
                    if models:
                        loaded = ",".join(m.get("name", "?") for m in models)
                        log.info("prober: ollama ready (loaded=%s)", loaded)
                        return True
        except Exception:
            pass  # ollama not up yet — keep polling
        await asyncio.sleep(poll_interval)
    log.warning(
        "prober: ollama not ready after %ds — first probe may time out, will recover next cycle",
        timeout_s,
    )
    return False


def _build_registry() -> dict[str, object]:
    """Mirror lifespan() in main.py — same agents, same instantiation."""
    return {
        "gemini_b": GeminiBAgent(),
        "ollama": OllamaAgent(),
        "hf": HFAgent(),
        "claude_b": ClaudeBAgent(),
        "chatgpt": ChatGPTAgent(),
        "groq": GroqAgent(),
        "openrouter": OpenRouterAgent(),
        "cerebras": CerebrasAgent(),
        "github_models": GitHubModelsAgent(),
    }


async def _probe_one(agent_id: str, agent: object) -> None:
    enabled = bool(getattr(agent, "enabled", False))
    if not enabled:
        # Don't burn a PG row for permanently-disabled agents (no key).
        return
    start = time.monotonic()
    try:
        response = await asyncio.wait_for(
            agent.respond(PROBE_QUERY, PROBE_DEADLINE_MS),
            timeout=PROBE_DEADLINE_MS / 1000 + 5,
        )
    except asyncio.TimeoutError:
        await _record(agent_id, "fail", None, "probe_timeout")
        return
    except Exception as exc:
        await _record(agent_id, "fail", None, f"probe_exc_{type(exc).__name__}")
        return
    latency_ms = int((time.monotonic() - start) * 1000)
    if response.success:
        await _record(agent_id, "ok", latency_ms, None)
        return
    err = response.error_class or "unknown"
    if "quota" in err or "rate_limit" in err or err.startswith("http_429"):
        status = "rate_limited" if "rate" in err else "quota_exhausted"
    elif err in ("no_api_key", "disabled", "binary_not_found"):
        status = err
    else:
        status = "fail"
    await _record(agent_id, status, latency_ms, err)


async def _record(agent_id: str, status: str, latency_ms: int | None, error: str | None) -> None:
    with suppress(Exception):
        await db.upsert_agent_health(
            agent_id=agent_id,
            model_id="",  # per-model granularity is tracked separately by quota_state
            status=status,
            latency_ms=latency_ms,
            error=error,
        )
    log.info("probe[%s] = %s lat=%s err=%s", agent_id, status, latency_ms, error)


async def _loop() -> None:
    await db.startup()
    try:
        registry = _build_registry()
        log.info(
            "prober: starting — interval=%ds query=%r agents=%s",
            PROBE_INTERVAL_S, PROBE_QUERY[:60],
            sorted(registry.keys()),
        )
        # If Ollama is enabled, wait for ollama-bootstrap to finish warmup
        # before the first cycle. Otherwise the very first ollama probe
        # races the cold-load and fails with timeout=20s. This block costs
        # at most OLLAMA_READY_TIMEOUT_S on first boot, then no-ops on
        # subsequent restarts (model stays resident on the volume).
        ollama_agent = registry.get("ollama")
        if ollama_agent is not None and getattr(ollama_agent, "enabled", False):
            await _wait_ollama_ready()
        while True:
            cycle_start = time.monotonic()
            for agent_id, agent in registry.items():
                try:
                    await _probe_one(agent_id, agent)
                except Exception as exc:
                    log.warning("probe[%s] crashed: %s", agent_id, type(exc).__name__)
            elapsed = time.monotonic() - cycle_start
            log.info("prober: cycle done in %.1fs", elapsed)
            sleep_for = max(PROBE_INTERVAL_S - int(elapsed), 30)
            await asyncio.sleep(sleep_for)
    finally:
        await db.shutdown()


def main() -> None:
    install_root_filter()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if os.environ.get("LEGION_ENABLED", "false").lower() != "true":
        log.info("prober: LEGION_ENABLED=false — exiting cleanly")
        return
    if os.environ.get("LEGION_PROBER_ENABLED", "true").lower() != "true":
        log.info("prober: LEGION_PROBER_ENABLED=false — exiting cleanly")
        return
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        log.info("prober: SIGINT, exiting")


if __name__ == "__main__":
    main()
