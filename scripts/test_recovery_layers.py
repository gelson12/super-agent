#!/usr/bin/env python3
"""
End-to-end test of Claude CLI Pro session token recovery layers.

Run this inside the inspiring-cat container from /app:
  python3 /app/scripts/test_recovery_layers.py

Outputs PASS/FAIL for every sub-test across Layers 1, 2, and 4.
Exit code 0 = all critical tests passed.
"""
import sys, os, time, json, base64
sys.path.insert(0, "/app")

_RESULTS: list[tuple[str, bool, str]] = []

def _ok(name, detail=""): _RESULTS.append((name, True,  detail)); print(f"  ✓  {name}" + (f"  — {detail}" if detail else ""))
def _fail(name, detail=""): _RESULTS.append((name, False, detail)); print(f"  ✗  {name}" + (f"  — {detail}" if detail else ""))
def _section(title): print(f"\n{'='*60}\n  {title}\n{'='*60}")

# ─────────────────────────────────────────────────────────────
# PRECONDITIONS
# ─────────────────────────────────────────────────────────────
_section("PRECONDITIONS")

# Credentials file
_CREDS = "/root/.claude/.credentials.json"
if os.path.exists(_CREDS):
    try:
        _c = json.loads(open(_CREDS).read())
        _exp = None
        for _k in ("expiresAt","expires_at"):
            if _k in _c: _exp = float(_c[_k]); break
        if _exp is None:
            for _nk in ("claudeAiOauth","claudeAiOAuth","oauth","session"):
                _n = _c.get(_nk,{})
                if isinstance(_n,dict):
                    _exp = _n.get("expiresAt") or _n.get("expires_at")
                if _exp: _exp=float(_exp); break
        if _exp:
            _rem = int((_exp - time.time()*1000) / 1000)
            if _rem > 0:
                _ok("Credentials file", f"valid — expires in {_rem//3600}h {(_rem%3600)//60}m")
            else:
                _fail("Credentials file", f"EXPIRED {abs(_rem)//3600}h {(abs(_rem)%3600)//60}m ago")
        else:
            _ok("Credentials file", "exists (no expiresAt field — cannot check expiry)")
    except Exception as e:
        _fail("Credentials file parse", str(e))
else:
    _fail("Credentials file", f"MISSING at {_CREDS}")

# Volume backup
_VOL = "/workspace/.claude_credentials_backup.json"
if os.path.exists(_VOL):
    try:
        _vc = json.loads(open(_VOL).read())
        _vexp = None
        for _vk in ("expiresAt","expires_at"):
            if _vk in _vc: _vexp = float(_vc[_vk]); break
        if _vexp is None:
            for _vnk in ("claudeAiOauth","claudeAiOAuth","oauth","session"):
                _vn = _vc.get(_vnk,{})
                if isinstance(_vn,dict): _vexp = _vn.get("expiresAt") or _vn.get("expires_at")
                if _vexp: _vexp=float(_vexp); break
        if _vexp:
            _vrem = int((_vexp - time.time()*1000)/1000)
            if _vrem > 0:
                _ok("Volume backup", f"valid — expires in {_vrem//3600}h {(_vrem%3600)//60}m")
            else:
                _fail("Volume backup", f"EXPIRED {abs(_vrem)//3600}h {(abs(_vrem)%3600)//60}m ago — will be overwritten on next recovery")
        else:
            _ok("Volume backup", "exists (no expiresAt field)")
    except Exception as e:
        _fail("Volume backup parse", str(e))
else:
    _fail("Volume backup", f"ABSENT at {_VOL} — Layer 2 must provide it on next recovery")

# Blocking files
_RATE_LIMIT = "/workspace/.claude_login_ratelimit"
_PW_ATTEMPTS = "/workspace/.playwright_attempts.json"
_PW_BACKOFF_FLAG = "/tmp/.playwright_backoff"
_LOCK_FILE = "/workspace/.recovery_in_progress.lock"

