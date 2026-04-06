"""
Self-Improving Prompt Library — versions all system prompts and tracks
per-version error rates so the nightly review can propose improvements.

Each tracked prompt starts at "v1" (seeded from the static constants in
prompts.py). New versions are proposed by the nightly review, staged as
inactive, and activated only after a 3/5 vote.

If the library file is missing or corrupted, all callers fall back to the
static constants in prompts.py — zero disruption to the dispatch pipeline.

Storage: /workspace/prompt_library.json  (fallback ./)
Format:  JSON dict {prompt_name: {active_id, versions: [...]}}
Writes:  best-effort / exception-swallowed
"""
import json
import os
import datetime
from pathlib import Path

_LIBRARY_FILE = "prompt_library.json"

# Prompts tracked in the library (keys must match get_prompt() calls in prompts.py)
TRACKED_PROMPTS = [
    "system_claude",
    "system_haiku",
    "system_gemini",
    "system_deepseek",
    "routing",
    "peer_review",
    "ensemble_synthesis",
    "red_team",
    "cot_reasoning",
    "compression",
]


def _resolve_path() -> Path:
    base = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
    return base / _LIBRARY_FILE


def _load() -> dict:
    try:
        return json.loads(_resolve_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(library: dict) -> None:
    try:
        _resolve_path().write_text(
            json.dumps(library, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


class PromptLibrary:
    """
    Versioned prompt store with outcome tracking.
    Module-level singleton is `prompt_library`.
    """

    def bootstrap(self, prompts_module) -> None:
        """
        Seed the library with v1 for each tracked prompt from the static
        constants in prompts.py.  Called once at startup if library is empty.
        Never overwrites existing versions.
        """
        try:
            library = _load()
            changed = False

            _name_to_attr = {
                "system_claude": "SYSTEM_PROMPT_CLAUDE",
                "system_haiku": "SYSTEM_PROMPT_HAIKU",
                "system_gemini": "SYSTEM_PROMPT_GEMINI",
                "system_deepseek": "SYSTEM_PROMPT_DEEPSEEK",
                "routing": "ROUTING_PROMPT",
                "peer_review": "PEER_REVIEW_PROMPT",
                "ensemble_synthesis": "ENSEMBLE_SYNTHESIS_PROMPT",
                "red_team": "RED_TEAM_PROMPT",
                "cot_reasoning": "COT_REASONING_PROMPT",
                "compression": "COMPRESSION_PROMPT",
            }

            for name in TRACKED_PROMPTS:
                if name in library:
                    continue  # already bootstrapped
                attr = _name_to_attr.get(name)
                if not attr:
                    continue
                text = getattr(prompts_module, attr, None)
                if text is None:
                    continue
                library[name] = {
                    "active_id": "v1",
                    "versions": [
                        {
                            "id": "v1",
                            "text": text,
                            "rationale": "initial — seeded from prompts.py",
                            "created_at": datetime.datetime.utcnow().isoformat(),
                            "call_count": 0,
                            "error_count": 0,
                            "error_rate": 0.0,
                            "active": True,
                        }
                    ],
                }
                changed = True

            if changed:
                _save(library)
        except Exception:
            pass

    def get_active(self, name: str) -> str | None:
        """
        Return the active prompt text for `name`, or None if not found / error.
        Callers should fall back to the static constant if None is returned.
        """
        try:
            library = _load()
            entry = library.get(name)
            if not entry:
                return None
            active_id = entry.get("active_id")
            for v in entry.get("versions", []):
                if v.get("id") == active_id:
                    return v.get("text")
            return None
        except Exception:
            return None

    def propose(self, name: str, new_text: str, rationale: str) -> str:
        """
        Create a new INACTIVE version for `name`.
        Returns the new version ID (e.g. "v3").
        Raises ValueError if name is not tracked.
        """
        if name not in TRACKED_PROMPTS:
            raise ValueError(f"'{name}' is not a tracked prompt. Tracked: {TRACKED_PROMPTS}")
        try:
            library = _load()
            entry = library.setdefault(name, {"active_id": "v1", "versions": []})
            existing_ids = [v["id"] for v in entry.get("versions", [])]
            # Generate next version ID
            max_n = 0
            for vid in existing_ids:
                try:
                    max_n = max(max_n, int(vid.lstrip("v")))
                except ValueError:
                    pass
            new_id = f"v{max_n + 1}"
            entry["versions"].append({
                "id": new_id,
                "text": new_text,
                "rationale": rationale,
                "created_at": datetime.datetime.utcnow().isoformat(),
                "call_count": 0,
                "error_count": 0,
                "error_rate": 0.0,
                "active": False,
            })
            library[name] = entry
            _save(library)
            return new_id
        except Exception as e:
            raise RuntimeError(f"propose failed: {e}") from e

    def activate(self, name: str, version_id: str) -> None:
        """
        Set `version_id` as the active version for `name`.
        Marks the previously active version as inactive.
        """
        try:
            library = _load()
            entry = library.get(name)
            if not entry:
                return
            for v in entry.get("versions", []):
                v["active"] = v["id"] == version_id
            entry["active_id"] = version_id
            library[name] = entry
            _save(library)
        except Exception:
            pass

    def record_outcome(self, name: str, was_error: bool) -> None:
        """
        Increment call_count (+1) and error_count (+1 if error) on the active
        version.  Recomputes error_rate.  Best-effort — never raises.
        """
        try:
            library = _load()
            entry = library.get(name)
            if not entry:
                return
            active_id = entry.get("active_id")
            changed = False
            for v in entry.get("versions", []):
                if v.get("id") == active_id:
                    v["call_count"] = v.get("call_count", 0) + 1
                    if was_error:
                        v["error_count"] = v.get("error_count", 0) + 1
                    total = v["call_count"]
                    v["error_rate"] = round(v["error_count"] / total, 4) if total else 0.0
                    changed = True
                    break
            if changed:
                library[name] = entry
                _save(library)
        except Exception:
            pass

    def get_history(self, name: str) -> list[dict]:
        """Return all versions for `name`, newest first, without prompt text (too large)."""
        try:
            library = _load()
            entry = library.get(name, {})
            versions = entry.get("versions", [])
            # Return metadata only (no full text) to keep response compact
            return [
                {
                    "id": v.get("id"),
                    "rationale": v.get("rationale"),
                    "created_at": v.get("created_at"),
                    "active": v.get("active", False),
                    "call_count": v.get("call_count", 0),
                    "error_count": v.get("error_count", 0),
                    "error_rate": v.get("error_rate", 0.0),
                    "text_length": len(v.get("text", "")),
                }
                for v in reversed(versions)
            ]
        except Exception:
            return []

    def get_summary(self) -> dict:
        """
        Return {prompt_name: {active_id, active_error_rate, version_count}}
        for all tracked prompts.
        """
        try:
            library = _load()
            summary = {}
            for name in TRACKED_PROMPTS:
                entry = library.get(name, {})
                active_id = entry.get("active_id", "v1")
                versions = entry.get("versions", [])
                active_v = next((v for v in versions if v.get("id") == active_id), {})
                summary[name] = {
                    "active_id": active_id,
                    "active_error_rate": active_v.get("error_rate", 0.0),
                    "active_call_count": active_v.get("call_count", 0),
                    "version_count": len(versions),
                    "bootstrapped": bool(versions),
                }
            return summary
        except Exception:
            return {}


# Module-level singleton
prompt_library = PromptLibrary()
