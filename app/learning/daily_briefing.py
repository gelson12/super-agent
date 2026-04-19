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
    """
    Extract repeated successful patterns from outcome logs and write them to
    patterns.md files. Two-stage: deterministic extraction first (zero API cost),
    then self_improve_agent for deeper synthesis if outcomes have enough data.
    """
    # Stage 1 — deterministic: read outcomes, find OK entries with repeated tags,
    # write structured pattern entries directly to patterns.md via MCP.
    _deterministic_promote(today)

    # Stage 2 — LLM synthesis: ask self_improve_agent to do a deeper pass.
    try:
        promotion_prompt = (
            f"[WEEKLY PATTERN PROMOTION — {today}]\n\n"
            f"Deep-scan last 7 days of agent outcomes and promote repeated success patterns.\n\n"
            f"Steps:\n"
            f"1. obsidian_read_note('KnowledgeBase/n8n/outcomes.md') — look for repeated OK entries\n"
            f"2. obsidian_read_note('KnowledgeBase/Shell/outcomes.md')\n"
            f"3. obsidian_read_note('KnowledgeBase/GitHub/outcomes.md')\n"
            f"4. For each agent: find tasks with outcome=OK that share the same approach 2+ times\n"
            f"5. For each identified pattern, append to '<agent>/patterns.md':\n"
            f"   Format: '### [Promoted {today}] <title> #promoted\\n"
            f"   **Pattern:** <what worked>\\n**Commands:** <key commands if any>\\n'\n"
            f"   Only promote reusable patterns (commands, API calls, config steps, workflows).\n"
            f"   Skip one-off or session-specific tasks.\n\n"
            f"6. obsidian_append_to_note('KnowledgeBase/errors.md', any recurring errors found)\n\n"
            f"If no patterns qualify: append one line to 'KnowledgeBase/SelfImprove/outcomes.md': "
            f"'[{today}] Weekly promotion ran — no new patterns identified.'"
        )
        from ..agents.self_improve_agent import run_self_improve_agent
        run_self_improve_agent(promotion_prompt, authorized=False)
    except Exception:
        pass


def _deterministic_promote(today: str) -> None:
    """
    Zero-LLM pattern promotion: read outcomes.md files via vault MCP, find tags
    that appear in 2+ OK entries within the last 7 days, and write a structured
    pattern note to patterns.md. Runs synchronously inside a daemon thread.
    """
    import asyncio
    import re

    _AGENT_PAIRS = [
        ("KnowledgeBase/Shell/outcomes.md",  "Shell/patterns.md",  "Shell"),
        ("KnowledgeBase/n8n/outcomes.md",    "n8n/patterns.md",    "N8N"),
        ("KnowledgeBase/GitHub/outcomes.md", "GitHub/patterns.md", "GitHub"),
    ]

    async def _promote():
        try:
            from mcp.client.sse import sse_client
            from mcp import ClientSession
            from ..tools.obsidian_tools import VAULT_MCP_URL as _URL

            async with sse_client(url=_URL) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    for outcomes_path, patterns_path, agent_name in _AGENT_PAIRS:
                        res = await s.call_tool("read_file", {"path": outcomes_path})
                        text = res.content[0].text if res.content else ""
                        if not text or "not found" in text.lower() or len(text) < 100:
                            continue

                        # Parse ## YYYY-MM-DD HH:MM — OK sections
                        entries = re.findall(
                            r"## (\d{4}-\d{2}-\d{2}) \d{2}:\d{2} — (OK|ERROR)\n\n"
                            r"\*\*Task:\*\* (.+?)\n\n\*\*Result:\*\* (.+?)\n\n"
                            r"(?:\*\*Tags:\*\* (.+?)\n)?",
                            text,
                            re.DOTALL,
                        )
                        # Filter to OK entries from last 7 days
                        from datetime import date, timedelta
                        cutoff = (date.today() - timedelta(days=7)).isoformat()
                        ok_entries = [
                            e for e in entries if e[1] == "OK" and e[0] >= cutoff
                        ]
                        if len(ok_entries) < 2:
                            continue

                        # Count tags across OK entries
                        tag_counts: dict[str, int] = {}
                        tag_examples: dict[str, list] = {}
                        for entry in ok_entries:
                            tags_raw = entry[4] or ""
                            tags = [t.strip().lstrip("#") for t in tags_raw.split(",") if t.strip()]
                            for tag in tags:
                                tag_counts[tag] = tag_counts.get(tag, 0) + 1
                                tag_examples.setdefault(tag, []).append(entry[2][:80])

                        # Tags appearing 2+ times = promoted pattern
                        promoted_tags = {t: c for t, c in tag_counts.items() if c >= 2}
                        if not promoted_tags:
                            continue

                        for tag, count in promoted_tags.items():
                            examples = tag_examples.get(tag, [])[:2]
                            pattern_note = (
                                f"\n### [Auto-promoted {today}] {agent_name} #{tag} "
                                f"({count} successful uses)\n"
                                f"**Pattern:** This approach succeeded {count}x this week.\n"
                                f"**Example tasks:**\n"
                                + "\n".join(f"  - {ex}" for ex in examples)
                                + "\n#promoted #auto\n"
                            )
                            await s.call_tool("append_to_file", {
                                "path": patterns_path,
                                "content": pattern_note,
                            })
        except Exception:
            pass

    try:
        asyncio.run(_promote())
    except Exception:
        pass


def _generate_briefing(today: str) -> None:
    """Check vault and write today's cross-agent briefing note if missing."""
    try:
        import asyncio
        from mcp.client.sse import sse_client
        from mcp import ClientSession
        from ..tools.obsidian_tools import VAULT_MCP_URL as _URL

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
