"""
Chain-of-Thought hand-off layer.

For classifier-routed queries at complexity >= 4, model A reasons through
the problem step-by-step WITHOUT answering, then model B receives the
reasoning trace and produces the final answer. This relay combines each
model's complementary strengths.

Model pairs:
  DEEPSEEK reasons → CLAUDE answers
    (structured technical analysis → nuanced writing/synthesis)
  CLAUDE reasons   → DEEPSEEK answers
    (deep analytical thinking → structured/code output)

An empty response string is returned when the gate condition is not met,
signalling the dispatcher to keep the standard single-model result.
"""
from ..models.deepseek import ask_deepseek
from .internal_llm import ask_internal
from ..prompts import COT_REASONING_PROMPT, COT_ANSWER_PROMPT
from ..learning.insight_log import insight_log

# (reasoning_model, answer_model) pairs
_COT_PAIRS: dict[str, tuple[str, str]] = {
    "DEEPSEEK": ("DEEPSEEK", "CLAUDE"),
    "CLAUDE":   ("CLAUDE",   "DEEPSEEK"),
}

_MODEL_CALLERS = {
    "CLAUDE":   ask_internal,   # CLI-first cascade instead of direct API
    "DEEPSEEK": ask_deepseek,
}


class CoTHandoff:
    def handoff(
        self,
        query: str,
        primary_model: str,
        routed_by: str,
        complexity: int,
        session_id: str = "default",
    ) -> dict:
        """
        Attempt a chain-of-thought hand-off.

        Returns:
            {
                response: str,           # empty string = gate not met, use primary result
                cot_used: bool,
                reasoning_model: str | None,
                answer_model: str | None,
                trace_length: int,
            }
        """
        _not_used = {
            "response": "",
            "cot_used": False,
            "reasoning_model": None,
            "answer_model": None,
            "trace_length": 0,
        }

        # Gate: only for classifier-routed complex queries with a known pair
        if complexity < 4 or routed_by != "classifier":
            return _not_used

        primary_model = primary_model.upper()
        pair = _COT_PAIRS.get(primary_model)
        if not pair:
            return _not_used

        reasoning_model, answer_model = pair
        reasoning_fn = _MODEL_CALLERS.get(reasoning_model)
        answer_fn = _MODEL_CALLERS.get(answer_model)
        if not reasoning_fn or not answer_fn:
            return _not_used

        try:
            # Step 1: Reasoning trace
            reasoning_prompt = COT_REASONING_PROMPT.format(query=query)
            trace = reasoning_fn(reasoning_prompt, system="")

            if trace.startswith("[") and trace.endswith("]"):
                return _not_used

            # Step 2: Answer using trace
            answer_prompt = COT_ANSWER_PROMPT.format(trace=trace, query=query)
            response = answer_fn(answer_prompt, system="")

            if response.startswith("[") and response.endswith("]"):
                return _not_used

            insight_log.record(
                query, answer_model, response, "cot_handoff", complexity, session_id
            )

            return {
                "response": response,
                "cot_used": True,
                "reasoning_model": reasoning_model,
                "answer_model": answer_model,
                "trace_length": len(trace),
            }

        except Exception:
            return _not_used


# Singleton
cot_handoff = CoTHandoff()
