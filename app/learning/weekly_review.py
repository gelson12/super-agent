"""
Weekly Review — runs every Sunday at 23:00 UTC via APScheduler.

Claude Opus 4.6 reviews the full week's activity across all 8 intelligence
features, identifies strategic patterns the nightly reviews may have missed,
and produces a deeper improvement report.

Suggestions follow the same authorization flow as the nightly review:
  - LOW priority + non-core file  → auto-applied immediately
  - MEDIUM / HIGH                 → 5-model vote (3/5 YES required)
  - Core files                    → skipped (human safe word only)

Output: /workspace/weekly_review_YYYY-MM-DD.json  (date = the Sunday)
API:    GET /weekly-review        (latest report)
        GET /weekly-review/list   (all available dates)

SAFETY CONTRACT:
  - Reads only; writes only to /workspace/weekly_review_*.json
  - Never modifies source files, never triggers redeployments
  - safe_to_auto_apply assessment is Claude Opus's honest judgment —
    the system still enforces its own risk classification rules
"""
import json
import os
import datetime
from pathlib import Path

from ..config import settings
from ..activity_log import bg_log as _bg_log


def _log(msg: str) -> None:
    _bg_log(msg, source="weekly_review")

_REVIEW_DIR = Path("/workspace")
_FALLBACK_DIR = Path(".")
_MAX_INTERACTIONS = 200    # week has more data — allow larger window
_MAX_ERRORS_SHOWN = 50

# Shared core-file protection list (same as nightly_review)
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
    return base / f"weekly_review_{date_str}.json"


def _latest_review_path() -> Path | None:
    base = _REVIEW_DIR if os.access(_REVIEW_DIR, os.R_OK) else _FALLBACK_DIR
    candidates = sorted(base.glob("weekly_review_*.json"), reverse=True)
    return candidates[0] if candidates else None


