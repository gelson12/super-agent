"""
Pre-processing layer: trivial query detection and complexity scoring.

Runs before the LLM classifier to short-circuit simple requests cheaply.
"""
import re

# ── Trivial patterns (handled by Haiku, no classifier call needed) ─────────────

_GREETINGS = {
    "hi", "hello", "hey", "sup", "yo", "howdy", "hiya",
    "good morning", "good afternoon", "good evening", "good night",
    "morning", "afternoon", "evening",
}

_THANKS = {
    "thanks", "thank you", "thank you so much", "ty", "thx",
    "cheers", "much appreciated", "great thanks",
}

_SIMPLE_AFFIRMATIONS = {
    "ok", "okay", "sure", "yes", "yep", "yup", "no", "nope",
    "got it", "understood", "cool", "nice", "great", "perfect",
    "sounds good", "makes sense", "alright", "right",
}

_MATH_PATTERN = re.compile(r"^[\d\s\+\-\*\/\^\(\)\.\%=]+[=?]?\s*$")


def detect_trivial(message: str) -> bool:
    """
    Return True if the message is trivially simple and should skip
    the LLM-based classifier, routing straight to Haiku.

    Covers: greetings, thanks, affirmations, pure arithmetic.
    """
    clean = message.lower().strip().rstrip("?!.,")
    if clean in _GREETINGS or clean in _THANKS or clean in _SIMPLE_AFFIRMATIONS:
        return True
    if _MATH_PATTERN.match(clean) and len(clean) <= 60:
        return True
    return False


# ── Complexity scoring ────────────────────────────────────────────────────────

_HIGH_COMPLEXITY_SIGNALS = {
    # deep analysis
    "analyse", "analyze", "compare", "evaluate", "assess", "critique",
    "review", "summarise", "summarize", "explain in depth", "elaborate",
    "strategy", "architecture", "design", "plan", "roadmap",
    # writing
    "write an essay", "write a report", "write a proposal", "draft",
    "compose", "long-form",
    # technical
    "debug", "fix this code", "implement", "refactor", "optimize",
    "algorithm", "build a", "create a system", "design a", "calculate",
    "prove", "derive", "model",
    # reasoning
    "pros and cons", "trade-offs", "tradeoffs", "first principles",
    "root cause", "implications", "consequences",
    # debugging / isolation
    "isolate", "troubleshoot", "diagnose", "reproduce", "minimum viable",
    "not working", "broken", "failing",
}

_MEDIUM_SIGNALS = {
    "what is", "what are", "how does", "how do", "explain",
    "why does", "why is", "difference between", "compare",
    "list", "give me", "show me", "help me",
}


def score_complexity(message: str) -> int:
    """
    Return a complexity score 1–5.

    1 — trivial (greeting, thanks, simple yes/no)
    2 — quick lookup / single factual question
    3 — moderate explanation or a few questions
    4 — multi-part analysis, structured output, moderate coding
    5 — deep reasoning, long-form writing, complex code, multi-step plans
    """
    lower = message.lower()
    words = lower.split()
    word_count = len(words)
    question_marks = message.count("?")

    # Start with base score from length
    if word_count <= 5:
        base = 1
    elif word_count <= 15:
        base = 2
    elif word_count <= 40:
        base = 3
    elif word_count <= 100:
        base = 4
    else:
        base = 5

    # Adjust for high-complexity signals
    high_hits = sum(1 for s in _HIGH_COMPLEXITY_SIGNALS if s in lower)
    if high_hits >= 2:
        base = min(5, base + 2)
    elif high_hits == 1:
        base = min(5, base + 1)

    # Multiple questions push complexity up
    if question_marks >= 3:
        base = min(5, base + 1)

    return base


def model_for_complexity(score: int) -> str:
    """
    Suggest a model tier based on complexity score.

    1–2 → HAIKU   (fast, cheap)
    3   → HAIKU   (still lightweight)
    4   → GEMINI or DEEPSEEK (moderate)
    5   → CLAUDE  (full reasoning)
    """
    if score <= 3:
        return "HAIKU"
    if score == 4:
        return "DEEPSEEK"
    return "CLAUDE"
