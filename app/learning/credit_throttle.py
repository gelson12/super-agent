"""
Credit-aware job throttling — scales back autonomous operations as daily budget depletes.

Throttle tiers (based on % of DAILY_BUDGET_USD remaining):
  > 50%  — FULL: all jobs run at normal frequency
  25-50% — REDUCED: health check 30min→2hr, diff review skipped on non-critical deploys
  < 25%  — MINIMAL: health check→4hr, nightly review skipped, voting paused, only critical jobs
  < 10%  — CRITICAL: only n8n monitor + basic health ping (no LLM)

Jobs and their throttle behaviour:
  health_check      — FULL:30min, REDUCED:2hr, MINIMAL:4hr, CRITICAL:skip_llm
  diff_review       — FULL:always, REDUCED:skip, MINIMAL:skip, CRITICAL:skip
  nightly_review    — FULL:daily, REDUCED:daily, MINIMAL:skip, CRITICAL:skip
  weekly_review     — FULL:weekly, REDUCED:weekly, MINIMAL:weekly, CRITICAL:skip
  voting            — FULL:normal, REDUCED:normal, MINIMAL:skip, CRITICAL:skip
  improvement       — FULL:normal, REDUCED:normal, MINIMAL:skip, CRITICAL:skip
  benchmark         — FULL:weekly, REDUCED:skip, MINIMAL:skip, CRITICAL:skip
  n8n_monitor       — always runs (cheap — no LLM unless broken)
  post_deploy       — always runs (important for link regeneration)

The throttle is re-evaluated at the start of each job. No scheduler restart needed —
jobs call should_run() and return early if throttled.
"""
import os
from .cost_ledger import get_credit_pct_remaining


def _tier() -> str:
    """Return current throttle tier based on remaining daily budget."""
    pct = get_credit_pct_remaining()
    if pct > 50:
        return "full"
    if pct > 25:
        return "reduced"
    if pct > 10:
        return "minimal"
    return "critical"


# Maps job_name → set of tiers where it is ALLOWED to run
_ALLOWED: dict[str, set] = {
    "health_check":   {"full", "reduced", "minimal", "critical"},  # always, but LLM skipped at critical
    "diff_review":    {"full"},
    "nightly_review": {"full", "reduced"},
    "weekly_review":  {"full", "reduced", "minimal"},
    "voting":         {"full", "reduced"},
    "improvement":    {"full", "reduced"},
    "benchmark":      {"full"},
    "n8n_monitor":    {"full", "reduced", "minimal", "critical"},   # cheap — always
    "post_deploy":    {"full", "reduced", "minimal", "critical"},   # always
}

# Effective health_check interval (minutes) per tier
HEALTH_CHECK_INTERVALS: dict[str, int] = {
    "full":     30,
    "reduced":  120,
    "minimal":  240,
    "critical": 240,
}


def should_run(job_name: str) -> bool:
    """
    Returns True if this job is allowed to run at the current credit level.
    Call at the top of every background job before doing any LLM work.
    """
    tier = _tier()
    allowed = job_name in _ALLOWED and tier in _ALLOWED.get(job_name, set())
    if not allowed:
        try:
            from ..activity_log import bg_log
            pct = get_credit_pct_remaining()
            bg_log(
                f"THROTTLED: {job_name} skipped — credit remaining {pct:.0f}% (tier={tier})",
                source="credit_throttle",
            )
        except Exception:
            pass
    return allowed


def health_check_uses_llm() -> bool:
    """
    At CRITICAL tier, health check runs but skips the LLM self_improve_agent call —
    it only takes a metrics snapshot (free). Returns True if LLM should be used.
    """
    return _tier() != "critical"


def get_status() -> dict:
    """Return current throttle status for the /credits/breakdown endpoint."""
    pct = get_credit_pct_remaining()
    tier = _tier()
    budget = float(os.environ.get("DAILY_BUDGET_USD", "0"))
    return {
        "tier": tier,
        "credit_pct_remaining": pct,
        "budget_usd": budget,
        "budget_configured": budget > 0,
        "health_check_interval_min": HEALTH_CHECK_INTERVALS[tier],
        "jobs_status": {
            job: ("allowed" if tier in allowed else "throttled")
            for job, allowed in _ALLOWED.items()
        },
        "message": {
            "full":     "All autonomous jobs running at full frequency.",
            "reduced":  "Credit < 50%: diff review paused, health check at 2hr intervals.",
            "minimal":  "Credit < 25%: voting/improvement/benchmark paused, health check at 4hr.",
            "critical": "Credit < 10%: only n8n monitor + post-deploy checks running. LLM paused.",
        }[tier],
    }
