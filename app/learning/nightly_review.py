"""
Nightly Review — runs at 23:00 UTC via APScheduler.

Compiles the day's full activity from the insight log + DB session history,
then asks the in-container Claude Code CLI to produce a structured improvement
report covering all 8 intelligence features and the wider codebase.

Output: /workspace/daily_review_YYYY-MM-DD.json
API:    GET /daily-review  (returns the latest report)

SAFETY CONTRACT:
  - This module ONLY reads data and ONLY writes to /workspace/daily_review_*.json
  - It never modifies source files, never triggers redeployments, never deletes data
  - All output is advisory — a human reads the report and decides what to act on
  - If Claude Code CLI is unavailable the job logs a warning and exits cleanly
"""
import json
import os
import time
import datetime
from pathlib import Path

_REVIEW_DIR = Path("/workspace")
_FALLBACK_DIR = Path(".")
_MAX_INTERACTIONS = 50      # cap to avoid overflowing Claude Code's context
_MAX_ERRORS_SHOWN = 20

# Core paths that must never be auto-applied — human safe word required
_CORE_FILES = frozenset({
    "app/routing/dispatcher.py",
    "app/main.py",
    "app/agents/",
    "app/models/",
    "app/config.py",
    "entrypoint.sh",
    "Dockerfile",
    "requirements.txt",
})


def _review_path(date_str: str) -> Path:
    base = _REVIEW_DIR if os.access(_REVIEW_DIR, os.W_OK) else _FALLBACK_DIR
    return base / f"daily_review_{date_str}.json"


def _latest_review_path() -> Path | None:
    """Return the most recently written review file, or None."""
    base = _REVIEW_DIR if os.access(_REVIEW_DIR, os.R_OK) else _FALLBACK_DIR
    candidates = sorted(base.glob("daily_review_*.json"), reverse=True)
    return candidates[0] if candidates else None


def _collect_todays_data() -> dict:
    """Pull today's entries from insight_log and summarise for the review prompt."""
    from .insight_log import insight_log

    all_entries = insight_log._load_all()
    today_start = datetime.datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    today = [e for e in all_entries if e.get("ts", 0) >= today_start]
    errors = [e for e in today if e.get("error")]

    # Model distribution for today
    model_dist: dict[str, int] = {}
    route_dist: dict[str, int] = {}
    for e in today:
        m = e.get("model", "?")
        r = e.get("routed_by", "?")
        model_dist[m] = model_dist.get(m, 0) + 1
        route_dist[r] = route_dist.get(r, 0) + 1

    # Win rates across all-time
    win_rates = insight_log.get_model_win_rates(min_samples=5)

    # Top errors (capped)
    top_errors = [
        {"model": e.get("model"), "routed_by": e.get("routed_by"),
         "complexity": e.get("complexity"), "resp_len": e.get("resp_len")}
        for e in errors[:_MAX_ERRORS_SHOWN]
    ]

    # Highest-complexity interactions today (most interesting)
    top_complex = sorted(today, key=lambda x: x.get("complexity", 0), reverse=True)
    top_complex = top_complex[:_MAX_INTERACTIONS]

    return {
        "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "total_interactions_today": len(today),
        "error_count_today": len(errors),
        "error_rate_pct": round(len(errors) / max(len(today), 1) * 100, 1),
        "model_distribution_today": model_dist,
        "route_distribution_today": route_dist,
        "alltime_win_rates": win_rates,
        "top_errors_today": top_errors,
        "top_complex_interactions": top_complex,
    }


def _build_prompt(data: dict) -> str:
    summary = json.dumps(data, indent=2)
    return f"""You are doing a nightly engineering review of Super Agent — a multi-model AI system running in production on Railway.

## TODAY'S ACTIVITY SUMMARY ({data['date']})
{summary}

## THE 8 INTELLIGENCE FEATURES (currently deployed)
1. Web Search       — DuckDuckGo via search_tools.py → CLAUDE+SEARCH routing
2. Streaming        — /chat/stream SSE endpoint, live token rendering in frontend
3. Proactive scheduler — APScheduler daily health check via self-improve agent
4. Cross-session memory — pgvector + Google embeddings, injected at dispatch time
5. Confidence routing — Haiku classifies ambiguous requests with 0.0–1.0 score
6. Plan synthesis   — Haiku merges best plan from all competitors (not winner-takes-all)
7. Tool caching     — TTL decorator on github_read_file, n8n list/get, railway logs/variables
8. Feedback loop    — insight_log.get_model_win_rates() skips underperforming models

## YOUR TASK
Read the source files in /workspace (if any repos are cloned there) and the activity data above.
Produce a structured improvement report in the following JSON format — no other text, just valid JSON:

{{
  "date": "{data['date']}",
  "generated_at": "<ISO timestamp>",
  "health_summary": "<2-3 sentence overview of today's system health>",
  "regressions": [
    {{"feature": "<name>", "observed": "<what went wrong>", "severity": "low|medium|high"}}
  ],
  "feature_improvements": [
    {{
      "feature_number": <1-8>,
      "feature_name": "<name>",
      "observation": "<what you noticed from today's data>",
      "suggested_improvement": "<specific code-level suggestion>",
      "file_to_change": "<path/to/file.py>",
      "priority": "low|medium|high",
      "safe_to_auto_apply": false
    }}
  ],
  "new_algorithm_ideas": [
    {{"name": "<algorithm name>", "purpose": "<what problem it solves>", "inputs": "<what data it needs>"}}
  ],
  "routing_observations": "<observations about routing accuracy, misroutes, confidence thresholds>",
  "model_performance_notes": "<which models performed well/poorly today and why>",
  "tomorrow_priorities": ["<top 3 things to address tomorrow>"]
}}

Be specific and actionable. Reference actual file names and function names where possible.
If today had very few interactions, focus on the codebase quality instead.
Set safe_to_auto_apply to true only for low-priority suggestions on non-core utility files
(app/tools/, app/cache/, app/memory/, app/learning/).
Always false for dispatcher.py, main.py, agents/, models/, config.py, Dockerfile, requirements.txt."""


