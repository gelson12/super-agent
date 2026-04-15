"""
Improvement Cycle — structured 9-step meta-improvement loop.

Augments the nightly and weekly reviewers with:
  OBSERVE → DIAGNOSE → HYPOTHESIZE → SELECT → MODIFY →
  EVALUATE → DECIDE → RECORD → REPEAT

This module provides:
  - CycleLog     : persistent store of per-suggestion cycle decisions
  - load_cycle_context()             : inject rejected history into OBSERVE step
  - parse_and_record_review_cycles() : persist A-G data after each review
  - evaluate_cycle_decision()        : pre-gate before voting/auto-apply

Storage: /workspace/improvement_cycle_log.json  (fallback ./)
Format:  flat JSON array, capped at 500 entries (same pattern as claude_code_insights.json)
Writes:  best-effort / exception-swallowed — never block the review pipeline
Imports: stdlib only — zero LLM or framework dependencies
"""
import json
import os
import datetime
from pathlib import Path


_CYCLE_LOG_FILE = "improvement_cycle_log.json"
_MAX_ENTRIES = 500

# The 8 recognised bottleneck categories
BOTTLENECK_CLASSES = frozenset({
    "prompt_instruction_failure",
    "planning_failure",
    "retrieval_context_failure",
    "memory_failure",
    "tool_selection_failure",
    "algorithm_design_failure",
    "evaluation_gap",
    "infrastructure_runtime_issue",
})


def _resolve_path() -> Path:
    base = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
    return base / _CYCLE_LOG_FILE