def _collect_weeks_data() -> dict:
    """Pull the last 7 days of entries from insight_log for the weekly prompt."""
    from .insight_log import insight_log

    all_entries = insight_log._load_all()
    week_start = (
        datetime.datetime.utcnow() - datetime.timedelta(days=7)
    ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    week = [e for e in all_entries if e.get("ts", 0) >= week_start]
    errors = [e for e in week if e.get("error")]

    # Per-day breakdown
    daily_counts: dict[str, int] = {}
    for e in week:
        day = datetime.datetime.utcfromtimestamp(e.get("ts", 0)).strftime("%Y-%m-%d")
        daily_counts[day] = daily_counts.get(day, 0) + 1

    # Model and route distribution across the week
    model_dist: dict[str, int] = {}
    route_dist: dict[str, int] = {}
    for e in week:
        m = e.get("model", "?")
        r = e.get("routed_by", "?")
        model_dist[m] = model_dist.get(m, 0) + 1
        route_dist[r] = route_dist.get(r, 0) + 1

    win_rates = insight_log.get_model_win_rates(min_samples=5)

    top_errors = [
        {
            "model": e.get("model"),
            "routed_by": e.get("routed_by"),
            "complexity": e.get("complexity"),
            "resp_len": e.get("resp_len"),
        }
        for e in errors[:_MAX_ERRORS_SHOWN]
    ]

    top_complex = sorted(week, key=lambda x: x.get("complexity", 0), reverse=True)
    top_complex = top_complex[:_MAX_INTERACTIONS]

    return {
        "week_ending": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "week_start": datetime.datetime.utcfromtimestamp(week_start).strftime("%Y-%m-%d"),
        "total_interactions_week": len(week),
        "error_count_week": len(errors),
        "error_rate_pct": round(len(errors) / max(len(week), 1) * 100, 1),
        "daily_interaction_counts": daily_counts,
        "model_distribution_week": model_dist,
        "route_distribution_week": route_dist,
        "alltime_win_rates": win_rates,
        "top_errors_week": top_errors,
        "top_complex_interactions": top_complex,
    }


def _build_prompt(data: dict, cycle_ctx: str = "") -> str:
    summary = json.dumps(data, indent=2)
    cycle_history_block = ""
    if cycle_ctx:
        cycle_history_block = (
            "## IMPROVEMENT CYCLE HISTORY\n"
            "(Past rejected hypotheses and NO_SAFE_IMPROVEMENT cycles.\n"
            "Do not re-propose these without citing new evidence that changes the assessment.)\n"
            f"{cycle_ctx}\n\n"
        )
    return f"""You are Claude Opus 4.6 performing a deep weekly engineering review of Super Agent —
a multi-model AI system running in production on Railway.

You have access to the full week's activity data below. Your job is to find
strategic patterns, recurring failures, and systemic improvements that the
nightly tactical reviews may have missed. Think like a principal engineer
doing a weekly retrospective.

{cycle_history_block}## WEEK ENDING {data['week_ending']} (covering {data['week_start']} → {data['week_ending']})

{summary}

## THE 8 INTELLIGENCE FEATURES (currently deployed)
1. Web Search       — DuckDuckGo via search_tools.py → CLAUDE+SEARCH routing
2. Streaming        — /chat/stream SSE endpoint, live token rendering in frontend
3. Proactive scheduler — APScheduler daily health check + nightly review + weekly review
4. Cross-session memory — pgvector + Google embeddings, injected at dispatch time
5. Confidence routing — CLI-first classifier (Claude CLI Pro → Gemini CLI → Haiku API last resort) routes ambiguous requests with 0.0–1.0 score; keyword match in dispatcher.py bypasses classifier entirely
6. Plan synthesis   — Haiku merges best plan from all competitors (not winner-takes-all)
7. Tool caching     — TTL decorator on github_read_file, n8n list/get, railway logs/variables
8. Feedback loop    — insight_log.get_model_win_rates() skips underperforming models

## YOUR TASK
Think like a principal engineer doing a weekly retrospective.

For EACH item in feature_improvements, reason through the following 9-step cycle
INTERNALLY before writing the JSON. All cycle outputs appear as new fields within
each feature_improvements object.

STEP 1 OBSERVE   — The week's activity summary above is your observation input. Also
                   consult IMPROVEMENT CYCLE HISTORY above if present.
STEP 2 DIAGNOSE  — Identify the single bottleneck category for this suggestion:
                   prompt_instruction_failure | planning_failure |
                   retrieval_context_failure | memory_failure |
                   tool_selection_failure | algorithm_design_failure |
                   evaluation_gap | infrastructure_runtime_issue
STEP 3 HYPOTHESIZE — Propose 1–3 candidate fixes. For each assign float scores:
                   expected_benefit, implementation_complexity, regression_risk,
                   safety_risk, reversibility, confidence  (all 0.0–1.0)
STEP 4 SELECT    — Compute score = expected_benefit * confidence * reversibility
                   / (implementation_complexity + regression_risk)
                   Select the candidate with the highest score.
STEP 5 MODIFY    — For the selected candidate, produce a structured patch:
                   current_behavior, proposed_behavior, rationale,
                   affected_components (list), rollback_method
STEP 6 EVALUATE  — Self-assess the proposed patch against:
                   • benchmark alignment (does it improve the primary metric?)
                   • adversarial edge cases (what could go wrong?)
                   • previous failure patterns (from insight log errors)
                   • cost/latency impact (will this slow or cost more?)
                   • safety (does it touch safety/identity/operator constraints?)
                   • regression risk (does it touch recently stable code?)
STEP 7 DECIDE    — Set cycle_decision to:
                   "ACCEPT"   if correctness improves, no critical regressions,
                              safety score unchanged, change is reversible
                   "REJECT"   if evaluation is negative or a hard constraint fires
                   "NO_SAFE_IMPROVEMENT" if no hypothesis passes evaluation
STEP 8 RECORD    — Set cycle_next_target to the highest remaining bottleneck.
STEP 9 REPEAT    — The next scheduled review is the next iteration.
                   Do NOT loop within this response.

HARD CONSTRAINTS — if any apply, set cycle_constraint_violated: true and
cycle_decision: "REJECT":
  • Never rewrite the entire system when a local fix is possible
  • Never optimize a proxy metric at the expense of the real objective
  • Never alter safety/identity/operator constraints without explicit authorization
  • Never hide failures, regressions, or uncertainties

SUCCESS METRIC PRIORITY: correctness → robustness → safety/compliance →
task completion rate → latency → cost

Produce a structured weekly improvement report in the following JSON format — no other text:

{{
  "week_ending": "{data['week_ending']}",
  "generated_at": "<ISO timestamp>",
  "executive_summary": "<3-4 sentence strategic overview of the week — trends, standout issues, progress>",
  "weekly_patterns": [
    {{"pattern": "<observed pattern>", "days_affected": <number>, "impact": "low|medium|high"}}
  ],
  "regressions": [
    {{"feature": "<name>", "observed": "<what went wrong>", "severity": "low|medium|high", "first_seen": "<date if known>"}}
  ],
  "feature_improvements": [
    {{
      "feature_number": <1-8>,
      "feature_name": "<name>",
      "observation": "<strategic observation — what the week's data reveals>",
      "suggested_improvement": "<specific code-level suggestion>",
      "file_to_change": "<path/to/file.py>",
      "priority": "low|medium|high",
      "safe_to_auto_apply": <true if low priority + non-core utility file, else false>,
      "estimated_impact": "<one sentence on expected improvement>",
      "bottleneck_category": "<one of 8 classes above>",
      "hypotheses": [
        {{
          "name": "...",
          "expected_benefit": 0.0,
          "implementation_complexity": 0.0,
          "regression_risk": 0.0,
          "safety_risk": 0.0,
          "reversibility": 0.0,
          "confidence": 0.0,
          "score": 0.0
        }}
      ],
      "selected_hypothesis_name": "...",
      "hypothesis_score": 0.0,
      "cycle_proposed_patch": {{
        "current_behavior": "...",
        "proposed_behavior": "...",
        "rationale": "...",
        "affected_components": [],
        "rollback_method": "..."
      }},
      "cycle_evaluation_plan": "...",
      "cycle_eval_results": "...",
      "cycle_decision": "ACCEPT|REJECT|NO_SAFE_IMPROVEMENT",
      "cycle_decision_rationale": "...",
      "cycle_constraint_violated": false,
      "cycle_constraint_detail": null,
      "cycle_next_target": "..."
    }}
  ],
  "systemic_issues": [
    {{"issue": "<systemic problem>", "root_cause": "<diagnosis>", "recommended_action": "<what to do>"}}
  ],
  "model_performance_week": {{
    "best_performer": "<model name and why>",
    "worst_performer": "<model name and why>",
    "routing_accuracy_assessment": "<how well did routing work this week?>"
  }},
  "new_algorithm_ideas": [
    {{"name": "<algorithm name>", "purpose": "<problem it solves>", "inputs": "<data it needs>", "priority": "low|medium|high"}}
  ],
  "required_env_vars": [
    {{
      "service": "super-agent | n8n | shared",
      "variable_name": "EXAMPLE_VAR",
      "suggested_value": "<value or description if secret>",
      "reason": "<why this env var is needed for the proposed improvement>",
      "is_secret": true,
      "priority": "low|medium|high"
    }}
  ],
  "next_week_priorities": ["<top 5 things to address next week>"],
  "comparison_to_last_week": "<if you can infer trends, note them; otherwise say: insufficient history>",
  "cycle_summary": {{
    "total_suggestions": 0,
    "accepted": 0,
    "rejected": 0,
    "no_safe_improvement": 0,
    "dominant_bottleneck_category": "...",
    "highest_priority_next_target": "..."
  }},
  "cycle_week_patterns": [
    {{
      "bottleneck_category": "...",
      "recurrence_count": 0,
      "trend": "worsening|stable|improving",
      "strategic_recommendation": "..."
    }}
  ]
}}

Be strategic and specific. Reference actual file names and function names.
Think about what a principal engineer would flag after seeing a full week of production data.
Set safe_to_auto_apply to true only for low-priority suggestions on non-core utility files
(app/tools/, app/cache/, app/memory/, app/learning/).
Always false for dispatcher.py, main.py, agents/, models/, config.py, Dockerfile, requirements.txt."""


def _ask_opus(prompt: str) -> str:
    """Weekly review via Claude Code CLI (Pro subscription — no API cost).
    Falls back to Gemini CLI, then Anthropic API if CLI is unavailable."""
    from .claude_code_worker import ask_claude_code
    result = ask_claude_code(prompt)
    if not result.startswith("["):
        return result

    # CLI error — try Gemini next (free)
    try:
        from .gemini_cli_worker import ask_gemini_cli
        gemini = ask_gemini_cli(prompt)
        if gemini and not gemini.startswith("["):
            return gemini
    except Exception:
        pass

    # CLI and Gemini both unavailable — do not call Anthropic API
    return "[weekly_review: CLI and Gemini both unavailable — skipping review this cycle]"


def _is_core_file(file_path: str) -> bool:
    return any(file_path.startswith(core) for core in _CORE_FILES)


def _is_low_auto_applicable(suggestion: dict) -> bool:
    return (
        suggestion.get("priority") == "low"
        and not _is_core_file(suggestion.get("file_to_change", ""))
    )


def _is_vote_eligible(suggestion: dict) -> bool:
    return (
        suggestion.get("priority") in ("medium", "high")
        and not _is_core_file(suggestion.get("file_to_change", ""))
    )


def _get_baseline_error_rate() -> float:
    try:
        from .insight_log import insight_log
        return float(insight_log.summary().get("error_rate_pct", 0.0))
    except Exception:
        return 0.0


def _apply_suggestions(review: dict) -> list[dict]:
    """Same authorization flow as nightly_review: low → immediate, medium/high → vote."""
    from ..agents.self_improve_agent import run_self_improve_agent
    from .improvement_vote import vote_on_suggestion
    from .improvement_monitor import start_monitoring

    applied = []

    for s in review.get("feature_improvements", []):
        # ── Cycle pre-gate: respect self-evaluation before voting/applying ─────
        from .improvement_cycle import evaluate_cycle_decision
        _cycle_decision, _cycle_rationale = evaluate_cycle_decision(s)
        if _cycle_decision in ("REJECT", "NO_SAFE_IMPROVEMENT"):
            _status = "cycle_rejected" if _cycle_decision == "REJECT" else "no_safe_improvement"
            _log(f"CYCLE {_cycle_decision}: {s.get('feature_name', '?')} — {_cycle_rationale}")
            applied.append({
                "feature_name": s.get("feature_name", "unknown"),
                "file_to_change": s.get("file_to_change", ""),
                "status": _status,
                "cycle_rationale": _cycle_rationale,
            })
            continue
        # ACCEPT or no cycle fields → fall through to existing logic unchanged

        feature_name = s.get("feature_name", "unknown")
        file_to_change = s.get("file_to_change", "")
        priority = s.get("priority", "low")

        vote_result = None
        authorized = False

        if _is_low_auto_applicable(s):
            authorized = True
            _log(f"LOW auto-apply: {feature_name}")

        elif _is_vote_eligible(s):
            _log(f"{priority.upper()} — calling 5-model vote for: {feature_name}")
            try:
                vote_result = vote_on_suggestion(s)
                authorized = vote_result["approved"]
            except Exception as e:
                applied.append({
                    "feature_name": feature_name,
                    "file_to_change": file_to_change,
                    "status": "vote_error",
                    "error": str(e),
                })
                continue

            if not authorized:
                _log(f"VOTE REJECTED ({vote_result['yes_count']}/5): {feature_name}")
                applied.append({
                    "feature_name": feature_name,
                    "file_to_change": file_to_change,
                    "status": "vote_rejected",
                    "vote_result": vote_result,
                })
                continue

            _log(f"VOTE APPROVED ({vote_result['yes_count']}/5): {feature_name}")

        else:
            _log(f"SKIPPED (core file): {feature_name} → {file_to_change}")
            continue

        ts_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H-%M")
        rollback_branch = f"rollback/weekly-{ts_str}"
        auth_prefix = "WEEKLY-VOTE-AUTHORIZED" if vote_result else "WEEKLY-PRE-APPROVED LOW PRIORITY"

        msg = (
            f"{auth_prefix} weekly Opus 4.6 review improvement:\n\n"
            f"STEP 1: Create backup branch '{rollback_branch}' from current master HEAD "
            f"using github_create_branch before making any changes.\n\n"
            f"STEP 2: Apply this minimal targeted change:\n"
            f"Feature: {feature_name}\n"
            f"Observation: {s.get('observation')}\n"
            f"Suggested improvement: {s.get('suggested_improvement')}\n"
            f"Estimated impact: {s.get('estimated_impact', 'not specified')}\n"
            f"File to change: {file_to_change}\n\n"
            f"STEP 3: Confirm the change was committed and pushed to master.\n"
            f"Report the rollback branch name and commit hash when done."
        )

        try:
            baseline = _get_baseline_error_rate()
            result = run_self_improve_agent(msg, authorized=authorized)
            start_monitoring(
                description=f"[weekly] {feature_name} — {s.get('suggested_improvement', '')[:100]}",
                rollback_branch=rollback_branch,
                files_changed=[file_to_change],
                baseline_error_rate=baseline,
            )
            applied.append({
                "feature_name": feature_name,
                "file_to_change": file_to_change,
                "rollback_branch": rollback_branch,
                "agent_result": result[:500],
                "status": "applied",
                "vote_result": vote_result,
            })
            _log(f"Applied + monitoring started: {feature_name}")
        except Exception as e:
            applied.append({
                "feature_name": feature_name,
                "file_to_change": file_to_change,
                "status": "error",
                "error": str(e),
                "vote_result": vote_result,
            })
            _log(f"Apply error for {feature_name}: {e}")

    return applied


def run_weekly_review() -> dict:
    """
    Entry point called by APScheduler every Sunday at 23:00 UTC.
    Returns the review dict (also written to disk).
    Never raises — all errors are caught and logged.
    """
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    out_path = _review_path(date_str)

    _log(f"Starting Opus 4.6 weekly review for week ending {date_str}")

    try:
        data = _collect_weeks_data()
        from .improvement_cycle import load_cycle_context
        prompt = _build_prompt(data, load_cycle_context())
        raw = _ask_opus(prompt)

        review: dict = {}
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[-1] if cleaned.count("```") >= 2 else cleaned
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            review = json.loads(cleaned)
            from .improvement_cycle import parse_and_record_review_cycles
            parse_and_record_review_cycles(review, "weekly")
        except json.JSONDecodeError:
            review = {
                "week_ending": date_str,
                "generated_at": datetime.datetime.utcnow().isoformat(),
                "raw_output": raw,
                "parse_error": "Opus 4.6 did not return valid JSON",
            }

        review["_meta"] = {
            "generated_at_utc": datetime.datetime.utcnow().isoformat(),
            "interactions_reviewed": data["total_interactions_week"],
            "source": "claude-code-cli-pro",
            "review_type": "weekly",
        }

        out_path.write_text(json.dumps(review, indent=2))
        _log(f"Review written to {out_path}")

        # Apply code suggestions via same vote + monitor pipeline
        applied = _apply_suggestions(review)
        if applied:
            review["_auto_applied"] = applied

        # Process env var proposals (voted, then auto-deploy if approved)
        from .nightly_review import apply_env_var_proposals
        env_applied = apply_env_var_proposals(review)
        if env_applied:
            review["_env_vars_applied"] = env_applied

        if applied or env_applied:
            out_path.write_text(json.dumps(review, indent=2))
            _log(f"Applied {len(applied)} suggestion(s), {len(env_applied)} env var(s)")

        return review

    except Exception as e:
        error_doc = {
            "week_ending": date_str,
            "error": str(e),
            "generated_at_utc": datetime.datetime.utcnow().isoformat(),
        }
        try:
            out_path.write_text(json.dumps(error_doc, indent=2))
        except Exception:
            pass
        _log(f"ERROR: {e}")
        return error_doc


def get_latest_review() -> dict | None:
    path = _latest_review_path()
    if not path:
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_review_dates() -> list[str]:
    base = _REVIEW_DIR if os.access(_REVIEW_DIR, os.R_OK) else _FALLBACK_DIR
    return [
        p.stem.replace("weekly_review_", "")
        for p in sorted(base.glob("weekly_review_*.json"), reverse=True)
    ]
