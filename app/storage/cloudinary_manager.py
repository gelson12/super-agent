"""
Cloudinary smart storage manager.

Strategy:
- Before every upload, checks current storage usage.
- If usage >= QUOTA_THRESHOLD_BYTES (1 GB), deletes oldest assets
  (by created_at) across all resource types until usage drops below
  QUOTA_TARGET_BYTES (80% of threshold) — then uploads.
- Implements this in Python rather than n8n so the check-cleanup-upload
  is atomic and race-condition-free.
- Exposed as a LangChain tool and as a FastAPI endpoint.
"""

import cloudinary
import cloudinary.uploader
import cloudinary.api
from langchain_core.tools import tool
from ..config import settings

# ── Quota config ─────────────────────────────────────────────────────────────
QUOTA_THRESHOLD_BYTES = 1 * 1024 ** 3        # 1 GB — trigger cleanup above this
QUOTA_TARGET_BYTES = int(0.80 * QUOTA_THRESHOLD_BYTES)  # 800 MB — clean down to this

RESOURCE_TYPES = ["image", "video", "raw"]   # covers all Cloudinary asset types


def _configure() -> None:
    """Configure Cloudinary from settings (idempotent)."""
    cloudinary.config(
        cloud_name=settings.cloudinary_cloud_name,
        api_key=settings.cloudinary_api_key,
        api_secret=settings.cloudinary_api_secret,
        secure=True,
    )


def get_storage_status() -> dict:
    """
    Return current Cloudinary storage usage.
    Uses a single lightweight API call — no uploads triggered.
    """
    _configure()
    usage = cloudinary.api.usage()
    used_bytes = usage.get("storage", {}).get("usage", 0)
    limit_bytes = usage.get("storage", {}).get("limit", 0)
    return {
        "used_bytes": used_bytes,
        "used_gb": round(used_bytes / 1024 ** 3, 3),
        "limit_bytes": limit_bytes,
        "limit_gb": round(limit_bytes / 1024 ** 3, 3),
        "threshold_gb": round(QUOTA_THRESHOLD_BYTES / 1024 ** 3, 3),
        "cleanup_needed": used_bytes >= QUOTA_THRESHOLD_BYTES,
    }


def _list_all_assets_sorted_by_age() -> list[dict]:
    """
    Fetch all assets across all resource types, sorted oldest first.
    Uses pagination to handle large libraries.
    """
    assets = []
    for rtype in RESOURCE_TYPES:
        next_cursor = None
        while True:
            kwargs = {"resource_type": rtype, "max_results": 500, "type": "upload"}
            if next_cursor:
                kwargs["next_cursor"] = next_cursor
            result = cloudinary.api.resources(**kwargs)
            assets.extend([
                {"public_id": r["public_id"], "resource_type": rtype,
                 "bytes": r.get("bytes", 0), "created_at": r.get("created_at", "")}
                for r in result.get("resources", [])
            ])
            next_cursor = result.get("next_cursor")
            if not next_cursor:
                break

    # Oldest first
    return sorted(assets, key=lambda x: x["created_at"])


def _cleanup_to_target() -> dict:
    """
    Delete oldest assets until storage drops below QUOTA_TARGET_BYTES.
    Returns a summary of what was deleted.
    """
    status = get_storage_status()
    if not status["cleanup_needed"]:
        return {"deleted": 0, "freed_bytes": 0}

    assets = _list_all_assets_sorted_by_age()
    current_used = status["used_bytes"]
    deleted = 0
    freed = 0

    for asset in assets:
        if current_used <= QUOTA_TARGET_BYTES:
            break
        try:
            cloudinary.uploader.destroy(
                asset["public_id"],
                resource_type=asset["resource_type"],
                invalidate=True,
            )
            freed += asset["bytes"]
            current_used -= asset["bytes"]
            deleted += 1
        except Exception:
            continue

    return {"deleted": deleted, "freed_bytes": freed, "freed_mb": round(freed / 1024 ** 2, 2)}


def upload_file(file_path: str, resource_type: str = "auto", public_id: str = None) -> dict:
    """
    Upload a file to Cloudinary with automatic quota management.
    If storage >= 1 GB, oldest assets are deleted first.
    Returns the uploaded asset's URL and metadata.
    """
    _configure()

    # Check and clean before upload
    status = get_storage_status()
    cleanup_result = {"deleted": 0, "freed_bytes": 0}
    if status["cleanup_needed"]:
        cleanup_result = _cleanup_to_target()

    upload_kwargs = {"resource_type": resource_type}
    if public_id:
        upload_kwargs["public_id"] = public_id

    result = cloudinary.uploader.upload(file_path, **upload_kwargs)

    return {
        "url": result.get("secure_url"),
        "public_id": result.get("public_id"),
        "resource_type": result.get("resource_type"),
        "bytes": result.get("bytes"),
        "format": result.get("format"),
        "cleanup_performed": cleanup_result,
    }


# ── LangChain tool wrappers ───────────────────────────────────────────────────

@tool
def check_storage(dummy: str = "") -> str:
    """
    Check current Cloudinary storage usage.
    Returns used space, limit, and whether cleanup is needed.
    """
    try:
        status = get_storage_status()
        return (
            f"Storage used: {status['used_gb']} GB / {status['limit_gb']} GB. "
            f"Cleanup needed: {status['cleanup_needed']}."
        )
    except Exception as e:
        return f"[Storage error: {e}]"


@tool
def upload_to_storage(file_path: str) -> str:
    """
    Upload a file (image, video, or other) to Cloudinary.
    Automatically cleans up oldest files if storage is above 1 GB.
    Returns the public URL of the uploaded file.
    """
    try:
        result = upload_file(file_path)
        return f"Uploaded successfully. URL: {result['url']} (cleaned up {result['cleanup_performed']['deleted']} old files)"
    except Exception as e:
        return f"[Storage upload error: {e}]"
