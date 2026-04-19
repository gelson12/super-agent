#!/usr/bin/env python3
"""
One-shot patch: adds a "← Observability" back button to the live
Crypto Specialist v3 dashboard HTML in n8n.

Run once from anywhere that has network access to n8n:
  python scripts/patch_crypto_dashboard_backbtn.py

Reads N8N_BASE_URL and N8N_API_KEY from environment (or .env).
"""
import json
import os
import sys
import urllib.request
import urllib.error

N8N_URL  = os.environ.get("N8N_BASE_URL", "").rstrip("/")
N8N_KEY  = os.environ.get("N8N_API_KEY", "")
# Known workflow ID from ROLLBACK_GUIDE.md
CRYPTO_WF_ID = "7onbBjeUwHkSsuyc"
# The super-agent observability URL to link back to
OBS_URL = os.environ.get("SUPER_AGENT_URL", "https://super-agent-production.up.railway.app").rstrip("/") + "/observability"

HEADERS = {
    "X-N8N-API-KEY": N8N_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

BACK_BUTTON_HTML = (
    '<a href="' + OBS_URL + '" '
    'style="position:fixed;top:14px;left:16px;z-index:9999;'
    'display:inline-flex;align-items:center;gap:6px;'
    'background:rgba(10,10,30,0.85);border:1px solid rgba(0,212,255,0.4);'
    'color:#00d4ff;text-decoration:none;font-family:monospace;font-size:12px;'
    'letter-spacing:1px;padding:6px 14px;border-radius:4px;'
    'backdrop-filter:blur(8px);transition:all 0.2s;" '
    'onmouseover="this.style.background=\'rgba(0,212,255,0.15)\'" '
    'onmouseout="this.style.background=\'rgba(10,10,30,0.85)\'">'
    '← OBSERVABILITY'
    '</a>'
)
MARKER = "<!-- BACK_BTN_INJECTED -->"


def _get(path):
    req = urllib.request.Request(f"{N8N_URL}{path}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _put(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{N8N_URL}{path}", data=data, headers=HEADERS, method="PUT")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def patch():
    if not N8N_URL or not N8N_KEY:
        print("ERROR: N8N_BASE_URL and N8N_API_KEY must be set.")
        sys.exit(1)

    print(f"Fetching workflow {CRYPTO_WF_ID}…")
    wf = _get(f"/api/v1/workflows/{CRYPTO_WF_ID}")
    print(f"  Name: {wf.get('name')}")

    nodes = wf.get("nodes", [])
    patched = 0

    for node in nodes:
        ntype = node.get("type", "")
        # Target: Respond to Webhook nodes and HTML nodes
        if "respondToWebhook" in ntype or "html" in ntype.lower() or "set" in ntype.lower():
            params = node.get("parameters", {})
            # Look for HTML content in any string parameter
            for key, val in params.items():
                if isinstance(val, str) and "<!DOCTYPE" in val and MARKER not in val:
                    # Inject button just after <body (or at start of body content)
                    if "<body" in val:
                        insert_after = val.index("<body")
                        body_tag_end = val.index(">", insert_after) + 1
                        val = val[:body_tag_end] + "\n" + MARKER + BACK_BUTTON_HTML + val[body_tag_end:]
                    else:
                        val = MARKER + BACK_BUTTON_HTML + val
                    params[key] = val
                    patched += 1
                    print(f"  ✓ Patched HTML in node '{node.get('name', node.get('type'))}' (key: {key})")

                # Handle nested dicts (n8n sometimes wraps in {value: ...})
                elif isinstance(val, dict):
                    for subkey, subval in val.items():
                        if isinstance(subval, str) and "<!DOCTYPE" in subval and MARKER not in subval:
                            if "<body" in subval:
                                insert_after = subval.index("<body")
                                body_tag_end = subval.index(">", insert_after) + 1
                                subval = subval[:body_tag_end] + "\n" + MARKER + BACK_BUTTON_HTML + subval[body_tag_end:]
                            else:
                                subval = MARKER + BACK_BUTTON_HTML + subval
                            val[subkey] = subval
                            patched += 1
                            print(f"  ✓ Patched HTML in node '{node.get('name')}' (nested key: {subkey})")

    if patched == 0:
        print("  ⚠ No HTML content found to patch. The dashboard HTML may be generated dynamically.")
        print("    In that case: open the n8n workflow, find the 'Respond to Webhook' node,")
        print("    and add this button HTML just after <body>:")
        print()
        print(BACK_BUTTON_HTML)
        sys.exit(0)

    print(f"\nPushing updated workflow ({patched} node(s) patched)…")
    _put(f"/api/v1/workflows/{CRYPTO_WF_ID}", wf)
    print("  ✓ Done. Reload the crypto dashboard to see the back button.")


if __name__ == "__main__":
    try:
        patch()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:300]}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
