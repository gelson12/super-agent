"""
Build recipes — records successful build tool-call sequences and replays them.

When a Flutter build completes successfully, the shell agent records which steps
ran and in what order as a "recipe". Next time a similar build is requested,
the recipe is injected into the agent's context so it skips re-planning and
goes straight to what worked last time.

Stored at /workspace/build_recipes.json
Max 20 recipes kept (oldest pruned).
"""
import json, os, time, datetime
from pathlib import Path

_DIR = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
RECIPES_PATH = _DIR / "build_recipes.json"
_MAX_RECIPES = 20


def _load() -> list:
    try:
        if RECIPES_PATH.exists():
            return json.loads(RECIPES_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def save_recipe(project_name: str, steps: list[str], notes: str = "") -> None:
    """
    Record a successful build sequence.

    Args:
        project_name: e.g. "super_agent_voice"
        steps: ordered list of what was done, e.g.
               ["flutter create", "patch build.gradle minSdk=24",
                "write AndroidManifest", "flutter build apk",
                "upload to Railway /downloads/TOKEN/app-debug.apk"]
        notes: any extra context (e.g. flutter_tts version, minSdk needed)
    """
    recipe = {
        "id": f"{project_name}_{int(time.time())}",
        "project_name": project_name,
        "recorded_at": datetime.datetime.utcnow().isoformat(),
        "steps": steps,
        "notes": notes,
        "times_used": 0,
    }
    try:
        existing = _load()
        # Remove older recipes for same project (keep latest)
        existing = [r for r in existing if r.get("project_name") != project_name]
        existing.append(recipe)
        if len(existing) > _MAX_RECIPES:
            existing = existing[-_MAX_RECIPES:]
        RECIPES_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        from ..activity_log import bg_log
        bg_log(f"Saved build recipe for '{project_name}' ({len(steps)} steps)", source="build_recipes")
    except Exception:
        pass


def get_recipe(project_name: str) -> dict | None:
    """Return the most recent recipe for a project name, or None."""
    recipes = _load()
    # Exact match first, then partial
    for r in reversed(recipes):
        if r.get("project_name") == project_name:
            return r
    for r in reversed(recipes):
        if project_name.lower() in r.get("project_name", "").lower():
            return r
    return None


def get_latest_recipe() -> dict | None:
    """Return the most recently saved recipe regardless of project."""
    recipes = _load()
    return recipes[-1] if recipes else None


def mark_used(recipe_id: str) -> None:
    """Increment the times_used counter for a recipe."""
    try:
        existing = _load()
        for r in existing:
            if r.get("id") == recipe_id:
                r["times_used"] = r.get("times_used", 0) + 1
                r["last_used_at"] = datetime.datetime.utcnow().isoformat()
        RECIPES_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception:
        pass


def build_context_hint(project_name: str) -> str:
    """
    Returns a natural-language context string to inject into the shell agent prompt.
    Empty string if no recipe found — agent plans from scratch as usual.
    """
    recipe = get_recipe(project_name)
    if not recipe:
        return ""
    steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(recipe["steps"]))
    hint = (
        f"WINNING RECIPE from last successful build of '{recipe['project_name']}' "
        f"({recipe['recorded_at'][:10]}, used {recipe['times_used']} times):\n"
        f"{steps_text}\n"
        f"Notes: {recipe['notes'] or 'none'}\n"
        f"Follow this recipe exactly unless a step fails, then adapt."
    )
    mark_used(recipe["id"])
    return hint


def list_recipes() -> list[dict]:
    """Return all recipes (summary view, no full steps list)."""
    return [
        {
            "id": r["id"],
            "project_name": r["project_name"],
            "recorded_at": r["recorded_at"],
            "step_count": len(r.get("steps", [])),
            "times_used": r.get("times_used", 0),
            "notes": r.get("notes", ""),
        }
        for r in _load()
    ]
