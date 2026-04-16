"""
Interaction logger — records every dispatch event to a JSON file.

Fields per entry:
  ts          — Unix timestamp
  msg_words   — word count of the user message
  model       — model that handled the request
  routed_by   — how routing was decided (classifier, trivial, etc.)
  complexity  — 1–5 complexity score
  resp_len    — character length of the response
  error       — True if response looks like an error
  session     — session_id (optional)

Saved to /workspace/super_agent_insights.json every 10 entries.
Falls back to ./super_agent_insights.json if /workspace is not writable.
"""
import json
import os
import time
from typing import Optional


def _resolve_path() -> str:
    for candidate in ("/workspace/super_agent_insights.json", "./super_agent_insights.json"):
        directory = os.path.dirname(candidate) or "."
        if os.access(directory, os.W_OK):
            return candidate
    return "./super_agent_insights.json"


LOG_PATH = _resolve_path()


class InsightLog:
    def __init__(self) -> None:
        self._buffer: list[dict] = []
        self._total = 0

    def record(
        self,
        message: str,
        model: str,
        response: str,
        routed_by: str,
        complexity: int,
        session: Optional[str] = None,
        latency_ms: Optional[float] = None,
        confidence: Optional[float] = None,
        memory_hits: int = 0,
        cache_hit: bool = False,
    ) -> None:
        entry = {
            "ts": round(time.time(), 2),
            "msg_words": len(message.split()),
            "model": model,
            "routed_by": routed_by,
            "complexity": complexity,
            "resp_len": len(response),
            "error": response.startswith("[") and response.endswith("]"),
            "session": session or "default",
        }
        # Optional enrichment fields — only written when provided
        if latency_ms is not None:
            entry["latency_ms"] = round(latency_ms, 1)
        if confidence is not None:
            entry["confidence"] = round(confidence, 3)
        if memory_hits:
            entry["memory_hits"] = memory_hits
        if cache_hit:
            entry["cache_hit"] = True
        self._buffer.append(entry)
        self._total += 1
        if len(self._buffer) >= 3:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        existing: list[dict] = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.extend(self._buffer)
        try:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except OSError:
            pass  # non-fatal — metrics are best-effort
        self._buffer.clear()

    def _load_all(self) -> list[dict]:
        """Return all entries: on-disk + in-memory buffer combined."""
        on_disk: list[dict] = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
            except (json.JSONDecodeError, OSError):
                on_disk = []
        return on_disk + self._buffer

    def get_model_win_rates(self, min_samples: int = 20) -> dict[str, float]:
        """
        Return {model: win_rate} for models with >= min_samples interactions.
        win_rate = fraction of non-error responses (0.0–1.0).
        Used by agent_planner to skip consistently underperforming models.
        """
        entries = self._load_all()
        counts: dict[str, dict] = {}
        for e in entries:
            model = e.get("model", "UNKNOWN")
            if model not in counts:
                counts[model] = {"total": 0, "errors": 0}
            counts[model]["total"] += 1
            if e.get("error"):
                counts[model]["errors"] += 1
        return {
            model: round(1.0 - (v["errors"] / v["total"]), 3)
            for model, v in counts.items()
            if v["total"] >= min_samples
        }

    def summary(self) -> dict:
        """Return in-memory + on-disk entry count and model distribution."""
        all_entries = self._load_all()
        total = len(all_entries)
        if not total:
            return {"total_interactions": 0}

        model_counts: dict[str, int] = {}
        error_count = 0
        for e in all_entries:
            model_counts[e.get("model", "?")] = model_counts.get(e.get("model", "?"), 0) + 1
            if e.get("error"):
                error_count += 1

        return {
            "total_interactions": total,
            "model_distribution": model_counts,
            "error_count": error_count,
            "error_rate_pct": round(error_count / total * 100, 1),
        }

    def normalized_summary(self) -> dict:
        """Summary with normalized model names for reporting.

        Composite names like CLAUDE+SEARCH, SELF_IMPROVE, GEMINI_CLI are
        mapped back to their base model so reports aggregate correctly.
        """
        all_entries = self._load_all()
        total = len(all_entries)
        if not total:
            return {"total_interactions": 0}

        raw_counts: dict[str, int] = {}
        normalized_counts: dict[str, int] = {}
        route_counts: dict[str, int] = {}
        error_count = 0
        for e in all_entries:
            raw_model = e.get("model", "?")
            raw_counts[raw_model] = raw_counts.get(raw_model, 0) + 1
            norm = _normalize_model(raw_model)
            normalized_counts[norm] = normalized_counts.get(norm, 0) + 1
            route = e.get("routed_by", "?")
            route_counts[route] = route_counts.get(route, 0) + 1
            if e.get("error"):
                error_count += 1

        return {
            "total_interactions": total,
            "model_distribution": normalized_counts,
            "raw_model_distribution": raw_counts,
            "route_distribution": route_counts,
            "error_count": error_count,
            "error_rate_pct": round(error_count / total * 100, 1),
        }


def _normalize_model(raw: str) -> str:
    """Normalize composite model names to base model for aggregation."""
    m = (raw or "UNKNOWN").upper()
    _MAP = {
        "CLAUDE+SEARCH": "CLAUDE",
        "SELF_IMPROVE": "CLAUDE",
        "SHELL": "CLAUDE",
        "GITHUB": "CLAUDE",
        "N8N": "CLAUDE",
        "GEMINI_CLI": "GEMINI",
    }
    return _MAP.get(m, m)


# Singleton
insight_log = InsightLog()