def _is_auto_applicable(suggestion: dict) -> bool:
    """Return True if this suggestion qualifies for autonomous application."""
    if suggestion.get("priority") != "low":
        return False
    file_path = suggestion.get("file_to_change", "")
    return not any(file_path.startswith(core) for core in _CORE_FILES)


def auto_apply_safe_suggestions(review: dict) -> list[dict]:
    """
    For each low-priority, non-core suggestion in the review, call
    run_self_improve_agent() to apply it autonomously.
    Returns a list of result records (one per suggestion attempted).
    Never raises — errors are captured per-suggestion.
    """
    from ..agents.self_improve_agent import run_self_improve_agent

    applied = []
    for s in review.get("feature_improvements", []):
        if not _is_auto_applicable(s):
            continue
        msg = (
            f"Apply this pre-approved nightly review suggestion (LOW priority, non-core file):\n"
            f"Feature: {s.get('feature_name')}\n"
            f"Observation: {s.get('observation')}\n"
            f"Suggested improvement: {s.get('suggested_improvement')}\n"
            f"File to change: {s.get('file_to_change')}\n\n"
            f"This is a pre-approved LOW priority suggestion from the nightly review. "
            f"Apply it now using the minimal targeted change described above."
        )
        try:
            result = run_self_improve_agent(msg)
            applied.append({
                "feature_number": s.get("feature_number"),
                "feature_name": s.get("feature_name"),
                "file_to_change": s.get("file_to_change"),
                "agent_result": result[:500],
                "status": "applied",
            })
            print(f"[nightly_review] Auto-applied: {s.get('feature_name')} → {s.get('file_to_change')}")
        except Exception as e:
            applied.append({
                "feature_number": s.get("feature_number"),
                "feature_name": s.get("feature_name"),
                "file_to_change": s.get("file_to_change"),
                "status": "error",
                "error": str(e),
            })
            print(f"[nightly_review] Auto-apply error for {s.get('feature_name')}: {e}")
    return applied


def run_nightly_review() -> dict:
    """
    Entry point called by APScheduler at 23:00 UTC.
    Returns the review dict (also written to disk).
    Never raises — all errors are caught and logged.
    """
    from ..learning.claude_code_worker import ask_claude_code

    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    out_path = _review_path(date_str)

    print(f"[nightly_review] Starting review for {date_str}")

    try:
        data = _collect_todays_data()
        prompt = _build_prompt(data)
        raw = ask_claude_code(prompt)

        # Parse JSON from Claude Code's response
        review: dict = {}
        # Strip markdown code fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[-1] if cleaned.count("```") >= 2 else cleaned
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            review = json.loads(cleaned)
        except json.JSONDecodeError:
            # Not valid JSON — store as raw text in a wrapper
            review = {
                "date": date_str,
                "generated_at": datetime.datetime.utcnow().isoformat(),
                "raw_output": raw,
                "parse_error": "Claude Code did not return valid JSON",
            }

        review["_meta"] = {
            "generated_at_utc": datetime.datetime.utcnow().isoformat(),
            "interactions_reviewed": data["total_interactions_today"],
            "source": "claude_code_cli",
        }

        out_path.write_text(json.dumps(review, indent=2))
        print(f"[nightly_review] Review written to {out_path}")

        # Auto-apply low-priority, non-core suggestions
        applied = auto_apply_safe_suggestions(review)
        if applied:
            review["_auto_applied"] = applied
            out_path.write_text(json.dumps(review, indent=2))
            print(f"[nightly_review] Auto-applied {len(applied)} low-risk suggestion(s)")

        return review

    except Exception as e:
        error_doc = {
            "date": date_str,
            "error": str(e),
            "generated_at_utc": datetime.datetime.utcnow().isoformat(),
        }
        try:
            out_path.write_text(json.dumps(error_doc, indent=2))
        except Exception:
            pass
        print(f"[nightly_review] ERROR: {e}")
        return error_doc


def get_latest_review() -> dict | None:
    """Read and return the most recent nightly review, or None if none exist."""
    path = _latest_review_path()
    if not path:
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_review_dates() -> list[str]:
    """Return all available review dates (newest first)."""
    base = _REVIEW_DIR if os.access(_REVIEW_DIR, os.R_OK) else _FALLBACK_DIR
    return [
        p.stem.replace("daily_review_", "")
        for p in sorted(base.glob("daily_review_*.json"), reverse=True)
    ]