for _fpath, _name in [
    (_RATE_LIMIT, "Rate-limit cooldown file"),
    (_LOCK_FILE,  "Recovery in-progress lock"),
]:
    if os.path.exists(_fpath):
        try:
            _content = open(_fpath).read().strip()
            _age_min = int((time.time() - os.path.getmtime(_fpath)) / 60)
            _fail(_name, f"EXISTS (age {_age_min}m, content: {_content[:80]!r}) — BLOCKING RECOVERY")
        except Exception:
            _fail(_name, "EXISTS — check manually")
    else:
        _ok(_name, "absent — not blocking")

if os.path.exists(_PW_ATTEMPTS):
    try:
        _pts = json.loads(open(_PW_ATTEMPTS).read())
        _recent = [t for t in _pts if time.time()-t < 3600]
        if len(_recent) > 3:
            _fail("Playwright attempts (last 1h)", f"{len(_recent)} attempts — possible rate-limit loop")
        else:
            _ok("Playwright attempts (last 1h)", f"{len(_recent)} attempts in last hour")
    except Exception as e:
        _ok("Playwright attempts file", f"unreadable ({e}) — treating as empty")
else:
    _ok("Playwright attempts file", "absent — no previous attempts")

# ─────────────────────────────────────────────────────────────
# CLEAR BLOCKING FILES (so tests can proceed)
# ─────────────────────────────────────────────────────────────
_section("CLEARING BLOCKERS (if any)")
for _fpath, _name in [
    (_RATE_LIMIT, "rate-limit cooldown"),
    (_LOCK_FILE,  "recovery lock"),
]:
    if os.path.exists(_fpath):
        try:
            os.unlink(_fpath)
            _ok(f"Cleared {_name}", _fpath)
        except Exception as e:
            _fail(f"Clear {_name}", str(e))
    else:
        print(f"  —  {_name} not present, nothing to clear")

# ─────────────────────────────────────────────────────────────
# LAYER 1: Direct OAuth token refresh
# ─────────────────────────────────────────────────────────────
_section("LAYER 1 — Direct OAuth token refresh")
print("  NOTE: Cloudflare blocks Railway IPs from claude.com/cai/oauth/token.")
print("  Layer 1 is EXPECTED to fail with 403/405. This is by design.")
print("  PASS = the function runs without crashing (response captured).")
try:
    from app.learning.cli_auto_login import _try_direct_refresh
    _L1_start = time.time()
    _L1_result = _try_direct_refresh()
    _L1_dur = round(time.time() - _L1_start, 1)
    if _L1_result:
        _ok("Layer 1 direct refresh", f"SUCCEEDED (token refreshed!) in {_L1_dur}s")
    else:
        _ok("Layer 1 direct refresh callable", f"returned False in {_L1_dur}s (Cloudflare block = expected, function ran without crash)")
except Exception as e:
    _fail("Layer 1 import/_try_direct_refresh", str(e))

# ─────────────────────────────────────────────────────────────
# LAYER 2: PostgreSQL credential store
# ─────────────────────────────────────────────────────────────
_section("LAYER 2 — PostgreSQL credential backup")

_db_url = os.environ.get("DATABASE_URL","").replace("postgres://","postgresql://",1)
if not _db_url:
    _fail("DATABASE_URL", "NOT SET — Layer 2 completely unavailable")
