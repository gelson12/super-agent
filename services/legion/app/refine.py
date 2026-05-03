"""
Legion Refinement Layer — quality-improvement pipeline that runs after pick_winner().

Four passes, each gated to avoid latency/cost regression.
Pipeline order (all failure-safe — any exception skips that pass):

  Pass 0 — MoA Synthesis       (complexity >= 2, runner_up available)
    Mixture-of-Agents: fuse winner + runner-up into one superior answer.
    Only fires when models DISAGREE (word overlap < 50%) — if they agree,
    synthesis would just produce a longer version of the same thing.
    Based on Together AI's MoA paper: consistently outperforms best-of-N selection.

  Pass 3 — Chain-of-thought boost  (complexity >= 4)
    A reasoning-capable agent produces a thinking trace.
    The winner re-answers grounded in that trace.
    Fires before critique so the refined answer already benefits from CoT.

  Pass 1+2 — Cross-agent critique → conditional re-answer  (complexity >= 3)
    A DIFFERENT fast agent critiques the current best answer.
    "LGTM" = skip. Substantive critique → winner re-answers with it injected.
    Only replaces original if refined answer is measurably better.

Refinement never degrades — any pass that produces a worse result is discarded.

Quota-awareness: _pick_agent() skips agents currently in a 429-cooldown window
so refinement calls don't steal quota from the primary hive.

Configuration: controlled by the `refinement:` block in legion_config.yaml.
"""
from __future__ import annotations

import logging
import time

from app import quota_state
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

_SYNTHESIS_PROMPT = """\
Two independent AI models answered the same question. Synthesize them into one \
superior answer — extract the strongest reasoning, facts, and examples from each, \
reconcile any differences, and produce the most complete and accurate response possible.

Do NOT copy one response verbatim. Do NOT list both answers side-by-side. \
Write a single, unified answer in your own words that is better than either input alone.

Question:
{query}

Model A answer:
{answer_a}

Model B answer:
{answer_b}

Synthesized answer:"""

# Minimum critique length (words) to be considered substantive
_CRITIQUE_MIN_WORDS = 8

# Preferred agents for MoA synthesis (fast API agents first)
_SYNTHESIS_AGENT_PREFERENCE = [
    "groq", "cerebras", "sambanova", "github_models",
    "chatgpt", "openrouter", "mistral", "glm", "claude_b",
]

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
    """
    Return the first enabled, non-excluded, quota-available agent from preference list.
    Agents in a 429-cooldown window are skipped to preserve their quota for hive calls.
    Falls back to any available agent if all preferred ones are exhausted.
    """
    def _is_quota_ok(aid: str, agent: object) -> bool:
        model = getattr(agent, "model", aid)
        return not quota_state.is_exhausted(aid, model)

    # Pass 1: preferred order, quota-available only
    for aid in preference:
        if aid == exclude:
            continue
        agent = agents.get(aid)
        if agent and getattr(agent, "enabled", False) and _is_quota_ok(aid, agent):
            return agent

    # Pass 2: any enabled non-excluded agent that isn't quota-exhausted
    for aid, agent in agents.items():
        if aid != exclude and getattr(agent, "enabled", False) and _is_quota_ok(aid, agent):
            return agent

    # Pass 3: last resort — any enabled non-excluded agent even if quota-exhausted
    # (better to try and get a 429 than skip refinement entirely)
    for aid, agent in agents.items():
        if aid != exclude and getattr(agent, "enabled", False):
            return agent

    return None


# ── Pass 0: MoA Synthesis ────────────────────────────────────────────────────

