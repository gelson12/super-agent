"""Sprite upload helper for the office simulation.

POST /api/office_sim/sprites/{bot_id} → save uploaded PNGs to disk
(immediate effect on the live deploy) AND commit to the gelson12/super-agent
repo on master so they survive the next Railway redeploy.

Slot → filename mapping mirrors what `static/office_sim/js/sprites.js`
already probes; the runtime cache (sprites.reloadBot) cache-busts these
URLs after the upload completes so the bot's sprite refreshes in-place.
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Dict, List

from fastapi import HTTPException, UploadFile

_log = logging.getLogger("sprite_upload")

# Repo paths
_REPO_ROOT = Path(__file__).resolve().parent.parent           # super-agent/
_BOTS_DIR = _REPO_ROOT / "static" / "office_sim" / "assets" / "sprites" / "bots"
_BOTS_JSON = _REPO_ROOT / "static" / "office_sim" / "data" / "bots.json"

# Frontend slot id  →  filesystem filename. Matches sprites.js BOT_DIR_FRAMES.
SLOT_TO_FILENAME: Dict[str, str] = {
    "stand_down":   "stand_down.png",
    "stand_left":   "stand_left.png",
    "stand_right":  "stand_right.png",
    "stand_up":     "stand_up.png",
    "walk_down_1":  "walk_down.png",
    "walk_down_2":  "walk_down_2.png",
    "walk_left_1":  "walk_left.png",
    "walk_left_2":  "walk_left_2.png",
    "walk_right_1": "walk_right.png",
    "walk_right_2": "walk_right_2.png",
    "walk_up_1":    "walk_up.png",
    "walk_up_2":    "walk_up_2.png",
}

# Folder aliases the runtime accepts (mirrors sprites.js FOLDER_ALIASES).
# We pick the bot id as the canonical destination folder; aliases are
# only relevant for READING already-uploaded sprites, not writing.
_VALID_BOT_IDS_CACHE: List[str] | None = None


def _load_valid_bot_ids() -> List[str]:
    global _VALID_BOT_IDS_CACHE
    if _VALID_BOT_IDS_CACHE is None:
        try:
            doc = json.loads(_BOTS_JSON.read_text(encoding="utf-8"))
            _VALID_BOT_IDS_CACHE = [b["id"] for b in doc.get("bots", [])]
        except Exception as e:
            _log.warning("Could not load bots.json: %s", e)
            _VALID_BOT_IDS_CACHE = []
    return _VALID_BOT_IDS_CACHE


def _alpha_key_inplace(path: Path) -> bool:
    """Alpha-key a saved PNG in place. No-op if it already has alpha.
    Returns True if it touched the file."""
    try:
        from PIL import Image
    except ImportError:
        _log.warning("Pillow not installed — skipping alpha-key for %s", path)
        return False

    img = Image.open(path).convert("RGBA")
    px = img.load()
    w, h = img.size

    # Sample to detect existing transparency
    sample_alphas = []
    for sy in range(0, h, max(1, h // 20)):
        for sx in range(0, w, max(1, w // 20)):
            sample_alphas.append(px[sx, sy][3])
    if sum(1 for a in sample_alphas if a < 32) / max(1, len(sample_alphas)) > 0.05:
        return False

    WHITE = 235
    FEATHER = 6
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if r >= WHITE and g >= WHITE and b >= WHITE:
                px[x, y] = (r, g, b, 0)
            else:
                m = min(r, g, b)
                if m >= WHITE - FEATHER:
                    falloff = (WHITE - m) / FEATHER
                    px[x, y] = (r, g, b, int(a * falloff))
    img.save(path, optimize=True)
    return True


def _save_locally(bot_id: str, slot_files: Dict[str, bytes]) -> List[Path]:
    """Save uploaded files to disk under bots/<bot_id>/<filename>.png.
    Runs alpha-key in place so white-bg uploads work immediately."""
    target_dir = _BOTS_DIR / bot_id
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []
    for slot, data in slot_files.items():
        if slot not in SLOT_TO_FILENAME:
            continue
        path = target_dir / SLOT_TO_FILENAME[slot]
        path.write_bytes(data)
        try:
            _alpha_key_inplace(path)
        except Exception as e:
            _log.warning("alpha-key failed for %s: %s", path, e)
        saved.append(path)
    return saved


def _commit_to_github(bot_id: str, slot_files: Dict[str, bytes]) -> Dict[str, object]:
    """Commit binary PNGs to gelson12/super-agent on master so they
    persist across Railway redeploys. Uses PyGithub directly because
    the existing github_create_or_update_file tool only handles text."""
    try:
        from github import Github, GithubException
    except ImportError:
        return {"ok": False, "reason": "PyGithub not installed"}

    pat = os.environ.get("GITHUB_PAT")
    if not pat:
        return {"ok": False, "reason": "GITHUB_PAT not set"}

    try:
        client = Github(pat)
        repo = client.get_repo("gelson12/super-agent")
        committed: List[str] = []
        errors: List[str] = []
        for slot, data in slot_files.items():
            if slot not in SLOT_TO_FILENAME:
                continue
            file_name = SLOT_TO_FILENAME[slot]
            file_path = f"static/office_sim/assets/sprites/bots/{bot_id}/{file_name}"
            commit_msg = f"office_sim: upload {bot_id}/{file_name}"
            try:
                existing = repo.get_contents(file_path, ref="master")
                repo.update_file(file_path, commit_msg, data, existing.sha, branch="master")
                committed.append(file_path)
            except GithubException as e:
                if e.status == 404:
                    repo.create_file(file_path, commit_msg, data, branch="master")
                    committed.append(file_path)
                else:
                    errors.append(f"{file_path}: {e}")
            except Exception as e:
                errors.append(f"{file_path}: {type(e).__name__}: {e}")
        return {"ok": len(errors) == 0, "committed": committed, "errors": errors}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


async def process_sprite_upload(
    bot_id: str,
    uploaded: Dict[str, UploadFile],
) -> Dict[str, object]:
    """Validate, save, and commit. Returns a JSON-serialisable dict."""
    valid_ids = _load_valid_bot_ids()
    if valid_ids and bot_id not in valid_ids:
        raise HTTPException(status_code=400, detail=f"Unknown bot id: {bot_id}")

    # Read each upload into memory once (multipart streams).
    slot_bytes: Dict[str, bytes] = {}
    for slot, upload in uploaded.items():
        if slot not in SLOT_TO_FILENAME:
            continue
        if upload is None:
            continue
        data = await upload.read()
        if not data:
            continue
        slot_bytes[slot] = data

    if not slot_bytes:
        raise HTTPException(status_code=400, detail="No files received")

    saved_paths = _save_locally(bot_id, slot_bytes)

    # Commit to GitHub asynchronously (fire-and-forget on a thread to keep
    # the response snappy). For now we keep it synchronous so the user sees
    # a definitive committed=true|false in the response — GitHub commits
    # take 1-2s which is acceptable.
    git_result = _commit_to_github(bot_id, slot_bytes)

    return {
        "ok": True,
        "bot_id": bot_id,
        "saved": [str(p.relative_to(_REPO_ROOT)) for p in saved_paths],
        "n_files": len(saved_paths),
        "committed": git_result.get("ok", False),
        "github": git_result,
    }
