"""
Shared collective wisdom pool — tracks per-model win rates by category
so the routing layer can learn which model is best at what over time.

Persists to /workspace/super_agent_wisdom_pool.json locally AND to
Cloudinary (as raw JSON) every 500 interactions so the knowledge
survives container rebuilds.

On startup: reads local file → tries Cloudinary URL stored inside it
→ uses fresher Cloudinary copy if available → falls back to local →
falls back to zero-state defaults.

Thread safety: record_outcome() and sync_to_cloudinary() are guarded
by a threading.Lock so concurrent FastAPI requests don't corrupt state.
"""
import json
import os
import tempfile
import time
import threading
from typing import Optional

# ── Storage path resolution ───────────────────────────────────────────────────

def _resolve_pool_path() -> str:
    for candidate in ("/workspace/super_agent_wisdom_pool.json", "./super_agent_wisdom_pool.json"):
        directory = os.path.dirname(candidate) or "."
        if os.access(directory, os.W_OK):
            return candidate
    return "./super_agent_wisdom_pool.json"


POOL_PATH = _resolve_pool_path()

_DEFAULT_POOL: dict = {
    "win_rates": {
        "CLAUDE":   {"writing/analysis":          {"wins": 0, "total": 0}},
        "DEEPSEEK": {"code/math":                 {"wins": 0, "total": 0}},
        "GEMINI":   {"extraction/classification": {"wins": 0, "total": 0}},
        "HAIKU":    {"trivial/chat":              {"wins": 0, "total": 0}},
    },
    "recent_outcomes": [],      # last 500 {model, category, win} — sliding window
    "cloudinary_backup_url": None,
    "last_synced_ts": 0,
    "interaction_count": 0,
    "drift_alerts": [],         # last 20 drift events
}

# Primary category per model (for fallback and drift detection)
_PRIMARY_CATEGORY: dict[str, str] = {
    "CLAUDE":   "writing/analysis",
    "DEEPSEEK": "code/math",
    "GEMINI":   "extraction/classification",
    "HAIKU":    "trivial/chat",
}

_FALLBACK_MODEL: dict[str, str] = {v: k for k, v in _PRIMARY_CATEGORY.items()}


class WisdomStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: dict = self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        """
        Load pool from best available source:
        1. Read local file to get stored Cloudinary URL
        2. If URL exists, try downloading from Cloudinary (fresher)
        3. Fall back to local file data
        4. Fall back to zero-state defaults
        """
        local_data: Optional[dict] = None
        if os.path.exists(POOL_PATH):
            try:
                with open(POOL_PATH, "r", encoding="utf-8") as f:
                    local_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                local_data = None

        # Try Cloudinary if URL is stored
        url = (local_data or {}).get("cloudinary_backup_url")
        if url:
            cloud_data = self._download_from_cloudinary(url)
            if cloud_data:
                return {**_DEFAULT_POOL, **cloud_data}

        if local_data:
            return {**_DEFAULT_POOL, **local_data}

        return dict(_DEFAULT_POOL)

    def _save_local(self) -> None:
        try:
            with open(POOL_PATH, "w", encoding="utf-8") as f:
                json.dump(self._pool, f, indent=2)
        except OSError:
            pass

    def _download_from_cloudinary(self, url: str) -> Optional[dict]:
        try:
            import requests
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def sync_to_cloudinary(self) -> Optional[str]:
        """
        Serialize pool to a temp .json file, upload to Cloudinary as raw resource,
        store the returned URL in the pool. Non-fatal — never raises.
        """
        tmp_path: Optional[str] = None
        try:
            from ..storage.cloudinary_manager import upload_file as _upload

            with self._lock:
                snapshot = dict(self._pool)

            # Write to temp file
            fd, tmp_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2)

            result = _upload(tmp_path, resource_type="raw", public_id="super_agent_wisdom_pool")
            url = result.get("url") or result.get("secure_url")

            with self._lock:
                self._pool["cloudinary_backup_url"] = url
                self._pool["last_synced_ts"] = round(time.time(), 2)
                self._save_local()

            return url
        except Exception:
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ── Recording outcomes ────────────────────────────────────────────────────

    def _detect_category(self, routed_by: str, model: str) -> str:
        """Map a model to its primary category for win-rate tracking."""
        return _PRIMARY_CATEGORY.get(model.upper(), "general")

    def record_outcome(self, model: str, category: str, error: bool) -> None:
        """
        Called after every dispatch. Updates cumulative win rates and the
        500-entry sliding window used for drift detection.
        Triggers Cloudinary sync every 500 interactions.
        """
        model = model.upper()
        win = not error

        with self._lock:
            # Cumulative win rates
            win_rates = self._pool.setdefault("win_rates", {})
            model_rates = win_rates.setdefault(model, {})
            cat_entry = model_rates.setdefault(category, {"wins": 0, "total": 0})
            if win:
                cat_entry["wins"] += 1
            cat_entry["total"] += 1

            # Sliding window (last 500)
            recent = self._pool.setdefault("recent_outcomes", [])
            recent.append({"model": model, "category": category, "win": win})
            if len(recent) > 500:
                self._pool["recent_outcomes"] = recent[-500:]

            self._pool["interaction_count"] = self._pool.get("interaction_count", 0) + 1
            count = self._pool["interaction_count"]

        # Drift check every 100
        if count % 100 == 0:
            self._check_drift_alerts()

        # Cloudinary sync every 500
        if count % 500 == 0:
            self.sync_to_cloudinary()

    # ── Drift detection ───────────────────────────────────────────────────────

    def _check_drift_alerts(self) -> None:
        """
        Slide over last 100 outcomes per model/primary-category.
        If win rate < 60% with >= 20 samples → log a drift alert.
        """
        with self._lock:
            recent = list(self._pool.get("recent_outcomes", []))

        for model, category in _PRIMARY_CATEGORY.items():
            window = [
                r for r in recent[-100:]
                if r.get("model") == model and r.get("category") == category
            ]
            if len(window) < 20:
                continue
            win_rate = sum(1 for r in window if r.get("win")) / len(window)
            if win_rate < 0.60:
                alert = {
                    "ts": round(time.time(), 2),
                    "model": model,
                    "category": category,
                    "win_rate": round(win_rate, 3),
                    "samples": len(window),
                }
                with self._lock:
                    alerts = self._pool.setdefault("drift_alerts", [])
                    alerts.append(alert)
                    self._pool["drift_alerts"] = alerts[-20:]

    # ── Query interface ───────────────────────────────────────────────────────

    def is_model_in_drift(self, model: str, category: str) -> bool:
        """
        Return True if this model has a recent drift alert for this category.
        Used by the router to skip a drifting model even when it's the 'primary' choice.
        A drift alert is considered recent if it was recorded within the last 2 hours.
        """
        cutoff = time.time() - 7200  # 2-hour window
        with self._lock:
            alerts = self._pool.get("drift_alerts", [])
        return any(
            a.get("model") == model.upper()
            and a.get("category") == category
            and a.get("ts", 0) > cutoff
            for a in alerts
        )

    def get_drift_summary(self) -> list[dict]:
        """Return recent drift alerts — used by /collective-wisdom endpoint."""
        with self._lock:
            return list(self._pool.get("drift_alerts", []))[-10:]

    def get_best_model_for_category(self, category: str) -> str:
        """
        Return the model with the highest win rate for a given category.
        Requires total >= 5 before trusting the rate; otherwise returns
        the default model for that category.
        """
        best_model = _FALLBACK_MODEL.get(category, "HAIKU")
        best_rate = -1.0

        with self._lock:
            win_rates = self._pool.get("win_rates", {})

        for model, cats in win_rates.items():
            entry = cats.get(category, {})
            total = entry.get("total", 0)
            if total < 5:
                continue
            rate = entry.get("wins", 0) / total
            if rate > best_rate:
                best_rate = rate
                best_model = model

        return best_model

    def get_collective_context(self) -> str:
        """
        Build a human-readable strength summary for injection into system prompts.
        Only includes categories with total >= 10 interactions.
        Returns empty string if insufficient data.
        """
        from ..prompts import COLLECTIVE_CONTEXT_PROMPT

        with self._lock:
            win_rates = self._pool.get("win_rates", {})

        parts: list[str] = []
        for model, cats in win_rates.items():
            for category, entry in cats.items():
                total = entry.get("total", 0)
                if total < 10:
                    continue
                rate = entry.get("wins", 0) / total
                parts.append(
                    f"{model} excels at {category} "
                    f"({rate:.0%} win rate, {total} interactions)."
                )

        if not parts:
            return ""

        return COLLECTIVE_CONTEXT_PROMPT.format(strengths_summary=" ".join(parts))

    def wisdom_dict(self) -> dict:
        """Return a copy of the full pool state for the /collective-wisdom endpoint."""
        with self._lock:
            return dict(self._pool)


# Singleton
wisdom_store = WisdomStore()
