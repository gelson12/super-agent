"""
Web search tool — gives Super Agent access to current information.

Uses DuckDuckGo (free, no API key required) via langchain-community.
Results are synthesised by Claude before returning to the user.

NOTE: DDG client is lazy-loaded so a missing package never crashes the app
at startup — the tool simply returns an error message at call time instead.
"""
from langchain_core.tools import tool

_ddg = None


def _get_ddg():
    global _ddg
    if _ddg is None:
        try:
            from langchain_community.tools import DuckDuckGoSearchRun
            _ddg = DuckDuckGoSearchRun()
        except Exception as e:
            return None, str(e)
    return _ddg, None


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
    try:
        return client.run(query)
    except Exception as e:
        return f"[search error: {e}]"
