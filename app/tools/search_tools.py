"""
Web search tool — gives Super Agent access to current information.

Uses DuckDuckGo (free, no API key required) via langchain-community.
Results are synthesised by Claude before returning to the user.

NOTE: DDG client is lazy-loaded so a missing package never crashes the app
at startup — the tool simply returns an error message at call time instead.
"""
import time

from langchain_core.tools import tool

_ddg = None

_RETRY_DELAYS_S = (0.5, 1.5)


def _get_ddg():
    global _ddg
    if _ddg is None:
        try:
            from langchain_community.tools import DuckDuckGoSearchRun
            _ddg = DuckDuckGoSearchRun()
        except Exception as e:
            return None, str(e)
    return _ddg, None


def _log_search_fallback(query: str, error: Exception) -> None:
    # DDG scrapes HTML; surface exhausted retries so layout breakages aren't silent.
    try:
        from ..learning.insight_log import insight_log
        insight_log.record(
            message=query,
            model="duckduckgo",
            response=f"[search fallback: retries exhausted — {error}]",
            routed_by="web_search",
            complexity=1,
        )
    except Exception:
        pass


@tool
def web_search(query: str) -> str:
    """
    Search the web for current information: news, prices, documentation,
    recent events, or anything not in the AI's training data.
    Returns top search results as plain text.
    """
    client, err = _get_ddg()
    if err:
        return f"[search unavailable: {err}]"
    last_error: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS_S) + 1):
        try:
            return client.run(query)
        except Exception as e:
            last_error = e
            if attempt < len(_RETRY_DELAYS_S):
                time.sleep(_RETRY_DELAYS_S[attempt])
    _log_search_fallback(query, last_error)
    return f"[search error: {last_error}]"
