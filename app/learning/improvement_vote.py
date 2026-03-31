"""
Multi-model consensus vote for medium/high priority improvement suggestions.

All 5 models are asked the same structured question:
  Claude Sonnet, Claude Haiku, DeepSeek, Gemini, Claude Code CLI

Threshold: 3 of 5 YES votes = approved.
Failed/unavailable models count as abstain, not as NO.
"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

_VOTE_PROMPT_TEMPLATE = """\
You are reviewing a proposed code improvement for Super Agent running in production.
Should this improvement be applied autonomously without a human safe word?

Feature: {feature_name}
Priority: {priority}
Observation: {observation}
Proposed change: {suggested_improvement}
File to change: {file_to_change}

Reply ONLY in this exact format (two lines, nothing else):
VOTE: YES
REASON: one sentence explaining your decision"""

_THRESHOLD = 3  # out of 5


def _parse_vote(response: str) -> tuple[str, str]:
    """Return (vote, reason) from a model's raw response. Defaults to NO on parse failure."""
    vote = "NO"
    reason = "parse error"
    for line in response.splitlines():
        line = line.strip()
        if line.upper().startswith("VOTE:"):
            v = line.split(":", 1)[1].strip().upper()
            if "YES" in v:
                vote = "YES"
            else:
                vote = "NO"
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return vote, reason


def _ask_sonnet(prompt: str) -> str:
    from ..models.claude import ask_claude
    return ask_claude(prompt)


def _ask_haiku(prompt: str) -> str:
    from ..models.claude import ask_claude_haiku
    return ask_claude_haiku(prompt)


def _ask_deepseek(prompt: str) -> str:
    from ..models.deepseek import ask_deepseek
    return ask_deepseek(prompt)


def _ask_gemini(prompt: str) -> str:
    from ..models.gemini import ask_gemini
    return ask_gemini(prompt)


def _ask_claude_code(prompt: str) -> str:
    from .claude_code_worker import ask_claude_code
    return ask_claude_code(prompt)


_VOTERS = [
    ("claude-sonnet", _ask_sonnet),
    ("claude-haiku", _ask_haiku),
    ("deepseek", _ask_deepseek),
    ("gemini", _ask_gemini),
    ("claude-code-cli", _ask_claude_code),
]


def vote_on_suggestion(suggestion: dict) -> dict:
    """
    Ask all 5 models whether to apply this suggestion.
    Returns a vote result dict including approval status and each model's reasoning.

    Never raises — individual model failures are caught and excluded from the count.
    """
    prompt = _VOTE_PROMPT_TEMPLATE.format(
        feature_name=suggestion.get("feature_name", "?"),
        priority=suggestion.get("priority", "?"),
        observation=suggestion.get("observation", "?"),
        suggested_improvement=suggestion.get("suggested_improvement", "?"),
        file_to_change=suggestion.get("file_to_change", "?"),
    )

    votes = []
    yes_count = 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn, prompt): name for name, fn in _VOTERS}
        for future in as_completed(futures):
            model_name = futures[future]
            try:
                raw = future.result(timeout=30)
                vote, reason = _parse_vote(raw)
            except Exception as e:
                # Abstain on failure — do not penalise for unavailability
                votes.append({"model": model_name, "vote": "ABSTAIN", "reason": str(e)})
                continue

            if vote == "YES":
                yes_count += 1
            votes.append({"model": model_name, "vote": vote, "reason": reason})

    approved = yes_count >= _THRESHOLD
    print(
        f"[improvement_vote] {suggestion.get('feature_name')}: "
        f"{yes_count}/5 YES → {'APPROVED' if approved else 'REJECTED'}"
    )
    return {
        "approved": approved,
        "yes_count": yes_count,
        "total_votes": len([v for v in votes if v["vote"] != "ABSTAIN"]),
        "threshold": _THRESHOLD,
        "votes": votes,
    }
