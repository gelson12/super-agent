"""
Legion Refinement Layer — quality-improvement pipeline that runs after pick_winner().

Mirrors the quality stack in the main app (peer_review, red_team, cot_handoff) but
implemented natively inside Legion so every Legion response benefits, regardless of
which upstream model won.

Three passes, each gated to avoid latency/cost regression:

  Pass 1 — Cross-agent critique  (complexity >= 3)
    A DIFFERENT fast agent critiques the winner's answer.
    Prompt asks for ONE specific flaw. "LGTM" = answer is good enough, skip refinement.

  Pass 2 — Refinement re-answer  (if critique is substantive)
    The winner agent re-answers with the critique injected as context.
    Only replaces the original if the refined answer is measurably better.

  Pass 3 — Chain-of-thought boost  (complexity >= 4, opt-in)
    A reasoning-capable agent (deepseek R1 preferred) produces a reasoning trace.
    The winner agent then answers using that trace as grounding.
    Fires BEFORE Pass 1/2 so the refined answer already benefits from the trace.

All passes are failure-safe — any exception skips that pass and returns the
previous best answer. Refinement never degrades; it can only improve.

Configuration: controlled by the `refinement:` block in legion_config.yaml.
"""
from __future__ import annotations

import logging
import time

from app.agents.base import run_with_deadline
from app.models import AgentResponse, RespondRequest

log = logging.getLogger("legion.refine")

# ── Prompt templates ──────────────────────────────────────────────────────────

_CRITIQUE_PROMPT = """\
You are a senior technical reviewer holding answers to the standard of GPT-4o / Claude Sonnet.

Read the question and the draft answer below.

Evaluate against these criteria:
1. Correctness — no factual errors, no outdated information
2. Completeness — all sub-questions answered, nothing important omitted
3. Depth — reasoning shown, not just conclusions; examples or code included where helpful
4. Clarity — structured, easy to follow, not vague or hand-wavy

Identify the MOST IMPORTANT flaw or gap. Be specific (2–4 sentences max).

If the answer fully meets the standard above — correct, complete, well-reasoned, and clear —
respond with exactly: LGTM

Question:
{query}

Draft answer:
{answer}

Critique:"""

_REFINE_PROMPT = """\
{query}

[Quality review note: a senior reviewer found this issue with the previous draft:
{critique}

Rewrite your answer addressing this critique. Be thorough: show reasoning, include
examples or code where relevant, and ensure every part of the question is answered.]"""

_COT_REASON_PROMPT = """\
You are a reasoning specialist. Think through the following question carefully and
produce a detailed reasoning trace — intermediate steps, considerations, edge cases,
and key insights. This trace will be used to ground a final answer.

Do NOT write the final answer itself. Only the reasoning process.

Question:
{query}

Reasoning trace:"""

_COT_ANSWER_PROMPT = """\
{query}

[A reasoning specialist has already worked through this problem. Use the trace below
as your foundation — ground your answer in it, but write a clean, complete response
for the user (not a repetition of the trace):

{trace}]

Final answer:"""

# Minimum critique length (words) to be considered substantive
_CRITIQUE_MIN_WORDS = 8

# Preferred agents for critique, in priority order (fast/free-tier first)
_CRITIQUE_AGENT_PREFERENCE = [
    "groq", "cerebras", "sambanova", "github_models", "hf",
    "openrouter", "chatgpt", "mistral", "gemini_b",
]

# Preferred agents for CoT reasoning pass
_COT_AGENT_PREFERENCE = [
    "deepseek", "claude_b", "openrouter", "chatgpt", "groq", "cerebras",
]


# ── Helper: pick a critic / CoT agent ────────────────────────────────────────

def _pick_agent(
    preference: list[str],
    agents: dict[str, object],
    exclude: str,
) -> object | None:
    """Return the first enabled, non-excluded agent from preference list."""
    for aid in preference:
        if aid == exclude:
            continue
        agent = agents.get(aid)
        if agent and getattr(agent, "enabled", False):
            return agent
    # fallback: any enabled agent that isn't the winner
    for aid, agent in agents.items():
        if aid != exclude and getattr(agent, "enabled", False):
            return agent
    return None


