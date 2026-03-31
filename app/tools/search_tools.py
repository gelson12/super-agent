"""
Web search tool — gives Super Agent access to current information.

Uses DuckDuckGo (free, no API key required) via langchain-community.
Results are synthesised by Claude before returning to the user.
"""
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.tools import tool

_ddg = DuckDuckGoSearchRun()


@tool
def web_search(query: str) -> str:
    """
    Search the web for current information: news, prices, documentation,
    recent events, or anything not in the AI's training data.
    Returns top search results as plain text.
    """
    try:
        return _ddg.run(query)
    except Exception as e:
        return f"[search error: {e}]"