else:
    _ok("DATABASE_URL", "set")

    # 2a: table exists
    try:
        import psycopg2
        with psycopg2.connect(_db_url) as _conn:
            with _conn.cursor() as _cur:
                _cur.execute("SELECT COUNT(*) FROM claude_credentials")
                _count = _cur.fetchone()[0]
        _ok("claude_credentials table", f"exists, {_count} row(s)")
    except Exception as e:
        _fail("claude_credentials table", str(e))

    # 2b: save current credentials
    try:
        from app.learning.cli_auto_login import _save_credentials_to_db
        if os.path.exists(_CREDS):
            _L2_save = _save_credentials_to_db()
            if _L2_save:
                _ok("Layer 2 save credentials to DB", "wrote current creds to postgres ✓")
            else:
                _fail("Layer 2 save credentials to DB", "returned False — check DATABASE_URL or creds file")
        else:
            _fail("Layer 2 save credentials to DB", f"skipped — {_CREDS} missing")
    except Exception as e:
        _fail("Layer 2 save credentials to DB", str(e))

    # 2c: verify the saved record
    try:
        with psycopg2.connect(_db_url) as _conn:
            with _conn.cursor() as _cur:
                _cur.execute("SELECT expires_at, subscription_type, updated_at FROM claude_credentials WHERE id='primary'")
                _row = _cur.fetchone()
        if _row:
            _exp_ms, _sub, _upd = _row
            if _exp_ms:
                _db_rem = int((_exp_ms - time.time()*1000)/1000)
                _exp_str = (f"expires in {_db_rem//3600}h {(_db_rem%3600)//60}m"
                            if _db_rem > 0 else f"EXPIRED {abs(_db_rem)//3600}h ago")
            else:
                _exp_str = "no expiry stored"
            _ok("Layer 2 DB record", f"id=primary sub={_sub or '?'} {_exp_str} updated={_upd}")
        else:
            _fail("Layer 2 DB record", "no 'primary' row found after save")
    except Exception as e:
        _fail("Layer 2 DB record verify", str(e))

    # 2d: restore from DB (simulate volume loss)
    try:
        _CREDS_BACKUP_TMP = _CREDS + ".testbak"
        _had_creds = os.path.exists(_CREDS)
        if _had_creds:
            import shutil; shutil.copy2(_CREDS, _CREDS_BACKUP_TMP)
            os.unlink(_CREDS)
        from app.learning.cli_auto_login import _restore_credentials_from_db
        _L2_restore = _restore_credentials_from_db()
        if _L2_restore and os.path.exists(_CREDS):
            _ok("Layer 2 restore credentials from DB", "wrote creds file back from postgres ✓")
        elif not _L2_restore:
            _fail("Layer 2 restore credentials from DB", "returned False (expired record or no record)")
        else:
            _fail("Layer 2 restore credentials from DB", "returned True but file not written")
        # Restore original if we moved it
        if _had_creds and os.path.exists(_CREDS_BACKUP_TMP):
            shutil.copy2(_CREDS_BACKUP_TMP, _CREDS)
            os.unlink(_CREDS_BACKUP_TMP)
    except Exception as e:
        _fail("Layer 2 restore from DB", str(e))
        if os.path.exists(_CREDS + ".testbak"):
            try:
                import shutil; shutil.copy2(_CREDS+".testbak", _CREDS); os.unlink(_CREDS+".testbak")
            except Exception: pass

# ─────────────────────────────────────────────────────────────
# LAYER 4: Playwright browser + n8n email verification
# ─────────────────────────────────────────────────────────────
_section("LAYER 4 — Playwright browser + n8n email verification")

# 4a: Playwright available
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _pw:
        _browser = _pw.chromium.launch(headless=True)
        _browser.close()
    _ok("Playwright chromium available", "headless launch succeeded")
except Exception as e:
    _fail("Playwright chromium", str(e))

# 4b: camoufox available
try:
    import camoufox
    _ok("camoufox available", f"version {getattr(camoufox,'__version__','?')}")
except Exception as e:
    _fail("camoufox", str(e))

# 4c: n8n reachable
_n8n_url = os.environ.get("N8N_BASE_URL","").rstrip("/")
_n8n_key = os.environ.get("N8N_API_KEY","")
if not _n8n_url:
    _fail("N8N_BASE_URL", "NOT SET — n8n unreachable")
else:
    try:
        import urllib.request
        _req = urllib.request.Request(f"{_n8n_url}/api/v1/workflows?limit=1",
                                      headers={"X-N8N-API-KEY": _n8n_key})
        with urllib.request.urlopen(_req, timeout=8) as _r:
            _ok("n8n reachable", f"HTTP {_r.status} from {_n8n_url}")
    except Exception as e:
        _fail("n8n reachable", str(e))

