"""
Cross-model peer review layer.

After the primary model answers a complex query (complexity >= 4),
a rival model critiques the response. If the critique is substantive
(> 50 words), the primary model re-answers with the critique injected
as additional context — improving its own answer.

Critic assignment:
  CLAUDE   → GEMINI   critiques
  DEEPSEEK → HAIKU    critiques
  GEMINI   → DEEPSEEK critiques
  HAIKU    → CLAUDE   critiques

This ensures each model is always reviewed by a complementary one:
Claude's nuanced answers are checked by Gemini's factual precision;
DeepSeek's code is spot-checked cheaply by Haiku, etc.
"""
from typing import Optional

from ..models.gemini import ask_gemini
from ..models.deepseek import ask_deepseek
from .claude_code_worker import ask_claude_code as _peer_ask_cli
from .internal_llm import ask_internal
from ..prompts import PEER_REVIEW_PROMPT
from ..learning.insight_log import insight_log

_WORKER_IDS = {
    "CLAUDE": "Claude CLI Pro", "GEMINI": "Gemini CLI",
    "DEEPSEEK": "DeepSeek", "HAIKU": "Anthropic Haiku",
}

def _talking(a: str, b: str) -> None:
    try:
        from .agent_status_tracker import mark_talking as _mt
        _mt(a, b)
    except Exception:
        pass

def _clear(a: str, b: str) -> None:
    try:
        from .agent_status_tracker import clear_talking as _ct
        _ct(a, b)
    except Exception:
        pass

# ── Constants ─────────────────────────────────────────────────────────────────

_CRITIC_MAP: dict[str, str] = {
    "CLAUDE":   "GEMINI",
    "DEEPSEEK": "HAIKU",
    "GEMINI":   "DEEPSEEK",
    "HAIKU":    "CLAUDE",
}

_MODEL_CALLERS = {
    "CLAUDE":   lambda prompt, system="": _peer_ask_cli(prompt),
    "DEEPSEEK": ask_deepseek,
    "GEMINI":   ask_gemini,
    "HAIKU":    lambda prompt, system="": ask_internal(prompt, system),
}

_SUBSTANTIVE_WORD_THRESHOLD = 50


class PeerReviewer:
    def review(
        self,
        query: str,
        response: str,
        primary_model: str,
        complexity: int,
        session_id: str = "default",
    ) -> dict:
        """
        Run cross-model peer review.

        Returns:
            {
                final_response: str,
                was_reviewed: bool,
                critic_model: str | None,
                critique_was_substantive: bool,
                critique: str | None,
            }
        """
        _not_reviewed = {
            "final_response": response,
            "was_reviewed": False,
            "critic_model": None,
            "critique_was_substantive": False,
            "critique": None,
        }

        # Gate: only run on complex queries
        if complexity < 4:
            return _not_reviewed

        # Skip if response itself is already an error
        if response.startswith("[") and response.endswith("]"):
            return _not_reviewed

        primary_model = primary_model.upper()
        critic_model = _CRITIC_MAP.get(primary_model)
        if not critic_model:
            return _not_reviewed

        critic_fn = _MODEL_CALLERS.get(critic_model)
        if not critic_fn:
            return _not_reviewed

        try:
            # Step 1: Critique — show line between primary and critic
            _wa = _WORKER_IDS.get(primary_model, primary_model)
            _wb = _WORKER_IDS.get(critic_model, critic_model)
            _talking(_wa, _wb)
            critique_prompt = PEER_REVIEW_PROMPT.format(query=query, response=response)
            critique = critic_fn(critique_prompt, system="")
            _clear(_wa, _wb)

            # Error from critic model — skip synthesis
            if critique.startswith("[") and critique.endswith("]"):
                return {
                    "final_response": response,
                    "was_reviewed": True,
                    "critic_model": critic_model,
                    "critique_was_substantive": False,
                    "critique": critique,
                }

            critique_words = len(critique.split())
            is_substantive = critique_words > _SUBSTANTIVE_WORD_THRESHOLD

            # Log the critique interaction
            insight_log.record(
                query, critic_model, critique, "peer_review", complexity, session_id
            )

            if not is_substantive:
                return {
                    "final_response": response,
                    "was_reviewed": True,
                    "critic_model": critic_model,
                    "critique_was_substantive": False,
                    "critique": critique,
                }

            # Step 2: Primary model re-answers with critique context
            primary_fn = _MODEL_CALLERS.get(primary_model)
            if not primary_fn:
                return {
                    "final_response": response,
                    "was_reviewed": True,
                    "critic_model": critic_model,
                    "critique_was_substantive": True,
                    "critique": critique,
                }

            synthesis_prompt = (
                f"Original question: {query}\n\n"
                f"Your previous response:\n{response}\n\n"
                f"A reviewer noted:\n{critique}\n\n"
                f"Provide an improved response that addresses the critique:"
            )
            final_response = primary_fn(synthesis_prompt, system="")

            # Fall back to original if synthesis fails
            if final_response.startswith("[") and final_response.endswith("]"):
                final_response = response

            return {
                "final_response": final_response,
                "was_reviewed": True,
                "critic_model": critic_model,
                "critique_was_substantive": True,
                "critique": critique,
            }

        except Exception:
            # Never let peer review crash the dispatch
            return _not_reviewed


# Singleton
peer_reviewer = PeerReviewer()
