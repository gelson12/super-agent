#!/usr/bin/env python3
"""
RECON OS — Deployment Script
Imports all 22 RECON OS workflows into n8n, patches cross-workflow IDs,
activates them, and prints the full ID map.

Usage:
  python deploy_recon_os.py                  # import only
  python deploy_recon_os.py --activate       # import + activate
  python deploy_recon_os.py --delete-old     # remove existing RECON OS workflows first
  python deploy_recon_os.py --schema         # also print schema migration reminder

Requirements:
  pip install requests
  export N8N_BASE_URL=https://outstanding-blessing-production-1d4b.up.railway.app
  export N8N_API_KEY=<your_api_key>
"""

import os, sys, json, time, re, argparse
from pathlib import Path
import requests

N8N_BASE_URL = os.getenv("N8N_BASE_URL", "https://outstanding-blessing-production-1d4b.up.railway.app")
N8N_API_KEY  = os.getenv("N8N_API_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwOTk3N2NkYS05MTQwLTRlNjgtYTk0OC1lODhkMWY0MDBjMTgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiZWUxMTlkYzItN2MzZi00NTc1LThjMDAtNGM1MWM5NmZhZmEzIiwiaWF0IjoxNzc2NTEwODI5LCJleHAiOjE3NzkwNTg4MDB9.Az2G4U7gh55B2MoJK1Ewhk-CNDUqpph-0JYMiSuIWMw")

HEADERS = {
    "X-N8N-API-KEY": N8N_API_KEY,
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}

# Import order respects dependency chain — dependents first
IMPORT_ORDER = [
    "WF19_audit_logger.json",
    "WF02_scope_guardian.json",
    "WF11_risk_scorer.json",
    "WF10_finding_deduplicator.json",
    "WF16_retest_validator.json",
    "WF13_executive_summary.json",
    "WF07_recon_screenshot.json",
    "WF06_recon_http.json",
    "WF05_recon_dns.json",
    "WF08_vuln_ingest.json",
    "WF09_validation_queue.json",
    "WF03_asset_discovery.json",
    "WF04_recon_orchestrator.json",
    "WF12_report_assembler.json",
    "WF15_retest_scheduler.json",
    "WF14_remediation_tracker.json",
    "WF17_daily_briefing.json",
    "WF18_engagement_dashboard.json",
    "WF20_sla_watchdog.json",
    "WF22_weakness_heatmap.json",
    "WF21_engagement_close.json",
    "WF01_engagement_kickoff.json",
]

# Placeholder → actual ID mapping (filled after first-pass import)
PLACEHOLDER_MAP: dict[str, str] = {}

def api(method: str, path: str, **kwargs) -> requests.Response:
    url = f"{N8N_BASE_URL}/api/v1{path}"
    resp = requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
    return resp

def list_workflows() -> list[dict]:
    results, cursor = [], None
    while True:
        params = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        r = api("GET", "/workflows", params=params)
        if r.status_code != 200:
            print(f"  ✗ Failed to list workflows: {r.status_code} {r.text[:200]}")
            return results
        data = r.json()
        results.extend(data.get("data", []))
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return results

def delete_workflow(wf_id: str, name: str):
    r = api("DELETE", f"/workflows/{wf_id}")
    if r.status_code == 200:
        print(f"  🗑  Deleted: {name} ({wf_id})")
    else:
        print(f"  ✗ Delete failed: {name} → {r.status_code}")

def import_workflow(filepath: Path) -> tuple[str | None, str]:
    with open(filepath) as f:
        wf = json.load(f)
    name = wf.get("name", filepath.stem)
    r = api("POST", "/workflows", json=wf)
    if r.status_code in (200, 201):
        wf_id = r.json().get("id")
        print(f"  ✓ Imported: {name}  →  ID: {wf_id}")
        return wf_id, name
    else:
        print(f"  ✗ Failed:   {name}  →  {r.status_code}: {r.text[:300]}")
        return None, name

def patch_placeholders(filepath: Path, id_map: dict[str, str]) -> bool:
    """Replace PLACEHOLDER_WFxx_ID strings with real workflow IDs in a JSON file."""
    text = filepath.read_text(encoding="utf-8")
    changed = False
    for placeholder, real_id in id_map.items():
        if placeholder in text:
            text = text.replace(placeholder, real_id)
            changed = True
    if changed:
        filepath.write_text(text, encoding="utf-8")
    return changed

def update_workflow(wf_id: str, filepath: Path) -> bool:
    with open(filepath) as f:
        wf = json.load(f)
    r = api("PUT", f"/workflows/{wf_id}", json=wf)
    if r.status_code == 200:
        print(f"  ✓ Updated:  {wf.get('name')} (patched cross-workflow IDs)")
        return True
    else:
        print(f"  ✗ Update failed: {r.status_code}: {r.text[:200]}")
        return False

def activate_workflow(wf_id: str, name: str):
    r = api("PATCH", f"/workflows/{wf_id}", json={"active": True})
    if r.status_code == 200:
        print(f"  ✓ Activated: {name}")
    else:
        print(f"  ✗ Activation failed: {name} → {r.status_code}: {r.text[:150]}")