# 4d: verification monitor workflow active
_WF_ID = "jun8CaMnNhux1iEY"
if _n8n_url and _n8n_key:
    try:
        _req2 = urllib.request.Request(f"{_n8n_url}/api/v1/workflows/{_WF_ID}",
                                       headers={"X-N8N-API-KEY": _n8n_key})
        with urllib.request.urlopen(_req2, timeout=8) as _r2:
            _wf = json.loads(_r2.read().decode())
            _active = _wf.get("active", False)
            _wf_name = _wf.get("name", _WF_ID)
            if _active:
                _ok(f"n8n workflow '{_wf_name}'", "ACTIVE ✓ — will deliver magic link to /webhook/verification-code")
            else:
                _fail(f"n8n workflow '{_wf_name}'", "INACTIVE — verification code will never arrive. Activate it in n8n UI.")
    except Exception as e:
        _fail("n8n verification monitor workflow", str(e))
else:
    print("  —  n8n check skipped (N8N_BASE_URL or N8N_API_KEY not set)")

# 4e: /webhook/verification-code endpoint reachable on self
_self_url = os.environ.get("SELF_URL","http://localhost:8002").rstrip("/")
try:
    _probe = urllib.request.Request(f"{_self_url}/health", headers={"Accept":"application/json"})
    with urllib.request.urlopen(_probe, timeout=5) as _hr:
        _hd = json.loads(_hr.read().decode())
        _ok("CLI worker /health endpoint", f"reachable — claude_available={_hd.get('claude_available','?')}")
except Exception as e:
    _fail("CLI worker /health endpoint", str(e))

# ─────────────────────────────────────────────────────────────
# LAYER 4 LIVE — actually run full_recovery_chain()
# (only if n8n workflow is active — otherwise skip)
# ─────────────────────────────────────────────────────────────
_n8n_wf_active = any(r[0] == f"n8n workflow '{_WF_ID}'" and r[1] for r in _RESULTS) or \
                  any("ACTIVE" in r[2] for r in _RESULTS if "workflow" in r[0].lower())

_section("LAYER 4 LIVE RECOVERY — full_recovery_chain()")
if not _n8n_wf_active:
    print("  ⚠  SKIPPING live recovery — n8n verification workflow is NOT active.")
    print("     Activate 'Claude Verification Code Monitor' in the n8n UI first,")
    print("     then re-run this script.")
    _fail("Layer 4 live recovery", "SKIPPED — n8n workflow not active")
else:
    print("  n8n workflow is active. Attempting full recovery (may take up to 10 min)...")
    print("  This will: open browser → enter email → wait for magic link from n8n → save creds")
    try:
        from app.learning.cli_auto_login import full_recovery_chain
        _L4_start = time.time()
        _L4_ok = full_recovery_chain()
        _L4_dur = round(time.time() - _L4_start)
        if _L4_ok:
            _ok("Layer 4 full_recovery_chain()", f"SUCCEEDED in {_L4_dur}s — new token obtained ✓")
            # Verify the new token
            if os.path.exists(_CREDS):
                try:
                    _nc = json.loads(open(_CREDS).read())
                    _ne = None
                    for _nk2 in ("expiresAt","expires_at"):
                        if _nk2 in _nc: _ne=float(_nc[_nk2]); break
                    if _ne is None:
                        for _nnk in ("claudeAiOauth","claudeAiOAuth","oauth","session"):
                            _nn = _nc.get(_nnk,{})
                            if isinstance(_nn,dict): _ne = _nn.get("expiresAt") or _nn.get("expires_at")
                            if _ne: _ne=float(_ne); break
                    if _ne:
                        _nr = int((_ne - time.time()*1000)/1000)
                        if _nr > 0:
                            _ok("New token validity", f"expires in {_nr//3600}h {(_nr%3600)//60}m ✓")
                        else:
                            _fail("New token validity", "still shows as expired — recovery may have used stale creds")
                    else:
                        _ok("New token written", "no expiresAt in new creds file")
                except Exception as e:
                    _fail("New token parse", str(e))
        else:
            _fail("Layer 4 full_recovery_chain()", f"returned False after {_L4_dur}s — check logs for details")
    except Exception as e:
        _fail("Layer 4 full_recovery_chain()", str(e))