class CycleLog:
    """Thin wrapper around the cycle log JSON file."""

    def __init__(self) -> None:
        self._path = _resolve_path()

    def _load(self) -> list:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, entries: list) -> None:
        try:
            self._path.write_text(
                json.dumps(entries[-_MAX_ENTRIES:], indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass  # best-effort

    def append(self, entry: dict) -> None:
        entries = self._load()
        entries.append(entry)
        self._save(entries)

    def get_recent(self, n: int = 20) -> list:
        return self._load()[-n:]

    def get_rejected(self, n: int = 10) -> list:
        all_entries = self._load()
        rejected = [e for e in all_entries if e.get("decision") == "REJECT"]
        return rejected[-n:]

    def get_no_safe(self, n: int = 5) -> list:
        all_entries = self._load()
        no_safe = [e for e in all_entries if e.get("decision") == "NO_SAFE_IMPROVEMENT"]
        return no_safe[-n:]

    def summary(self) -> dict:
        entries = self._load()
        counts: dict = {"ACCEPT": 0, "REJECT": 0, "NO_SAFE_IMPROVEMENT": 0, "other": 0}
        for e in entries:
            d = e.get("decision", "other")
            if d in counts:
                counts[d] += 1
            else:
                counts["other"] += 1
        most_recent = entries[-1].get("recorded_at") if entries else None
        return {
            "total_cycles": len(entries),
            "accepted": counts["ACCEPT"],
            "rejected": counts["REJECT"],
            "no_safe_improvement": counts["NO_SAFE_IMPROVEMENT"],
            "other": counts["other"],
            "most_recent_cycle": most_recent,
        }


# Module-level singleton — same pattern as insight_log
cycle_log = CycleLog()


# ── Public API ──────────────────────────────────────────────────────────────


def load_cycle_context(n_rejected: int = 5, n_no_safe: int = 3) -> str:
    """
    Return a compact human-readable string of recent rejected/no-safe
    hypotheses suitable for prompt injection into the OBSERVE step.
    Returns "" on first run or any error (no history yet).
    Keeps output under ~400 tokens regardless of entry count.
    """
    try:
        rejected = cycle_log.get_rejected(n_rejected)
        no_safe = cycle_log.get_no_safe(n_no_safe)
    except Exception:
        return ""

    if not rejected and not no_safe:
        return ""

    lines: list[str] = []

    if rejected:
        lines.append("Past rejected hypotheses (do not re-propose without new evidence):")
        for i, e in enumerate(rejected, 1):
            date = e.get("date", "?")
            feature = e.get("feature_name", "?")
            category = e.get("bottleneck_category", "?")
            rationale = (e.get("decision_rationale") or "no rationale recorded")[:120]
            next_target = e.get("next_iteration_target", "")
            lines.append(f"  {i}. [{date}] Feature: {feature} | Category: {category}")
            lines.append(f"     Rejected: \"{rationale}\"")
            if next_target:
                lines.append(f"     Next target noted: {next_target[:80]}")

    if no_safe:
        lines.append("Past NO_SAFE_IMPROVEMENT cycles:")
        for i, e in enumerate(no_safe, 1):
            date = e.get("date", "?")
            feature = e.get("feature_name", "?")
            category = e.get("bottleneck_category", "?")
            rationale = (e.get("decision_rationale") or "no rationale recorded")[:120]
            lines.append(f"  {i}. [{date}] Feature: {feature} | Category: {category}")
            lines.append(f"     No safe improvement identified — {rationale}")

    return "\n".join(lines)


def _append_vault_cycle_log(entries: list, date_str: str, review_type: str) -> None:
    """
    Append cycle decisions to Engineering/Improvement Cycle Log.md in the vault.
    Runs in a fire-and-forget thread — never blocks the review pipeline.
    """
    if not entries:
        return
    try:
        lines = [f"\n## {review_type.title()} Cycle — {date_str}\n"]
        for e in entries:
            icon = {"ACCEPT": "✅", "REJECT": "❌", "NO_SAFE_IMPROVEMENT": "⏭️"}.get(e["decision"], "•")
            lines.append(
                f"{icon} **{e['feature_name']}** — `{e['decision']}`  \n"
                f"  Category: {e['bottleneck_category']}  \n"
                f"  Rationale: {e['decision_rationale'][:200]}\n"
            )
        content = "".join(lines)
        content_repr = repr(content)

        script = (
            "import asyncio\n"
            f"CONTENT = {content_repr}\n"
            "PATH = 'Engineering/Improvement Cycle Log.md'\n"
            "async def main():\n"
            "    from mcp.client.sse import sse_client\n"
            "    from mcp import ClientSession\n"
            "    async with sse_client(url='http://obsidian-vault.railway.internal:22360/sse') as (r, w):\n"
            "        async with ClientSession(r, w) as s:\n"
            "            await s.initialize()\n"
            "            await s.call_tool('append_to_file', {'path': PATH, 'content': CONTENT})\n"
            "            print('Cycle log appended to vault', flush=True)\n"
            "asyncio.run(main())\n"
        )
        import threading

        def _run():
            try:
                from ..tools.shell_tools import run_shell_via_cli_worker
                cmd = "python3 << 'PYEOF'\n" + script + "PYEOF"
                run_shell_via_cli_worker(cmd)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass  # never raise from here


def parse_and_record_review_cycles(review: dict, review_type: str) -> None:
    """
    Called immediately after JSON parse in run_nightly_review / run_weekly_review.
    Iterates feature_improvements; for any entry that has cycle_decision set,
    builds and appends a CycleLog record. Also appends to vault cycle log.

    Wrapped in a broad try/except — failure here must never crash the review.
    """
    try:
        date_str = review.get("date") or review.get("week_ending") or \
                   datetime.datetime.utcnow().strftime("%Y-%m-%d")
        ts_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H-%M")
        prefix = review_type.upper()

        vault_entries = []
        for s in review.get("feature_improvements", []):
            if "cycle_decision" not in s:
                continue  # old-format suggestion — skip silently

            feature_name = s.get("feature_name", "unknown")
            entry = {
                "id": f"cycle-{ts_str}-{prefix}-{feature_name[:30].replace(' ', '_')}",
                "review_type": review_type,
                "date": date_str,
                "recorded_at": datetime.datetime.utcnow().isoformat(),
                "feature_name": feature_name,
                "bottleneck_category": s.get("bottleneck_category", "unknown"),
                "current_bottleneck": s.get("observation", ""),
                "root_cause_analysis": s.get("cycle_decision_rationale", ""),
                "proposed_patch": s.get("cycle_proposed_patch", {}),
                "evaluation_plan": s.get("cycle_evaluation_plan", ""),
                "eval_results": s.get("cycle_eval_results", ""),
                "decision": s.get("cycle_decision", "ACCEPT"),
                "decision_rationale": s.get("cycle_decision_rationale", ""),
                "next_iteration_target": s.get("cycle_next_target", ""),
                "hypotheses": s.get("hypotheses", []),
                "selected_hypothesis_name": s.get("selected_hypothesis_name", ""),
                "hypothesis_score": s.get("hypothesis_score", None),
                "constraint_violated": s.get("cycle_constraint_violated", False),
                "constraint_detail": s.get("cycle_constraint_detail", None),
            }
            cycle_log.append(entry)
            vault_entries.append(entry)

        # Fire-and-forget vault append — never blocks the review
        _append_vault_cycle_log(vault_entries, date_str, review_type)

    except Exception:
        pass  # never raise from here


def evaluate_cycle_decision(suggestion: dict) -> tuple[str, str]:
    """
    Pre-gate for auto_apply_safe_suggestions / _apply_suggestions.
    Returns (decision, rationale) where decision is one of:
      "ACCEPT"                — proceed to existing vote/auto-apply logic
      "REJECT"                — skip; log to cycle log
      "NO_SAFE_IMPROVEMENT"   — skip; log to cycle log

    If cycle fields are absent (old-format suggestion), returns ("ACCEPT", ...)
    so the existing pipeline runs completely unchanged — full backward compat.
    """
    if "cycle_decision" not in suggestion:
        return ("ACCEPT", "no cycle data — backward compatible")

    rationale = suggestion.get("cycle_decision_rationale", "")

    # Hard constraint violation always forces REJECT regardless of cycle_decision
    if suggestion.get("cycle_constraint_violated"):
        detail = suggestion.get("cycle_constraint_detail") or "constraint violated"
        return ("REJECT", f"hard constraint: {detail}")

    decision = suggestion.get("cycle_decision", "ACCEPT")

    if decision == "NO_SAFE_IMPROVEMENT":
        return ("NO_SAFE_IMPROVEMENT", rationale or "no safe improvement identified")

    if decision == "REJECT":
        return ("REJECT", rationale or "cycle self-evaluation rejected this hypothesis")

    if decision == "ACCEPT":
        return ("ACCEPT", rationale or "cycle self-evaluation accepted")

    # Unrecognised value — default to ACCEPT for safety
    return ("ACCEPT", f"unrecognised cycle_decision '{decision}' — defaulting to ACCEPT")


def get_cycle_log() -> list:
    """Return last 100 cycle entries for the /cycle-log endpoint."""
    return cycle_log.get_recent(100)


def get_cycle_summary() -> dict:
    """Return aggregated cycle statistics for the /cycle-log/summary endpoint."""
    return cycle_log.summary()


# ── Feedback loop closure ────────────────────────────────────────────────────


_OUTCOME_LOG_FILE = "improvement_outcome_log.json"
_MAX_OUTCOME_ENTRIES = 200


def _resolve_outcome_path() -> Path:
    base = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
    return base / _OUTCOME_LOG_FILE


def record_outcome_snapshot(
    feature_name: str,
    snapshot_type: str,
    error_rate_7d: float,
    dispatch_total_7d: int,
    notes: str = "",
) -> None:
    """
    Record a before/after error-rate snapshot for a cycle entry.

    Call with snapshot_type="before" immediately when a proposal is ACCEPT-ed,
    and with snapshot_type="after" 7 days later (the nightly review job checks
    for pending after-snapshots and calls this automatically).

    Never raises — all writes are best-effort.

    Args:
        feature_name:     Matches CycleLog entry feature_name.
        snapshot_type:    "before" | "after"
        error_rate_7d:    Trailing 7-day error rate (0.0–1.0) at snapshot time.
        dispatch_total_7d: Total requests in the same 7-day window.
        notes:            Optional free-text context.
    """
    try:
        path = _resolve_outcome_path()
        try:
            entries: list = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            entries = []

        entry = {
            "feature_name": feature_name,
            "snapshot_type": snapshot_type,
            "recorded_at": datetime.datetime.utcnow().isoformat(),
            "error_rate_7d": round(error_rate_7d, 6),
            "dispatch_total_7d": dispatch_total_7d,
            "notes": notes,
        }
        entries.append(entry)
        path.write_text(
            json.dumps(entries[-_MAX_OUTCOME_ENTRIES:], indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def get_outcome_delta(feature_name: str) -> dict | None:
    """
    Return the before/after error-rate delta for a feature, or None if
    either snapshot is missing.

    Returns::

        {
            "feature_name": str,
            "before_rate":  float,
            "after_rate":   float,
            "delta":        float,   # negative = improvement
            "improvement":  bool,    # True if after < before
            "before_recorded_at": str,
            "after_recorded_at":  str,
        }
    """
    try:
        path = _resolve_outcome_path()
        entries: list = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    feature_entries = [e for e in entries if e.get("feature_name") == feature_name]
    before = next((e for e in reversed(feature_entries) if e.get("snapshot_type") == "before"), None)
    after  = next((e for e in reversed(feature_entries) if e.get("snapshot_type") == "after"),  None)

    if not before or not after:
        return None

    b = before["error_rate_7d"]
    a = after["error_rate_7d"]
    return {
        "feature_name":       feature_name,
        "before_rate":        b,
        "after_rate":         a,
        "delta":              round(a - b, 6),
        "improvement":        a < b,
        "before_recorded_at": before["recorded_at"],
        "after_recorded_at":  after["recorded_at"],
    }


def check_pending_after_snapshots() -> list[str]:
    """
    Scan the outcome log for accepted proposals that have a 'before' snapshot
    but no 'after' snapshot older than 7 days.  Returns the feature names
    that are ready for an after-snapshot.

    Called by the nightly review to prompt the LLM to measure post-deploy
    error rates and call record_outcome_snapshot(..., "after", ...).
    """
    try:
        path = _resolve_outcome_path()
        entries: list = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    now = datetime.datetime.utcnow()
    seven_days = datetime.timedelta(days=7)

    by_feature: dict = {}
    for e in entries:
        fname = e.get("feature_name", "")
        stype = e.get("snapshot_type", "")
        by_feature.setdefault(fname, set()).add(stype)

    pending = []
    for e in entries:
        if e.get("snapshot_type") != "before":
            continue
        fname = e["feature_name"]
        if "after" in by_feature.get(fname, set()):
            continue  # already have an after-snapshot
        try:
            recorded = datetime.datetime.fromisoformat(e["recorded_at"])
        except Exception:
            continue
        if now - recorded >= seven_days:
            pending.append(fname)

    # Deduplicate while preserving order
    seen: set = set()
    result = []
    for f in pending:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result