# ── Pass 1+2: Cross-agent critique → conditional re-answer ───────────────────

async def _critique_and_refine(
    winner: AgentResponse,
    agents: dict[str, object],
    query: str,
    critique_deadline_ms: int,
    refine_deadline_ms: int,
) -> AgentResponse:
    """
    Ask a different agent to critique the winner's answer.
    If substantive, have the winner re-answer with the critique as context.
    Returns the best of (original, refined).
    """
    critic_agent = _pick_agent(_CRITIQUE_AGENT_PREFERENCE, agents, winner.agent_id)
    if critic_agent is None:
        log.debug("refine: no critic agent available — skipping critique pass")
        return winner

    critique_query = _CRITIQUE_PROMPT.format(
        query=query[:2000],
        answer=(winner.content or "")[:3000],
    )
    log.debug("refine: critiquing with agent=%s", getattr(critic_agent, "agent_id", "?"))
    critique_resp = await run_with_deadline(critic_agent, critique_query, critique_deadline_ms)

    if not critique_resp.success or not critique_resp.content:
        log.debug("refine: critic failed or empty — skipping critique pass")
        return winner

    critique_text = critique_resp.content.strip()

    # LGTM = no issues found, skip refinement
    if "lgtm" in critique_text.lower() and len(critique_text.split()) < 6:
        log.debug("refine: critic returned LGTM — answer is good as-is")
        return winner

    word_count = len(critique_text.split())
    if word_count < _CRITIQUE_MIN_WORDS:
        log.debug("refine: critique too short (%d words) — skipping", word_count)
        return winner

    log.info(
        "refine: substantive critique (%d words) from %s — requesting re-answer from %s",
        word_count, getattr(critic_agent, "agent_id", "?"), winner.agent_id,
    )

    # Ask the winner agent to re-answer with the critique injected
    refine_query = _REFINE_PROMPT.format(
        query=query[:2000],
        critique=critique_text[:800],
    )

    winner_agent = agents.get(winner.agent_id)
    if winner_agent is None or not getattr(winner_agent, "enabled", False):
        log.debug("refine: winner agent not available for re-answer — using original")
        return winner

    refined_resp = await run_with_deadline(winner_agent, refine_query, refine_deadline_ms)

    if not refined_resp.success or not refined_resp.content:
        log.debug("refine: re-answer failed — keeping original")
        return winner

    original_len = len(winner.content or "")
    refined_len = len(refined_resp.content)

    # Accept refined if it's meaningfully longer, or original was very short
    if refined_len >= original_len * 0.9 or original_len < 80:
        log.info(
            "refine: refined answer accepted (orig=%d chars, refined=%d chars)",
            original_len, refined_len,
        )
        # Return a new AgentResponse carrying the refined content but with
        # the winner's agent_id so scoring/logging stays consistent.
        return AgentResponse(
            agent_id=winner.agent_id,
            content=refined_resp.content,
            success=True,
            latency_ms=winner.latency_ms + refined_resp.latency_ms,
            self_confidence=max(winner.self_confidence, 0.8),
            cost_cents=winner.cost_cents + critique_resp.cost_cents + refined_resp.cost_cents,
        )

    log.debug(
        "refine: refined answer rejected (too short: %d < %d * 0.9) — keeping original",
        refined_len, original_len,
    )
    return winner


# ── Pass 3: Chain-of-thought boost ───────────────────────────────────────────

