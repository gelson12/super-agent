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
        if self._interaction_count % 200 == 0:
            try:
                from .algorithm_builder import build_and_commit_algorithms as _build
                _build()
            except Exception:
                pass
        if self._interaction_count % 500 == 0:
            try:
                from .wisdom_store import wisdom_store
                wisdom_store.sync_to_cloudinary()
            except Exception:
                pass

    def maybe_analyse(self) -> None:
        """Alias — kept for backwards compatibility."""
        self.tick()

    def get_collective_context(self) -> str:
        """Return collective model strength context from wisdom_store."""
        try:
            from .wisdom_store import wisdom_store
            return wisdom_store.get_collective_context()
        except Exception:
            return ""

    def get_learned_context(self) -> str:
        """
        Return combined learned + collective context for system prompt injection.

        Staleness guard: if the last analysis was more than 24 hours ago and we
        have enough interactions, schedule a fresh analysis in the background so
        the next call returns up-to-date context. This prevents the system from
        injecting obsolete patterns that no longer reflect current reality.
        """
        ctx = self._wisdom.get("learned_context", "")
        last_ts = self._wisdom.get("last_analysed_ts", 0)

        # If context is older than 24h and we have enough data, force a refresh
        if ctx and (time.time() - last_ts) > 86400 and self._interaction_count >= 10:
            import threading as _thr
            _thr.Thread(target=self._analyse, daemon=True).start()
            ctx = ""  # Return empty this call; fresh context will be cached for next call

        collective = self.get_collective_context()
        parts = []
        if ctx:
            parts.append(f"[Adaptive context from past interactions]\n{ctx}")
        if collective:
            parts.append(collective)

        # Append live drift warnings so models know which peers are unreliable
        try:
            from .wisdom_store import wisdom_store as _ws
            _drift = _ws.get_drift_summary()
            if _drift:
                _drift_lines = [
                    f"  - {d['model']} performing below threshold on {d['category']} "
                    f"(win_rate={d['win_rate']:.0%}, {d['samples']} samples)"
                    for d in _drift[-3:]
                ]
                parts.append("[Current model drift alerts — route away from these if possible]\n"
                             + "\n".join(_drift_lines))
        except Exception:
            pass

        return "\n\n".join(parts) if parts else ""

    def suggest_model_avoiding_drift(self, category: str, default_model: str) -> str:
        """
        Return the best model for category, skipping any currently in drift.
        Falls back to default_model if no alternative with enough data.
        """
        try:
            from .wisdom_store import wisdom_store as _ws
            if not _ws.is_model_in_drift(default_model, category):
                return default_model
            # Default is drifting — find next best
            best = _ws.get_best_model_for_category(category)
            if best != default_model:
                return best
        except Exception:
            pass
        return default_model

    def analyse_peer_review_impact(self) -> dict:
        """
        Compare error rates between peer-reviewed and non-reviewed high-complexity
        queries (complexity >= 4). Useful for measuring whether peer review helps.
        """
        from .insight_log import LOG_PATH

        entries: list[dict] = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            except (json.JSONDecodeError, OSError):
                return {"error": "Could not read insight log"}

        reviewed = [e for e in entries if "peer_review" in e.get("routed_by", "")]
        non_reviewed = [
            e for e in entries
            if "peer_review" not in e.get("routed_by", "")
            and e.get("complexity", 0) >= 4
        ]

        def _error_rate(lst: list) -> Optional[float]:
            if not lst:
                return None
            return round(sum(1 for e in lst if e.get("error")) / len(lst) * 100, 1)

        reviewed_rate = _error_rate(reviewed)
        non_reviewed_rate = _error_rate(non_reviewed)
        improvement = (
            (non_reviewed_rate or 0.0) - (reviewed_rate or 0.0)
            if reviewed_rate is not None and non_reviewed_rate is not None
            else None
        )

        return {
            "reviewed_count": len(reviewed),
            "reviewed_error_rate_pct": reviewed_rate,
            "non_reviewed_count": len(non_reviewed),
            "non_reviewed_error_rate_pct": non_reviewed_rate,
            "improvement_pct": improvement,
        }

    def get_haiku_ceiling(self) -> int:
        """Return max complexity score that Haiku should handle."""
        return self._wisdom.get("haiku_ceiling", 3)

    def record_preference(self, model: str, rating: int) -> None:
        """
        Record a user rating (1-5) for a model's response.
        Adjusts per-model preference scores used by get_preferred_model().
        Rating >= 4 → positive signal. Rating <= 2 → negative signal.
        """
        prefs = self._wisdom.setdefault("model_preferences", {})
        entry = prefs.setdefault(model.upper(), {"score": 0, "count": 0})
        delta = (rating - 3)  # -2..+2 range centred at 3
        entry["score"] = round(entry["score"] * 0.95 + delta, 3)  # exponential decay
        entry["count"] += 1
        self._save_wisdom()

    def get_preferred_model(self, candidates: list[str]) -> Optional[str]:
        """
        Return the candidate model with the highest preference score.
        Only acts when a model has >= 5 rated interactions and score > 1.0.
        Returns None if no clear preference — caller falls back to normal routing.
        """
        prefs = self._wisdom.get("model_preferences", {})
        best_model = None
        best_score = 1.0  # minimum threshold to override routing
        for model in candidates:
            entry = prefs.get(model.upper(), {})
            if entry.get("count", 0) >= 5 and entry.get("score", 0) > best_score:
                best_score = entry["score"]
                best_model = model
        return best_model

    def get_complexity_calibration(self) -> dict:
        """
        Return auto-calibrated complexity thresholds based on real interaction data.
        Analyses the last 500 interactions and adjusts what complexity level
        each model handles well (error rate < 15%).
        Returns dict: {model: max_complexity_handled}
        """
        from .insight_log import LOG_PATH
        calibration = self._wisdom.get("complexity_calibration", {})
        entries: list[dict] = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            except (json.JSONDecodeError, OSError):
                return calibration

        recent = entries[-500:]
        if len(recent) < 50:
            return calibration

        # For each model+complexity bucket, compute error rate
        buckets: dict[tuple, dict] = {}
        for e in recent:
            key = (e.get("model", "?"), e.get("complexity", 3))
            b = buckets.setdefault(key, {"total": 0, "errors": 0})
            b["total"] += 1
            if e.get("error"):
                b["errors"] += 1

        new_calibration: dict = {}
        for (model, complexity), b in buckets.items():
            if b["total"] < 5:
                continue
            error_rate = b["errors"] / b["total"]
            if error_rate < 0.15:  # model handles this complexity well
                current = new_calibration.get(model, 0)
                new_calibration[model] = max(current, complexity)

        if new_calibration:
            self._wisdom["complexity_calibration"] = new_calibration
            self._save_wisdom()
        return self._wisdom.get("complexity_calibration", {})

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

        # ── Pull latest drift alert from wisdom_store ─────────────────────────
        try:
            from .wisdom_store import wisdom_store
            drift_alerts = wisdom_store._pool.get("drift_alerts", [])
            if drift_alerts:
                latest = drift_alerts[-1]
                drift_note = (
                    f"Drift alert: {latest.get('model')} win rate in "
                    f"{latest.get('category')} dropped to "
                    f"{latest.get('win_rate', 0):.0%} "
                    f"({latest.get('samples', 0)} samples)"
                )
                if drift_note not in notes:
                    notes.append(drift_note)
        except Exception:
            pass

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
