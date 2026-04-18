import os,sys,pty,select,time,re,subprocess,shutil
OAUTH_URL_FILE="/tmp/login_oauth_url.txt"
AUTH_CODE_FILE="/tmp/login_auth_code.txt"
LOG_FILE="/tmp/login_standalone.log"
CREDS_FILE="/root/.claude/credentials.json"
CREDS_BACKUP="/tmp/credentials_backup.json"

def log(msg):
    ts=time.strftime("%H:%M:%S")
    line=f"[{ts}] {msg}"
    with open(LOG_FILE,"a") as f: f.write(line+"\n")
    print(line,flush=True)

for f in [OAUTH_URL_FILE,AUTH_CODE_FILE]:
    try: os.remove(f)
    except: pass

# CRITICAL: delete expired credentials so claude login starts fresh OAuth flow
if os.path.exists(CREDS_FILE):
    shutil.copy2(CREDS_FILE,CREDS_BACKUP)
    os.remove(CREDS_FILE)
    log("Deleted expired credentials (backup saved)")

env={k:v for k,v in os.environ.items() if k!="ANTHROPIC_API_KEY"}
env["HOME"]="/root"
# Write a browser capture script — if claude login tries to auto-open a browser,
# this captures the full URL. BROWSER env var is checked by the 'open' npm package.
_browser_script="/tmp/capture_browser.sh"
with open(_browser_script,"w") as _bf:
    _bf.write('#!/bin/bash\necho "$1" > /tmp/login_oauth_url.txt\n')
os.chmod(_browser_script,0o755)
env["BROWSER"]=_browser_script
# Also try xdg-open override
_xdg_dir="/tmp/xdg_bin"
os.makedirs(_xdg_dir,exist_ok=True)
with open(f"{_xdg_dir}/xdg-open","w") as _xf:
    _xf.write('#!/bin/bash\necho "$1" > /tmp/login_oauth_url.txt\n')
os.chmod(f"{_xdg_dir}/xdg-open",0o755)
env["PATH"]=f"{_xdg_dir}:{env.get('PATH','')}"

log("Starting claude login PTY...")
master_fd,slave_fd=pty.openpty()
proc=subprocess.Popen(["claude","login"],stdin=slave_fd,stdout=slave_fd,stderr=slave_fd,env=env,close_fds=True)
os.close(slave_fd)
log(f"PTY PID={proc.pid}")

def _clean_pty(raw):
    # Extract URLs from OSC 8 hyperlinks: \x1b]8;;URL\x07TEXT\x1b]8;;\x07
    s=re.sub(r'\x1b\]8;;([^\x07\x1b]*)\x07[^\x1b]*\x1b\]8;;\x07',r' \1 ',raw,flags=re.DOTALL)
    s=re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]','',s)
    s=re.sub(r'\x1b\][^\x07]*\x07','',s)
    s=s.replace('\r','')
    return s

URL_PATTERNS=[
    re.compile(r'https://claude\.com/cai/oauth/authorize\?[^\s\r\n\x1b"\'<> ]+',re.I),
    re.compile(r'https://claude\.com/[^\s\r\n\x1b"\'<> ]*oauth[^\s\r\n\x1b"\'<> ]+',re.I),
    re.compile(r'https://claude\.ai/oauth/authorize\?[^\s\r\n\x1b"\'<> ]+',re.I),
    re.compile(r'https://claude\.ai/[^\s\r\n\x1b"\'<> ]*oauth[^\s\r\n\x1b"\'<> ]+',re.I),
    re.compile(r'https://[^\s\r\n\x1b"\'<> ]{30,}',re.I),
]

def _find_url(text):
    for pat in URL_PATTERNS:
        m=pat.search(text)
        if m:
            c=m.group(0).rstrip(".,;)")
            if "?" in c and ("oauth" in c.lower() or "client_id" in c.lower() or "redirect_uri" in c.lower()):
                return c
    return None

def _find_url_unwrapped(raw):
    """Remove terminal line-wrapping then search for URL.
    Strategy 2 runs first: strips ALL control chars (including ESC from TUI cursor
    movement) to get the full URL. Strategy 1 is a fallback."""
    # Strategy 2 FIRST: strip ALL control chars (\r\r\n and \x1b sequences)
    # The TUI inserts ESC cursor-movement sequences mid-URL; we must strip them.
    m=re.search(r'https://claude\.com/cai/oauth/authorize',raw,re.I)
    if m:
        segment=raw[m.start():m.start()+1500]  # 1500b > URL + all \r\r\n overhead
        clean=re.sub(r'[\x00-\x1f\x7f]+','',segment)
        u=_find_url(clean)
        if u and len(u)>200:  # Full URL >> 200b; reject truncated versions
            # Debug: show what char comes right after the matched URL in clean
            pos=clean.find(u)
            after=clean[pos+len(u):pos+len(u)+20] if pos>=0 else '?'
            log(f"Strat2 URL ({len(u)}b) ends at: {repr(after[:20])}")
            return u
    # Strategy 1 fallback: remove only \r\n line-wrapping
    u1=_find_url(raw.replace('\r\n','').replace('\r',''))
    if u1: return u1
    return None

ENTER=b"\r"
ONBOARDING=[
    ("WelcometoClaude",ENTER),
    ("Choosethetextstyle",ENTER),
    ("Choosethesyntaxtheme",ENTER),
    ("Syntaxtheme:",ENTER),
    ("Selectloginmethod:",ENTER),
    ("1Claudeaccount",ENTER),
    ("Pressanykeyto",ENTER),
    ("pressEnterto",ENTER),
    ("Tologincontinue",ENTER),
]

