from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("legion.suitability")


RUBRIC_SYSTEM = (
    "You are an agent-routing classifier. Score each candidate agent's suitability "
    "for answering the user query on a 0.0-1.0 scale. Consider the query's modality "
    "(code/chat/summarize/qa), length, complexity, and whether it needs real-time "
    "data, code execution, or long context.\n\n"
    "Respond with JSON ONLY, no prose, no markdown:\n"
    '{"agent_id": 0.85, ...}'
)


def _heuristic(query: str, agent_ids: list[str]) -> dict[str, float]:
    is_code = bool(re.search(
        r"\b(def |class |function|import |const |let |var |SELECT |curl |```)",
        query,
    ))
    is_chat = len(query) < 200 and not is_code
    scores: dict[str, float] = {}
    code_agents = (
        "claude_b", "chatgpt", "groq", "cerebras", "github_models",
        "openrouter", "mistral", "sambanova", "deepseek",
    )
    chat_agents = (
        "gemini_b", "ollama", "chatgpt", "groq", "cerebras",
        "github_models", "openrouter", "sambanova", "deepseek",
    )
    api_general = (
        "chatgpt", "groq", "cerebras", "github_models", "openrouter",
        "mistral", "sambanova", "deepseek",
    )
    for aid in agent_ids:
        if is_code and aid in code_agents:
            # Specialists score above the generalist code baseline (0.75):
            #   claude_b → strongest code agent overall
            #   mistral  → Codestral is purpose-built for code
            #   deepseek → R1 reasoning helps for complex multi-step code
            #   sambanova→ very fast Llama-70B, great for early-termination
            if aid == "claude_b":
                scores[aid] = 0.80
            elif aid == "mistral":
                scores[aid] = 0.80
            elif aid == "deepseek":
                scores[aid] = 0.78
            elif aid == "sambanova":
                scores[aid] = 0.77
            else:
                scores[aid] = 0.75
        elif is_chat and aid in chat_agents:
            scores[aid] = 0.70
        elif aid in api_general:
            scores[aid] = 0.65  # general-purpose default
        elif aid == "hf":
            scores[aid] = 0.45
        else:
            scores[aid] = 0.55
    return scores


async def classify(query: str, agent_ids: list[str]) -> dict[str, float]:
    """
    Return {agent_id: suitability_score}. Uses Claude Haiku when
    ANTHROPIC_API_KEY_HAIKU_CLASSIFIER is set, else deterministic heuristic.
    """
    if not agent_ids:
        return {}
    api_key = os.environ.get("ANTHROPIC_API_KEY_HAIKU_CLASSIFIER")
    if not api_key:
        return _heuristic(query, agent_ids)
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic sdk not installed — falling back to heuristic")
        return _heuristic(query, agent_ids)
    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        user_msg = (
            f"Query: {query[:500]}\n\n"
            f"Agents: {', '.join(agent_ids)}\n\n"
            "Reply with JSON scores only."
        )
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=RUBRIC_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text if resp.content else ""
        parsed = json.loads(text)
        return {
            aid: max(0.0, min(1.0, float(parsed.get(aid, 0.5))))
            for aid in agent_ids
        }
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        log.warning("haiku parse failed: %s", type(exc).__name__)
        return _heuristic(query, agent_ids)
    except Exception as exc:
        log.warning("haiku classifier error: %s", type(exc).__name__)
        return _heuristic(query, agent_ids)


def shortlist(scores: dict[str, float], k: int = 3, max_k: int = 5) -> list[str]:
    effective_k = min(max(k, 1), max_k)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [aid for aid, _ in ranked[:effective_k]]