def _word_overlap(a: str, b: str) -> float:
    """Jaccard similarity on word sets. 1.0 = identical, 0.0 = no shared words."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


async def _synthesis_pass(
    winner: AgentResponse,
    runner_up: AgentResponse,
    agents: dict[str, object],
    query: str,
    budget_ms: int,
) -> AgentResponse:
    """
    Mixture-of-Agents: fuse the top-2 responses into one answer that exceeds either.

    Skipped when overlap > 50% — models essentially agree, so synthesis would just
    produce a longer version of the same content (not worth the latency).
    When models disagree, synthesis captures the best of both worlds and consistently
    outperforms best-of-N selection (Together AI MoA paper, 2024).
    """
    overlap = _word_overlap(winner.content or "", runner_up.content or "")
    if overlap > 0.50:
        log.debug("refine: synthesis skipped — models agree (overlap=%.2f)", overlap)
        return winner

    synth_agent = _pick_agent(_SYNTHESIS_AGENT_PREFERENCE, agents, exclude="")
    if synth_agent is None:
        log.debug("refine: synthesis skipped — no available agent")
        return winner

    synth_query = _SYNTHESIS_PROMPT.format(
        query=query[:2000],
        answer_a=(winner.content or "")[:2000],
        answer_b=(runner_up.content or "")[:2000],
    )
    log.info(
        "refine: MoA synthesis — %s + %s → %s (overlap=%.2f)",
        winner.agent_id, runner_up.agent_id,
        getattr(synth_agent, "agent_id", "?"), overlap,
    )
    synth_resp = await run_with_deadline(synth_agent, synth_query, budget_ms)

    if not synth_resp.success or not synth_resp.content:
        log.debug("refine: synthesis agent failed — keeping winner")
        return winner

    synth_len  = len(synth_resp.content)
    winner_len = len(winner.content or "")

    if synth_len >= winner_len * 0.75:
        log.info(
            "refine: synthesis accepted (orig=%d chars → synth=%d chars)",
            winner_len, synth_len,
        )
        return AgentResponse(
            agent_id=f"{winner.agent_id}+{runner_up.agent_id}→synth",
            content=synth_resp.content,
            success=True,
            latency_ms=winner.latency_ms + synth_resp.latency_ms,
            self_confidence=max(winner.self_confidence, 0.85),
            cost_cents=winner.cost_cents + synth_resp.cost_cents,
        )

    log.debug("refine: synthesis too short (%d < %d*0.75) — keeping winner", synth_len, winner_len)
    return winner


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
    runner_up: AgentResponse | None = None,
) -> tuple[AgentResponse, bool]:
    """
    Run the full refinement pipeline on the hive winner.

    Pipeline (each pass failure-safe, skipped when time runs out):
      Pass 0 — MoA Synthesis (complexity >= 2, runner_up exists, models disagree)
      Pass 3 — CoT boost     (complexity >= 4)
      Pass 1+2 — Critique → re-answer (complexity >= 3)

    Returns (final_response, was_refined).
    """
    if not refinement_cfg.get("enabled", True):
        return winner, False

    # MoA synthesis fires from complexity 2 upwards — lower bar than critique
    synthesis_threshold = refinement_cfg.get("synthesis_complexity_min", 2)
    critique_threshold  = refinement_cfg.get("complexity_min", 3)

    # Skip the whole pipeline only if even synthesis wouldn't fire
    if req.complexity < synthesis_threshold:
        log.debug("refine: complexity=%d below synthesis threshold=%d — skipping all",
                  req.complexity, synthesis_threshold)
        return winner, False

    if not winner.content or len(winner.content.strip()) < 20:
        return winner, False

    original_content = winner.content
    current = winner
    refine_start = time.monotonic()

    def _remaining() -> int:
        return max(0, time_budget_ms - int((time.monotonic() - refine_start) * 1000))

    # ── Pass 0: MoA Synthesis (complexity >= 2, runner_up available) ─────────
    if (
        req.complexity >= synthesis_threshold
        and runner_up is not None
        and runner_up.success
        and runner_up.content
        and _remaining() > 4000
    ):
        synth_budget = min(refinement_cfg.get("synthesis_deadline_ms", 10000), _remaining() - 2000)
        if synth_budget > 3000:
            try:
                current = await _synthesis_pass(
                    current, runner_up, agents, req.query, synth_budget
                )
            except Exception as exc:
                log.warning("refine: synthesis raised %s — skipping", type(exc).__name__)

    # ── Pass 3: CoT boost (complexity >= 4, before critique) ─────────────────
    if req.complexity >= 4 and refinement_cfg.get("cot_enabled", True) and _remaining() > 4000:
        cot_budget = min(refinement_cfg.get("cot_deadline_ms", 15000), _remaining() - 3000)
        if cot_budget > 2000:
            try:
                current = await _cot_boost(
                    current, agents, req.query,
                    cot_deadline_ms=cot_budget // 2,
                    answer_deadline_ms=cot_budget // 2,
                )
            except Exception as exc:
                log.warning("refine: CoT boost raised %s — skipping", type(exc).__name__)

    # ── Pass 1+2: Cross-agent critique → conditional re-answer ───────────────
    if req.complexity >= critique_threshold and refinement_cfg.get("critique_enabled", True):
        critique_budget = min(refinement_cfg.get("critique_deadline_ms", 6000), _remaining() - 1000)
        refine_budget   = min(refinement_cfg.get("refine_deadline_ms", 10000), _remaining() - 500)
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
