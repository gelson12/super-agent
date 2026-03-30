"""
Self-improvement adapter — periodically analyses insight logs and
updates a learned_context string that is injected into system prompts.

Analysis runs every 100 interactions (in-process counter).
Wisdom is persisted to /workspace/super_agent_wisdom.json so it
survives container restarts.

The adapter also dynamically adjusts the Haiku ceiling:
if Haiku's error rate exceeds 20% at complexity >= 3, it lowers
the ceiling so those queries are escalated to a smarter model.
"""
import json
import os
import time
from typing import Optional


def _resolve_wisdom_path() -> str:
    for candidate in ("/workspace/super_agent_wisdom.json", "./super_agent_wisdom.json"):
        directory = os.path.dirname(candidate) or "."
        if os.access(directory, os.W_OK):
            return candidate
    return "./super_agent_wisdom.json"


WISDOM_PATH = _resolve_wisdom_path()

_DEFAULT_WISDOM = {
    "learned_context": "",
    "haiku_ceiling": 3,       # max complexity Haiku should handle
    "last_analysed_ts": 0,
    "analysis_count": 0,
    "notes": [],
}


class Adapter:
    def __init__(self) -> None:
        self._interaction_count = 0
        self._wisdom: dict = self._load_wisdom()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_wisdom(self) -> dict:
        if os.path.exists(WISDOM_PATH):
            try:
                with open(WISDOM_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {**_DEFAULT_WISDOM, **data}
            except (json.JSONDecodeError, OSError):
                pass
        return dict(_DEFAULT_WISDOM)

    def _save_wisdom(self) -> None:
        try:
            with open(WISDOM_PATH, "w", encoding="utf-8") as f:
                json.dump(self._wisdom, f, indent=2)
        except OSError:
            pass

    # ── Public API ────────────────────────────────────────────────────────────

    def tick(self) -> None:
        """Call after every dispatch. Triggers analysis every 100 interactions."""
        self._interaction_count += 1
        if self._interaction_count % 100 == 0:
            self._analyse()

    def maybe_analyse(self) -> None:
        """Alias — kept for backwards compatibility."""
        self.tick()

    def get_learned_context(self) -> str:
        """Return learned context string for injection into system prompts."""
        ctx = self._wisdom.get("learned_context", "")
        if not ctx:
            return ""
        return f"\n\n[Adaptive context from past interactions]\n{ctx}"

    def get_haiku_ceiling(self) -> int:
        """Return max complexity score that Haiku should handle."""
        return self._wisdom.get("haiku_ceiling", 3)

    def wisdom_dict(self) -> dict:
        """Return full wisdom state for the /wisdom endpoint."""
        return dict(self._wisdom)

    # ── Analysis ──────────────────────────────────────────────────────────────

    def _analyse(self) -> None:
        """Read insight logs, derive patterns, update wisdom."""
        from .insight_log import LOG_PATH  # local import to avoid circular

        entries: list[dict] = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            except (json.JSONDecodeError, OSError):
                return

        if len(entries) < 20:
            return  # not enough data yet

        # ── Error rates by model ───────────────────────────────────────────────
        model_totals: dict[str, int] = {}
        model_errors: dict[str, int] = {}
        haiku_high_complexity_errors = 0
        haiku_high_complexity_total = 0

        for e in entries[-500:]:  # last 500 only
            m = e.get("model", "?")
            model_totals[m] = model_totals.get(m, 0) + 1
            if e.get("error"):
                model_errors[m] = model_errors.get(m, 0) + 1
            if m == "HAIKU" and e.get("complexity", 0) >= 3:
                haiku_high_complexity_total += 1
                if e.get("error"):
                    haiku_high_complexity_errors += 1

        notes: list[str] = []

        # ── Haiku ceiling adjustment ───────────────────────────────────────────
        haiku_ceiling = self._wisdom.get("haiku_ceiling", 3)
        if haiku_high_complexity_total >= 10:
            error_rate = haiku_high_complexity_errors / haiku_high_complexity_total
            if error_rate > 0.20 and haiku_ceiling > 2:
                haiku_ceiling = max(2, haiku_ceiling - 1)
                notes.append(
                    f"Haiku ceiling lowered to {haiku_ceiling} "
                    f"(error rate at complexity>=3 was {error_rate:.0%})"
                )
            elif error_rate < 0.05 and haiku_ceiling < 3:
                haiku_ceiling = min(3, haiku_ceiling + 1)
                notes.append(
                    f"Haiku ceiling raised to {haiku_ceiling} "
                    f"(low error rate at high complexity)"
                )

        # ── Build learned context ─────────────────────────────────────────────
        context_parts: list[str] = []

        top_model = max(model_totals, key=lambda k: model_totals[k], default=None)
        if top_model:
            context_parts.append(
                f"Most queries go to {top_model} "
                f"({model_totals[top_model]}/{sum(model_totals.values())} recent requests)."
            )

        worst_model = None
        worst_rate = 0.0
        for m, total in model_totals.items():
            if total < 5:
                continue
            rate = model_errors.get(m, 0) / total
            if rate > worst_rate:
                worst_rate = rate
                worst_model = m
        if worst_model and worst_rate > 0.10:
            context_parts.append(
                f"Note: {worst_model} has a {worst_rate:.0%} error rate — "
                "consider routing complex queries to Claude if issues persist."
            )

        learned_context = " ".join(context_parts)

        # ── Persist ───────────────────────────────────────────────────────────
        self._wisdom["learned_context"] = learned_context
        self._wisdom["haiku_ceiling"] = haiku_ceiling
        self._wisdom["last_analysed_ts"] = round(time.time(), 2)
        self._wisdom["analysis_count"] = self._wisdom.get("analysis_count", 0) + 1
        if notes:
            existing_notes = self._wisdom.get("notes", [])
            self._wisdom["notes"] = (existing_notes + notes)[-20:]  # keep last 20

        self._save_wisdom()


# Singleton
adapter = Adapter()
