"""
Pro CLI Usage Tracker — records every Claude Code CLI call.

Tracks prompt/response sizes, estimates token consumption, and
predicts when the daily Pro subscription limit will be hit.

Storage: /workspace/pro_usage_log.json  (fallback ./)
Format:  flat JSON array, capped at 5000 entries
Writes:  best-effort / exception-swallowed — never block the caller
"""
import json
import os
import datetime
import time
from pathlib import Path

_USAGE_LOG_FILE = "pro_usage_log.json"
_MAX_ENTRIES = 5000
# Rough estimate: 4 chars ≈ 1 token (GPT-style)
_CHARS_PER_TOKEN = 4
# Claude Sonnet API equivalent price per 1M tokens ($4.50)
_VALUE_PER_1M_TOKENS = 4.50
# Conservative daily token quota for Claude Pro (real limit: 100k–200k/day)
_ESTIMATED_DAILY_TOKEN_QUOTA = 100_000


def _resolve_path() -> Path:
    base = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
    return base / _USAGE_LOG_FILE


def _load() -> list:
    try:
        return json.loads(_resolve_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: list) -> None:
    try:
        _resolve_path().write_text(
            json.dumps(entries[-_MAX_ENTRIES:], indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def record(prompt_chars: int, response_chars: int, was_cached: bool = False) -> None:
    """
    Append one Pro CLI call record.
    Called from claude_code_worker.ask_claude_code() after every call.
    Best-effort — never raises.
    """
    try:
        est_tokens = (prompt_chars + response_chars) // _CHARS_PER_TOKEN
        entry = {
            "ts": time.time(),
            "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
            "prompt_chars": prompt_chars,
            "response_chars": response_chars,
            "est_tokens": est_tokens,
            "was_cached": was_cached,
        }
        entries = _load()
        entries.append(entry)
        _save(entries)
    except Exception:
        pass


def _today_str() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def get_daily_summary() -> dict:
    """Returns stats for today's Pro CLI usage."""
    try:
        today = _today_str()
        entries = _load()
        today_entries = [e for e in entries if e.get("date") == today]
        cached = [e for e in today_entries if e.get("was_cached")]
        est_tokens = sum(e.get("est_tokens", 0) for e in today_entries)
        est_value_usd = round(est_tokens / 1_000_000 * _VALUE_PER_1M_TOKENS, 4)

        yesterday = (datetime.datetime.utcnow() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_entries = [e for e in entries if e.get("date") == yesterday]
        pct_of_yesterday = None
        if yesterday_entries:
            pct_of_yesterday = round(len(today_entries) / len(yesterday_entries) * 100, 1)

        return {
            "date": today,
            "calls": len(today_entries),
            "cached_calls": len(cached),
            "api_calls": len(today_entries) - len(cached),
            "est_tokens": est_tokens,
            "est_value_usd": est_value_usd,
            "pct_of_yesterday": pct_of_yesterday,
        }
    except Exception:
        return {"date": _today_str(), "calls": 0, "est_tokens": 0, "est_value_usd": 0.0}


def get_weekly_summary() -> dict:
    """Returns stats for the last 7 days of Pro CLI usage."""
    try:
        entries = _load()
        week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).timestamp()
        week_entries = [e for e in entries if e.get("ts", 0) >= week_ago]
        est_tokens = sum(e.get("est_tokens", 0) for e in week_entries)
        est_value_usd = round(est_tokens / 1_000_000 * _VALUE_PER_1M_TOKENS, 4)

        daily: dict[str, int] = {}
        for e in week_entries:
            d = e.get("date", "?")
            daily[d] = daily.get(d, 0) + 1

        return {
            "week_calls": len(week_entries),
            "week_est_tokens": est_tokens,
            "week_est_value_usd": est_value_usd,
            "daily_avg_calls": round(len(week_entries) / 7, 1),
            "daily_breakdown": daily,
        }
    except Exception:
        return {"week_calls": 0, "week_est_tokens": 0, "week_est_value_usd": 0.0}


def predict_daily_limit_eta() -> str:
    """
    Based on call rate in the last 2 hours, predict time until daily token quota hit.
    Returns a human-readable estimate string.
    """
    try:
        entries = _load()
        now = time.time()
        two_hours_ago = now - 7200

        recent = [e for e in entries if e.get("ts", 0) >= two_hours_ago and not e.get("was_cached")]
        if len(recent) < 3:
            return "rate too low to predict (fewer than 3 API calls in last 2h)"

        elapsed_hours = (now - recent[0]["ts"]) / 3600
        if elapsed_hours < 0.05:
            return "rate too low to predict"

        tokens_per_hour = sum(e.get("est_tokens", 0) for e in recent) / elapsed_hours

        today = _today_str()
        tokens_today = sum(
            e.get("est_tokens", 0) for e in entries
            if e.get("date") == today and not e.get("was_cached")
        )
        tokens_remaining = _ESTIMATED_DAILY_TOKEN_QUOTA - tokens_today

        if tokens_remaining <= 0:
            return "daily token quota likely exhausted for today"

        hours_remaining = tokens_remaining / tokens_per_hour
        if hours_remaining > 48:
            return f"daily limit not expected today (est. {hours_remaining:.0f}h at current rate)"

        h = int(hours_remaining)
        m = int((hours_remaining - h) * 60)
        return f"at current rate, daily limit in ~{h}h {m}min"
    except Exception:
        return "prediction unavailable"
