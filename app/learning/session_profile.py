"""
Adaptive Session Routing — learns per-session model preferences to skip
the Haiku classifier for sessions with a stable, predictable routing pattern.

After MIN_CALLS_TO_PROFILE calls from a session, if one model handles
CONFIDENCE_THRESHOLD of them, subsequent requests skip the classifier and
route directly to that model.

Only applies to GENERAL model selection (CLAUDE/HAIKU/GEMINI/DEEPSEEK).
N8N / SHELL / GITHUB / SELF_IMPROVE keyword routes always take priority.

Storage: /workspace/session_profiles.json  (fallback ./)
Format:  dict keyed by session_id (max 500 sessions)
Writes:  best-effort / exception-swallowed
"""
import json
import os
import datetime
from pathlib import Path

_PROFILES_FILE = "session_profiles.json"
_MAX_PROFILES = 500
_MIN_CALLS_TO_PROFILE = 5        # minimum calls before a routing hint activates
_CONFIDENCE_THRESHOLD = 0.80     # 80% same model → skip classifier
_GENERAL_MODELS = {"CLAUDE", "HAIKU", "GEMINI", "DEEPSEEK"}


def _resolve_path() -> Path:
    base = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
    return base / _PROFILES_FILE


def _load() -> dict:
    try:
        return json.loads(_resolve_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(profiles: dict) -> None:
    try:
        # Cap to MAX_PROFILES by evicting least-recently-seen sessions
        if len(profiles) > _MAX_PROFILES:
            sorted_by_seen = sorted(
                profiles.items(),
                key=lambda kv: kv[1].get("last_seen", ""),
                reverse=True,
            )
            profiles = dict(sorted_by_seen[:_MAX_PROFILES])
        _resolve_path().write_text(
            json.dumps(profiles, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


class SessionProfile:
    """Per-session routing profiler. Module-level singleton is `session_profile`."""

    def update(self, session_id: str, model: str, route: str, complexity: int) -> None:
        """
        Record one dispatch outcome for this session.
        Only tracks GENERAL models (CLAUDE/HAIKU/GEMINI/DEEPSEEK) — not agents.
        Best-effort — never raises.
        """
        if model.upper() not in _GENERAL_MODELS:
            return
        try:
            profiles = _load()
            p = profiles.get(session_id, {
                "session_id": session_id,
                "call_count": 0,
                "model_counts": {},
                "dominant_model": None,
                "dominant_model_pct": 0.0,
                "avg_complexity": 0.0,
                "last_seen": "",
                "routing_hint_active": False,
            })

            # Update counts
            p["call_count"] = p.get("call_count", 0) + 1
            model_counts = p.get("model_counts", {})
            model_counts[model] = model_counts.get(model, 0) + 1
            p["model_counts"] = model_counts

            # Recompute dominant model
            total = sum(model_counts.values())
            dominant = max(model_counts, key=model_counts.__getitem__)
            dominant_pct = round(model_counts[dominant] / total, 3)
            p["dominant_model"] = dominant
            p["dominant_model_pct"] = dominant_pct

            # Update rolling average complexity
            prev_avg = p.get("avg_complexity", float(complexity))
            p["avg_complexity"] = round(
                (prev_avg * (p["call_count"] - 1) + complexity) / p["call_count"], 2
            )

            p["last_seen"] = datetime.datetime.utcnow().isoformat()
            p["routing_hint_active"] = (
                p["call_count"] >= _MIN_CALLS_TO_PROFILE
                and dominant_pct >= _CONFIDENCE_THRESHOLD
            )

            profiles[session_id] = p
            _save(profiles)
        except Exception:
            pass

    def get_routing_hint(self, session_id: str) -> str | None:
        """
        Returns the dominant model name if the session has a confident routing pattern,
        else None (caller should use the normal classifier path).
        """
        try:
            profiles = _load()
            p = profiles.get(session_id)
            if not p:
                return None
            if (
                p.get("routing_hint_active")
                and p.get("call_count", 0) >= _MIN_CALLS_TO_PROFILE
                and p.get("dominant_model_pct", 0.0) >= _CONFIDENCE_THRESHOLD
                and p.get("dominant_model") in _GENERAL_MODELS
            ):
                return p["dominant_model"]
            return None
        except Exception:
            return None

    def get_all(self) -> list[dict]:
        """Return all session profiles as a list, sorted by last_seen descending."""
        try:
            profiles = _load()
            return sorted(
                profiles.values(),
                key=lambda p: p.get("last_seen", ""),
                reverse=True,
            )
        except Exception:
            return []


# Module-level singleton
session_profile = SessionProfile()
