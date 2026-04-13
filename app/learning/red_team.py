"""
Red team / adversarial challenge layer.

Haiku attacks every non-trivial response (complexity >= 3) looking for
one specific flaw, factual error, or dangerous assumption. If it finds
one (response is NOT "LGTM"), Claude Sonnet is escalated to provide a
corrected definitive answer.

This layer is OFF by default. Enable by setting CONFIDENCE_MODE=true
in Railway environment variables. The extra cost per query is:
  - 1 Haiku call (cheap)
  - 1 Claude call only when a flaw is found (escalation)

Red team runs AFTER peer review so it validates the already-improved answer.
"""
from .internal_llm import ask_internal
from ..prompts import RED_TEAM_PROMPT
from ..learning.insight_log import insight_log
from ..config import settings


class RedTeam:
    def challenge(
        self,
        query: str,
        response: str,
        complexity: int,
        session_id: str = "default",
    ) -> dict:
        """
        Adversarially challenge a response.

        Returns:
            {
                response: str,          # final answer (escalated or original)
                red_team_ran: bool,
                escalated: bool,
                red_verdict: str | None,
            }
        """
        _passthrough = {
            "response": response,
            "red_team_ran": False,
            "escalated": False,
            "red_verdict": None,
        }

        # Gate: off by default, only on non-trivial queries
        if not settings.confidence_mode or complexity < 3:
            return _passthrough

        # Don't attack already-errored responses
        if response.startswith("[") and response.endswith("]"):
            return _passthrough

        try:
            attack_prompt = RED_TEAM_PROMPT.format(query=query, response=response)
            verdict = ask_internal(attack_prompt)

            if verdict.startswith("[") and verdict.endswith("]"):
                # Haiku call failed — pass through silently
                return {
                    "response": response,
                    "red_team_ran": True,
                    "escalated": False,
                    "red_verdict": None,
                }

            insight_log.record(
                query, "HAIKU", verdict, "red_team", complexity, session_id
            )

            is_lgtm = verdict.strip().upper().startswith("LGTM")

            if is_lgtm:
                return {
                    "response": response,
                    "red_team_ran": True,
                    "escalated": False,
                    "red_verdict": verdict,
                }

            # Escalate to Claude with flaw context
            escalation_prompt = (
                f"Query: {query}\n\n"
                f"A previous response was:\n{response}\n\n"
                f"A reviewer found this specific flaw:\n{verdict}\n\n"
                f"Provide a corrected, definitive answer that fixes this flaw:"
            )
            escalated_response = ask_internal(escalation_prompt)

            if escalated_response.startswith("[") and escalated_response.endswith("]"):
                # Escalation failed — keep peer-reviewed answer
                escalated_response = response

            insight_log.record(
                query, "CLAUDE", escalated_response, "red_team_escalation",
                complexity, session_id,
            )

            return {
                "response": escalated_response,
                "red_team_ran": True,
                "escalated": True,
                "red_verdict": verdict,
            }

        except Exception:
            # Never let red team crash dispatch
            return _passthrough


# Singleton
red_team = RedTeam()