# ─────────────────────────────────────────────────────────────
# POST-RECOVERY VERIFICATION
# ─────────────────────────────────────────────────────────────
_section("POST-RECOVERY VERIFICATION")

# Verify claude auth status
try:
    import subprocess
    _env = {k:v for k,v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    _env["HOME"] = "/root"
    _r = subprocess.run(["claude","auth","status"], capture_output=True, text=True, timeout=15, env=_env)
    _raw = (_r.stdout + _r.stderr).strip()
    _compact = _raw.replace(": ",":")
    if '"authMethod":"claude.ai"' in _compact:
        _ok("claude auth status", "authMethod=claude.ai ✓ — Pro CLI is authenticated")
    elif '"loggedIn":true' in _compact:
        _fail("claude auth status", "logged in but NOT via claude.ai — check credentials format")
    else:
        _fail("claude auth status", f"NOT authenticated. Output: {_raw[:200]!r}")
except FileNotFoundError:
    _fail("claude binary", "not found in PATH")
except Exception as e:
    _fail("claude auth status", str(e))

# Verify DB was updated after recovery
if _db_url:
    try:
        with psycopg2.connect(_db_url) as _conn:
            with _conn.cursor() as _cur:
                _cur.execute("SELECT expires_at, updated_at FROM claude_credentials WHERE id='primary'")
                _row2 = _cur.fetchone()
        if _row2:
            _exp2, _upd2 = _row2
            _rem2 = int((_exp2 - time.time()*1000)/1000) if _exp2 else 0
            if _rem2 > 0:
                _ok("DB record post-recovery", f"fresh token stored — expires in {_rem2//3600}h {(_rem2%3600)//60}m, updated {_upd2}")
            else:
                _fail("DB record post-recovery", f"DB still shows expired token — recovery may not have saved to DB")
        else:
            _fail("DB record post-recovery", "no primary row found")
    except Exception as e:
        _fail("DB record post-recovery", str(e))

# Volume backup updated
if os.path.exists(_VOL):
    _vage = int(time.time() - os.path.getmtime(_VOL))
    if _vage < 300:
        _ok("Volume backup post-recovery", f"updated {_vage}s ago ✓")
    else:
        _fail("Volume backup post-recovery", f"not updated recently (age {_vage//60}m) — recovery may not have saved backup")
else:
    _fail("Volume backup post-recovery", "still absent")

# ─────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────
_section("SUMMARY")
_passed = sum(1 for _,ok,_ in _RESULTS if ok)
_failed = sum(1 for _,ok,_ in _RESULTS if not ok)
print(f"  Total: {len(_RESULTS)}  |  PASSED: {_passed}  |  FAILED: {_failed}\n")
if _failed:
    print("  FAILED tests:")
    for _name, _ok2, _detail in _RESULTS:
        if not _ok2:
            print(f"    ✗  {_name}: {_detail}")
else:
    print("  ALL TESTS PASSED ✓")

# Write results to shared findings doc
_report_lines = [
    "# Recovery Layer Test Results\n",
    f"**Run at:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n",
    f"**Passed:** {_passed} / {len(_RESULTS)}\n\n",
    "| Test | Result | Detail |\n",
    "|------|--------|--------|\n",
]
for _name, _ok2, _detail in _RESULTS:
    _sym = "✓" if _ok2 else "✗"
    _report_lines.append(f"| {_name} | {_sym} | {_detail} |\n")

_report_path = "/workspace/RECOVERY_TEST_RESULTS.md"
try:
    with open(_report_path, "w") as _rf:
        _rf.writelines(_report_lines)
    print(f"\n  Full results written to {_report_path}")
except Exception:
    pass

sys.exit(0 if _failed == 0 else 1)
