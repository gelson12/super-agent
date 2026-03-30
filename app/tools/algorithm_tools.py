"""
Algorithm tools — LangChain @tool wrappers for self-built algorithms
stored in the 'super-agent-algorithms' GitHub repo.

These tools are available to the GitHub agent, shell agent, and any
future agent that needs data-driven routing or complexity heuristics.
"""
import json
from langchain_core.tools import tool
from ..learning.algorithm_store import algorithm_store
from ..learning.algorithm_builder import build_and_commit_algorithms


@tool
def recommend_model_for_query(category: str, complexity: int) -> str:
    """
    Use the self-built routing heuristic to recommend the best AI model
    for a query based on its category and complexity score.

    Args:
        category: Query category, e.g. 'code/math', 'writing/analysis',
                  'extraction/classification', 'trivial/chat'.
        complexity: Integer 1–5 complexity score.

    Returns:
        Recommended model name string (CLAUDE, DEEPSEEK, GEMINI, HAIKU).
    """
    try:
        result = algorithm_store.run(
            "routing_heuristic",
            fn_name="recommend_model",
            category=category,
            complexity=complexity,
        )
        return str(result)
    except RuntimeError:
        # Fallback when algorithm not yet built
        fallback_map = {"code/math": "DEEPSEEK", "writing/analysis": "CLAUDE",
                        "trivial/chat": "HAIKU"}
        return fallback_map.get(category, "DEEPSEEK" if complexity >= 4 else "HAIKU")


@tool
def predict_query_complexity(message: str) -> str:
    """
    Use the self-calibrated complexity predictor to estimate the complexity
    score (1–5) of a user message.

    Args:
        message: The raw user query.

    Returns:
        JSON string: {"complexity": int, "source": "algorithm" | "fallback"}
    """
    try:
        result = algorithm_store.run(
            "complexity_predictor",
            fn_name="predict_complexity",
            message=message,
        )
        return json.dumps({"complexity": int(result), "source": "algorithm"})
    except RuntimeError:
        # Fallback heuristic
        words = len(message.split())
        if words <= 5:
            c = 1
        elif words <= 15:
            c = 2
        elif words <= 30:
            c = 3
        elif words <= 60:
            c = 4
        else:
            c = 5
        return json.dumps({"complexity": c, "source": "fallback"})


@tool
def get_fallback_model(failed_model: str, excluded_models: str = "") -> str:
    """
    Use the self-built error recovery algorithm to recommend the best
    fallback model when a primary model returns an error.

    Args:
        failed_model: The model name that failed (e.g. 'CLAUDE').
        excluded_models: Comma-separated list of additional models to skip.

    Returns:
        Recommended fallback model name string.
    """
    excluded = [m.strip().upper() for m in excluded_models.split(",") if m.strip()]
    try:
        result = algorithm_store.run(
            "error_recovery",
            fn_name="recommend_fallback",
            failed_model=failed_model,
            excluded=excluded,
        )
        return str(result)
    except RuntimeError:
        # Static fallback chain
        chain = ["CLAUDE", "DEEPSEEK", "GEMINI", "HAIKU"]
        skip = {failed_model.upper()} | set(excluded)
        for m in chain:
            if m not in skip:
                return m
        return "HAIKU"


@tool
def list_available_algorithms() -> str:
    """
    List all self-built algorithms currently loaded in the algorithm store.

    Returns:
        JSON string describing available algorithms and their status.
    """
    try:
        algos = algorithm_store.list_algorithms()
        status = algorithm_store.status()
        return json.dumps({
            "algorithms": algos,
            "store_status": status,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def trigger_algorithm_build() -> str:
    """
    Manually trigger a build of self-generated algorithms from the current
    wisdom store and insight log data. New algorithms are committed to the
    'super-agent-algorithms' GitHub repo.

    Returns:
        JSON string with build summary: built, skipped, failed counts.
    """
    try:
        summary = build_and_commit_algorithms()
        return json.dumps(summary, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
