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
        self._buffer.append(entry)
        self._total += 1
        if len(self._buffer) >= 10:
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

    def summary(self) -> dict:
        """Return in-memory + on-disk entry count and model distribution."""
        on_disk: list[dict] = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
            except (json.JSONDecodeError, OSError):
                on_disk = []

        all_entries = on_disk + self._buffer
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


# Singleton
insight_log = InsightLog()
