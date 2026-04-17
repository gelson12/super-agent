"""
Daily cross-agent briefing generator.

On the first agent call of each calendar day, checks whether today's briefing note
already exists in the vault. If not, triggers the self_improve_agent in a background
thread to write one — summarising recent outcomes from all agent KnowledgeBase paths.

Runs in daemon thread — never blocks the dispatch path. Safe to call on every request.
"""
import datetime
import threading
import time

_last_checked: float = 0.0
_last_checked_date: str = ""
_lock = threading.Lock()
_CHECK_INTERVAL = 3600  # check at most once per hour

_last_promotion_date: str = ""


def trigger_daily_briefing_if_needed() -> None:
    """Non-blocking: fire a daemon thread to write today's briefing if missing."""
    global _last_checked, _last_checked_date
    now = time.time()
    today = datetime.date.today().isoformat()
    with _lock:
        if now - _last_checked < _CHECK_INTERVAL:
            return
        if _last_checked_date == today:
            return
        _last_checked = now
        _last_checked_date = today
    threading.Thread(target=_generate_briefing, args=(today,), daemon=True).start()


def promote_patterns_if_needed() -> None:
    """
    Weekly (Monday only): scan outcome logs for repeated successful patterns
    and promote them to the relevant patterns.md file via self_improve_agent.
    Safe to call on every request — exits immediately on non-Monday days.
    """
    global _last_promotion_date
    today = datetime.date.today()
    if today.weekday() != 0:  # 0 = Monday
        return
    today_str = today.isoformat()
    with _lock:
        if _last_promotion_date == today_str:
            return
        _last_promotion_date = today_str
    threading.Thread(target=_run_promotion, args=(today_str,), daemon=True).start()


def _run_promotion(today: str) -> None:
    """Ask self_improve_agent to extract repeated successful patterns from outcome logs."""
    try:
        promotion_prompt = (
            f"[WEEKLY PATTERN PROMOTION — {today}]\n\n"
            f"Scan last 7 days of agent outcomes and promote repeated success patterns.\n\n"
            f"Steps:\n"
            f"1. obsidian_read_note('KnowledgeBase/n8n/outcomes.md')\n"
            f"2. obsidian_read_note('KnowledgeBase/Shell/outcomes.md')\n"
            f"3. obsidian_read_note('KnowledgeBase/GitHub/outcomes.md')\n"
            f"4. For each agent: find tasks with outcome=OK that share the same approach 3+ times\n"
            f"5. For each identified pattern:\n"
            f"   - obsidian_append_to_note('<agent>/patterns.md',\n"
            f"     '\\n## [Promoted {today}] <pattern title>\\n<description>\\n#promoted')\n"
            f"   Only promote reusable technical patterns (commands, API calls, config steps).\n"
            f"   Skip one-off or overly specific tasks.\n\n"
            f"If no patterns qualify for promotion, just log: 'No patterns promoted this week.'"
        )
        from ..agents.self_improve_agent import run_self_improve_agent
        run_self_improve_agent(promotion_prompt, authorized=False)
    except Exception:
        pass


def _generate_briefing(today: str) -> None:
    """Check vault and write today's cross-agent briefing note if missing."""
    try:
        import asyncio
        from mcp.client.sse import sse_client
        from mcp import ClientSession
        _URL = "http://obsidian-vault.railway.internal:22360/sse"

        async def _check_exists():
            async with sse_client(url=_URL) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    result = await s.call_tool("list_directory", {"path": "Daily"})
                    content = result.content[0].text if result.content else ""
                    return f"{today}-briefing" in content

        if asyncio.run(_check_exists()):
            return

        brief_prompt = (
            f"[AUTOMATED DAILY BRIEFING TASK — {today}]\n\n"
            f"Write today's cross-agent knowledge briefing. Steps:\n\n"
            f"1. obsidian_get_recent_notes() — list notes modified in last 24h\n"
            f"2. obsidian_read_note('KnowledgeBase/n8n/outcomes.md') — n8n outcomes\n"
            f"3. obsidian_read_note('KnowledgeBase/Shell/outcomes.md') — shell outcomes\n"
            f"4. obsidian_read_note('KnowledgeBase/GitHub/outcomes.md') — github outcomes\n"
            f"5. obsidian_write_note('Daily/{today}-briefing.md', content)\n\n"
            f"Briefing format:\n"
            f"---\ntype: briefing\ndate: {today}\ntags: [daily, briefing, cross-agent]\n---\n\n"
            f"# Daily Briefing — {today}\n\n"
            f"## Agent Activity\n(what each agent accomplished)\n\n"
            f"## Errors & Failures\n(anything that went wrong)\n\n"
            f"## Key Patterns\n(insights all agents should know)\n\n"
            f"## Today's Priorities\n(suggested focus areas)\n\n"
            f"Keep it under 400 words. This note is read by all agents as context."
        )
        from ..agents.self_improve_agent import run_self_improve_agent
        run_self_improve_agent(brief_prompt, authorized=False)
    except Exception:
        pass