def main():
    parser = argparse.ArgumentParser(description="Deploy RECON OS to n8n")
    parser.add_argument("--activate",   action="store_true", help="Activate all workflows after import")
    parser.add_argument("--delete-old", action="store_true", help="Delete existing RECON OS workflows first")
    parser.add_argument("--schema",     action="store_true", help="Print schema migration instructions")
    parser.add_argument("--dir",        default=".",         help="Directory with workflow JSON files")
    args = parser.parse_args()

    if not N8N_API_KEY:
        print("ERROR: N8N_API_KEY not set"); sys.exit(1)

    wf_dir = Path(args.dir)
    print(f"\n{'='*65}")
    print(f"  RECON OS — Deployment to {N8N_BASE_URL}")
    print(f"{'='*65}\n")

    # ── Step 0: Delete old RECON OS workflows ──────────────────────────
    if args.delete_old:
        print("STEP 0: Removing existing RECON OS workflows...")
        existing = list_workflows()
        recon_wfs = [w for w in existing if w.get("name", "").startswith("RECON OS")]
        if not recon_wfs:
            print("  (none found)")
        for w in recon_wfs:
            delete_workflow(w["id"], w["name"])
        print()

    # ── Step 1: First-pass import ──────────────────────────────────────
    print("STEP 1: Importing workflows (first pass)...")
    name_to_id: dict[str, str] = {}
    file_to_id: dict[str, str] = {}

    for filename in IMPORT_ORDER:
        fp = wf_dir / filename
        if not fp.exists():
            print(f"  ⚠  Not found: {filename}")
            continue
        wf_id, name = import_workflow(fp)
        if wf_id:
            name_to_id[name] = wf_id
            file_to_id[filename] = wf_id
            # Build placeholder → real-ID map
            key = filename.replace(".json", "").upper()  # e.g. WF02_SCOPE_GUARDIAN
            PLACEHOLDER_MAP[f"PLACEHOLDER_{key}_ID"] = wf_id
        time.sleep(0.3)

    print(f"\n  Imported: {len(file_to_id)}/{len(IMPORT_ORDER)}\n")

    # ── Step 2: Patch cross-workflow ID references ─────────────────────
    print("STEP 2: Patching cross-workflow ID references...")
    needs_update: list[tuple[str, str, Path]] = []

    for filename in IMPORT_ORDER:
        fp = wf_dir / filename
        if not fp.exists() or filename not in file_to_id:
            continue
        changed = patch_placeholders(fp, PLACEHOLDER_MAP)
        if changed:
            needs_update.append((file_to_id[filename], filename, fp))
            print(f"  ✎  Patched: {filename}")

    if not needs_update:
        print("  (no cross-workflow references to patch)")

    # ── Step 3: Re-upload patched workflows ───────────────────────────
    if needs_update:
        print("\nSTEP 3: Re-uploading patched workflows...")
        for wf_id, filename, fp in needs_update:
            update_workflow(wf_id, fp)
            time.sleep(0.3)

    # ── Step 4: Activate ───────────────────────────────────────────────
    if args.activate:
        print("\nSTEP 4: Activating workflows...")
        for name, wf_id in name_to_id.items():
            activate_workflow(wf_id, name)
            time.sleep(0.2)

    # ── Summary ────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("  RECON OS Workflow ID Map")
    print(f"{'='*65}")
    for fname, wf_id in file_to_id.items():
        print(f"  {fname:<40} {wf_id}")

    if args.schema:
        print(f"\n{'='*65}")
        print("  DATABASE SCHEMA MIGRATION")
        print(f"{'='*65}")
        print("  Run against your Railway divine-contentment PostgreSQL:")
        print("  psql $DATABASE_URL < recon_os_schema.sql")
        print()
        print("  Then create a PostgreSQL credential in n8n:")
        print("  → Settings → Credentials → New → PostgreSQL")
        print("  → Name it exactly: 'RECON OS PostgreSQL'")
        print("  → Host: your Railway PG host")
        print("  → Database: railway (or your DB name)")
        print()
        print("  Required n8n environment variables:")
        print("  → RECON_OS_ANALYST_EMAIL  — analyst digest recipient")
        print("  → RECON_OS_SLACK_WEBHOOK  — Slack incoming webhook URL (optional)")

    print(f"\n  ✅ RECON OS deployment complete.\n")
    print("  Test it:")
    print(f"  curl -X POST {N8N_BASE_URL}/webhook/recon-os/engagement/new \\")
    print("    -H 'Content-Type: application/json' \\")
    print("    -d '{\"client_name\":\"Test Corp\",\"engagement_name\":\"Q2 Pentest\",")
    print("         \"authorized_targets\":[{\"value\":\"example.com\",\"scope_type\":\"domain\"}],")
    print("         \"testing_window_start\":\"2026-05-01T08:00:00Z\",")
    print("         \"testing_window_end\":\"2026-05-15T18:00:00Z\"}'")

if __name__ == "__main__":
    main()