last_resp=0.0
fired=set()

def maybe_respond(clean_nospace):
    global last_resp
    now=time.time()
    if now-last_resp<2.0:
        return False
    for marker,resp in ONBOARDING:
        if marker in clean_nospace and marker not in fired:
            fired.add(marker)
            log(f"Onboarding: {marker!r} -> Enter")
            try:
                os.write(master_fd,resp)
                last_resp=now
            except: pass
            return True
    return False

accumulated=""
accumulated_raw=""
oauth_url=None
deadline=time.time()+180  # 3 minutes for full onboarding + URL

while time.time()<deadline:
    # Check if BROWSER script already captured the full URL
    if os.path.exists(OAUTH_URL_FILE):
        with open(OAUTH_URL_FILE) as _f: _browser_url=_f.read().strip()
        if _browser_url and len(_browser_url)>200 and "?" in _browser_url:
            oauth_url=_browser_url
            log(f"BROWSER captured URL ({len(oauth_url)}b)")
            break
    if proc.poll() is not None:
        log(f"claude login exited rc={proc.poll()} before URL captured")
        log(f"Last output: {accumulated[-400:]!r}")
        break
    r,_,_=select.select([master_fd],[],[],0.3)
    if r:
        try:
            chunk=os.read(master_fd,4096)
            raw=chunk.decode("utf-8",errors="replace")
            accumulated_raw+=raw
            clean=_clean_pty(raw)
            accumulated+=clean
            nospace=re.sub(r'\s+','',accumulated[-2000:])
            maybe_respond(nospace)
            # Try 3 paths: raw unwrapped first (full URL), then cleaned, then raw
            u=(_find_url_unwrapped(accumulated_raw) or
               _find_url(accumulated) or
               _find_url(accumulated_raw))
            if u:
                oauth_url=u
                log(f"OAuth URL ({len(u)}b): {oauth_url[:200]}")
                # Debug: show raw bytes around URL to understand split format
                m=re.search(r'https://claude\.com/cai/oauth',accumulated_raw,re.I)
                if m:
                    segment=accumulated_raw[m.start():m.start()+400]
                    log(f"RAW segment: {repr(segment[:200])}")
                break
        except OSError:
            break
    else:
        # Idle: re-check onboarding with last accumulated
        nospace=re.sub(r'\s+','',accumulated[-2000:])
        maybe_respond(nospace)

if not oauth_url:
    log(f"FAILED: No URL in 3 min. Output: {accumulated[-600:]!r}")
    log(f"Raw tail: {accumulated_raw[-400:]!r}")
    if os.path.exists(CREDS_BACKUP):
        shutil.copy2(CREDS_BACKUP,CREDS_FILE)
    proc.kill()
    sys.exit(1)

with open(OAUTH_URL_FILE,"w") as f: f.write(oauth_url)
log("URL written to /tmp/login_oauth_url.txt")
log("=== READY: open URL in incognito, authorize, write code to /tmp/login_auth_code.txt ===")

# Wait up to 600s for auth code file
deadline2=time.time()+600
auth_code=None
while time.time()<deadline2:
    if os.path.exists(AUTH_CODE_FILE):
        with open(AUTH_CODE_FILE) as f: auth_code=f.read().strip()
        if auth_code:
            log(f"Code: {auth_code[:25]}...")
            break
    r,_,_=select.select([master_fd],[],[],1.0)
    if r:
        try: os.read(master_fd,4096)
        except: pass
    if proc.poll() is not None:
        log(f"claude login exited early rc={proc.poll()}")
        break

if not auth_code:
    log("TIMEOUT")
    if os.path.exists(CREDS_BACKUP): shutil.copy2(CREDS_BACKUP,CREDS_FILE)
    proc.kill()
    sys.exit(1)

# Drain before writing
for _ in range(30):
    r,_,_=select.select([master_fd],[],[],0.05)
    if not r: break
    try: os.read(master_fd,4096)
    except: break

log("Writing code to PTY...")
os.write(master_fd,(auth_code+"\r").encode())

creds_before=os.path.getmtime(CREDS_FILE) if os.path.exists(CREDS_FILE) else 0
deadline3=time.time()+120
while time.time()<deadline3:
    r,_,_=select.select([master_fd],[],[],0.5)
    if r:
        try:
            out=os.read(master_fd,4096).decode("utf-8",errors="replace")
            if out.strip(): log(f"PTY: {repr(out.strip()[-150:])}")
        except: pass
    if proc.poll() is not None:
        log(f"CLI exited rc={proc.poll()}")
        break
    try:
        if os.path.exists(CREDS_FILE) and os.path.getmtime(CREDS_FILE)>creds_before:
            log("Credentials updated!")
            proc.terminate()
            break
    except: pass
    try: os.write(master_fd,b"\r")
    except: pass
    time.sleep(1)

creds_after=os.path.getmtime(CREDS_FILE) if os.path.exists(CREDS_FILE) else 0
if creds_after>creds_before:
    log("=== SUCCESS ===")
    try: os.remove(CREDS_BACKUP)
    except: pass
else:
    log("=== FAILED: credentials not updated ===")
    if os.path.exists(CREDS_BACKUP) and not os.path.exists(CREDS_FILE):
        shutil.copy2(CREDS_BACKUP,CREDS_FILE)
try: os.close(master_fd)
except: pass
