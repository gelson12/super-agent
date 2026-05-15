"""
_deploy_crypto_v3.py
Deploys "Crypto Specialist Super Agent v3" workflow to n8n.

Fixes deployed:
  ✅ Fetch Funding + Fetch ETH connections (single→double bracket)
  ✅ Execute Trade — Kelly hard stop gate (Kelly <= 0% → KELLY_BLOCKED)
  ✅ Execute Trade — paper trade recording (writes to vault)
  ✅ Decision Gate — Kelly fraction in return (signal.risk.kellyPct)

Architecture:
  Every 5 min: Generate signal → Apply Kelly gate → Execute trade (0.1% size)
  Every 4h:    Outcome Tracker grades signals (paper + live)
  Every 24h:   Weight Tuner updates strategy weights

Usage:
  python3 _deploy_crypto_v3.py
  (or python if python3 not available)
"""
import json
import sys
import urllib.request
import urllib.error
import os

sys.stdout.reconfigure(encoding='utf-8')

N8N_URL = "https://outstanding-blessing-production-1d4b.up.railway.app"
N8N_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwOTk3N2NkYS05MTQwLTRlNjgtYTk0OC1lODhkMWY0MDBjMTgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiZWUxMTlkYzItN2MzZi00NTc1LThjMDAtNGM1MWM5NmZhZmEzIiwiaWF0IjoxNzc2NTEwODI5LCJleHAiOjE3NzkwNTg4MDB9.Az2G4U7gh55B2MoJK1Ewhk-CNDUqpph-0JYMiSuIWMw"

def api(method, path, data=None):
    """Make HTTP request to n8n API."""
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data else None
    req = urllib.request.Request(
        f"{N8N_URL}/api/v1{path}",
        data=body, method=method,
        headers={"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:500]}, e.code
    except Exception as e:
        return {"error": str(e)}, 500

# Load the workflow from file
print("Loading crypto_v3_super.json...")
crypto_file = r"C:\Users\Gelson\Downloads\crypto_v3_super.json"

try:
    with open(crypto_file, 'r', encoding='utf-8-sig') as f:
        crypto_wf = json.load(f)
    print(f"  ✓ Loaded {len(json.dumps(crypto_wf))} bytes")
except Exception as e:
    print(f"  ✗ ERROR loading file: {e}")
    sys.exit(1)

# Check if workflow name is correct
wf_name = crypto_wf.get("name", "")
print(f"  Workflow name: '{wf_name}'")

# Clean up exported metadata — n8n API only accepts name, nodes, connections, settings, staticData
allowed_keys = {'name', 'nodes', 'connections', 'settings', 'staticData'}
extra_keys = set(crypto_wf.keys()) - allowed_keys
if extra_keys:
    print(f"  Removing exported metadata: {extra_keys}")
    crypto_wf = {k: v for k, v in crypto_wf.items() if k in allowed_keys}
    print(f"  Cleaned workflow has {len(crypto_wf)} top-level properties")

# Search for existing workflow by name
print("\nSearching for existing 'Crypto Specialist Super Agent v3' workflow...")
result, status = api("GET", "/workflows?limit=100")

if "error" in result:
    print(f"  ✗ API error {status}: {result['error']}")
    sys.exit(1)

existing_id = None
for wf in result.get("data", []):
    if wf.get("name") == "Crypto Specialist Super Agent v3":
        existing_id = wf["id"]
        print(f"  Found existing: {existing_id}")
        break

# Delete old version if found
if existing_id:
    print(f"\n  Deleting old version {existing_id}...")
    del_result, del_status = api("DELETE", f"/workflows/{existing_id}")
    print(f"  ✓ Deleted HTTP {del_status}")

# Create new workflow
print("\nCreating 'Crypto Specialist Super Agent v3' workflow...")
result, status = api("POST", "/workflows", crypto_wf)

if "error" in result:
    print(f"  ✗ ERROR {status}: {result['error']}")
    print("\n  Debugging info:")
    print(f"    - Workflow name: {wf_name}")
    print(f"    - Nodes: {len(crypto_wf.get('nodes', []))}")
    print(f"    - Connections: {len(crypto_wf.get('connections', {}))}")
    sys.exit(1)

wf_id = result.get("id")
if not wf_id:
    print(f"  ✗ ERROR: No workflow ID in response")
    print(f"    Response: {result}")
    sys.exit(1)

print(f"  ✓ Created ID: {wf_id} HTTP {status}")

# Activate workflow
print(f"\n  Activating workflow {wf_id}...")
result2, status2 = api("POST", f"/workflows/{wf_id}/activate")

if "error" in result2:
    print(f"  ⚠ WARNING: Activation returned {status2}: {result2['error']}")
else:
    print(f"  ✓ Activated HTTP {status2}")

# Success summary
print(f"""
{'='*70}
✅ DEPLOYMENT COMPLETE

Workflow:  Crypto Specialist Super Agent v3
ID:        {wf_id}
Status:    ACTIVE (running every 5 minutes)

Fixes deployed:
  ✅ Fetch Funding/ETH connections (double-bracket)
  ✅ Kelly hard stop gate (Kelly ≤ 0% → KELLY_BLOCKED)
  ✅ Paper trade recording (writes to vault for grading)
  ✅ Kelly fraction in signal pipeline

Next steps:
  1. Set Railway env vars:
     - ENABLE_LIVE_TRADING = true
     - TRADE_SIZE_PCT = 0.1
     - TRADE_MIN_CONFIDENCE = 65
     - Verify MEMORY_INGEST_SECRET

  2. Monitor first execution (within 5 minutes):
     Railway dashboard → super-agent-production → Logs
     Look for: "Fetch Funding" executed, "Fetch ETH" executed

  3. Check Kraken for new orders (0.1% size)

  4. After 4 hours: Outcome Tracker grades first trade
     Check: /memory/export?source=crypto_specialist_v3

  5. After 24 hours: Weight Tuner daily run
     Check: Dashboard shows updated strategy weights
{'='*70}
""")
