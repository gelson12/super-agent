"""
bridge_nightly_n8n_backup.py — fetch all n8n workflows and save to n8n/backups/YYYY-MM-DD/.

Run manually:   python scripts/bridge_nightly_n8n_backup.py
Schedule via:   cron / n8n "Execute Command" node / Railway cron

Env vars required:
  N8N_BASE_URL       e.g. https://outstanding-blessing-production-1d4b.up.railway.app
  N8N_API_KEY        n8n API key (same one used by bots)
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

N8N_BASE_URL = os.environ.get("N8N_BASE_URL", "").rstrip("/")
N8N_API_KEY  = os.environ.get("N8N_API_KEY", "")

def main() -> int:
    if not N8N_BASE_URL or not N8N_API_KEY:
        print("ERROR: N8N_BASE_URL and N8N_API_KEY must be set", file=sys.stderr)
        return 1

    headers = {"X-N8N-API-KEY": N8N_API_KEY, "Accept": "application/json"}

    # Paginate through all workflows
    all_workflows: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(f"{N8N_BASE_URL}/api/v1/workflows", headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        workflows = body.get("data", [])
        all_workflows.extend(workflows)
        next_cursor = body.get("nextCursor")
        if not next_cursor or not workflows:
            break
        cursor = next_cursor

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_dir = Path(__file__).parent.parent / "n8n" / "backups" / today
    backup_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    for wf in all_workflows:
        wf_id   = wf.get("id", "unknown")
        wf_name = wf.get("name", wf_id)
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in wf_name).strip()
        filename = backup_dir / f"{safe_name}_{wf_id}.json"
        filename.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
        saved += 1

    # Write manifest
    manifest = {
        "backup_date": today,
        "total_workflows": saved,
        "n8n_base_url": N8N_BASE_URL,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (backup_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Backed up {saved} workflows → {backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