async def _cot_boost(
    winner: AgentResponse,
    agents: dict[str, object],
    query: str,
    cot_deadline_ms: int,
    answer_deadline_ms: int,
) -> AgentResponse:
    """
    Have a reasoning-capable agent produce a reasoning trace, then have the
    winner re-answer grounded in that trace. Fires for complexity >= 4 only.
    """
    cot_agent = _pick_agent(_COT_AGENT_PREFERENCE, agents, exclude="")
    if cot_agent is None:
        return winner

    log.debug("refine: CoT boost — reasoning with agent=%s", getattr(cot_agent, "agent_id", "?"))

    reason_query = _COT_REASON_PROMPT.format(query=query[:2000])
    reason_resp = await run_with_deadline(cot_agent, reason_query, cot_deadline_ms)

    if not reason_resp.success or not reason_resp.content:
        log.debug("refine: CoT reasoning failed — skipping CoT boost")
        return winner

    trace = reason_resp.content.strip()
    if len(trace) < 50:
        return winner

    log.info(
        "refine: CoT trace produced (%d chars) by %s — re-answering with %s",
        len(trace), getattr(cot_agent, "agent_id", "?"), winner.agent_id,
    )

    winner_agent = agents.get(winner.agent_id)
    if winner_agent is None or not getattr(winner_agent, "enabled", False):
        return winner

    answer_query = _COT_ANSWER_PROMPT.format(
        query=query[:2000],
        trace=trace[:3000],
    )
    boosted_resp = await run_with_deadline(winner_agent, answer_query, answer_deadline_ms)

    if not boosted_resp.success or not boosted_resp.content:
        return winner

    original_len = len(winner.content or "")
    boosted_len = len(boosted_resp.content)
    if boosted_len >= original_len * 0.85:
        log.info(
            "refine: CoT-boosted answer accepted (orig=%d, boosted=%d chars)",
            original_len, boosted_len,
        )
        return AgentResponse(
            agent_id=winner.agent_id,
            content=boosted_resp.content,
            success=True,
            latency_ms=winner.latency_ms + reason_resp.latency_ms + boosted_resp.latency_ms,
            self_confidence=max(winner.self_confidence, 0.82),
            cost_cents=winner.cost_cents + reason_resp.cost_cents + boosted_resp.cost_cents,
        )

    return winner


# ── Public entry point ────────────────────────────────────────────────────────

async def refine_winner(
    winner: AgentResponse,
    agents: dict[str, object],
    req: RespondRequest,
    time_budget_ms: int,
    refinement_cfg: dict,
) -> tuple[AgentResponse, bool]:
    """
    Run the refinement pipeline on the hive winner.

    Returns:
        (final_response, was_refined)
        was_refined=True if the content changed from the original winner.
    """
    if not refinement_cfg.get("enabled", True):
        return winner, False

    complexity_threshold = refinement_cfg.get("complexity_min", 3)
    if req.complexity < complexity_threshold:
        log.debug("refine: complexity=%d < threshold=%d — skipping", req.complexity, complexity_threshold)
        return winner, False

    if not winner.content or len(winner.content.strip()) < 20:
        return winner, False

    original_content = winner.content
    current = winner
    refine_start = time.monotonic()

    # ── Pass 3: CoT boost (complexity >= 4, before critique so critique sees CoT answer)
    if req.complexity >= 4 and refinement_cfg.get("cot_enabled", True):
        cot_budget = min(
            refinement_cfg.get("cot_deadline_ms", 15000),
            max(0, time_budget_ms - int((time.monotonic() - refine_start) * 1000) - 3000),
        )
        if cot_budget > 2000:
            try:
                current = await _cot_boost(
                    current, agents, req.query,
                    cot_deadline_ms=cot_budget // 2,
                    answer_deadline_ms=cot_budget // 2,
                )
            except Exception as exc:
                log.warning("refine: CoT boost raised %s — skipping", type(exc).__name__)

    # ── Pass 1+2: Cross-agent critique → conditional re-answer
    if refinement_cfg.get("critique_enabled", True):
        critique_budget = min(
            refinement_cfg.get("critique_deadline_ms", 6000),
            max(0, time_budget_ms - int((time.monotonic() - refine_start) * 1000) - 1000),
        )
        refine_budget = min(
            refinement_cfg.get("refine_deadline_ms", 10000),
            max(0, time_budget_ms - int((time.monotonic() - refine_start) * 1000) - 500),
        )
        if critique_budget > 1500 and refine_budget > 2000:
            try:
                current = await _critique_and_refine(
                    current, agents, req.query,
                    critique_deadline_ms=critique_budget,
                    refine_deadline_ms=refine_budget,
                )
            except Exception as exc:
                log.warning("refine: critique/refine raised %s — skipping", type(exc).__name__)

    was_refined = current.content != original_content
    return current, was_refined
