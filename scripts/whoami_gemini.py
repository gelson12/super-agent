"""
Decode the email/sub from /root/.gemini/oauth_creds.json's id_token.
Read-only diagnostic — prints only the public claims (email, name, sub).
Run inside the inspiring-cat container:
    railway ssh --service VS-Code-inspiring-cat -- python3 /app/scripts/whoami_gemini.py
"""
import base64
import json
import os
import sys


def main() -> int:
    paths = [
        "/root/.gemini/oauth_creds.json",
        "/root/.config/gemini/oauth_creds.json",
        "/root/.gemini/credentials.json",
        "/root/.gemini/auth.json",
    ]
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                d = json.load(f)
        except Exception as e:
            print(f"{p}: read error {type(e).__name__}")
            continue
        tok = d.get("id_token") or d.get("idToken") or ""
        if not tok or "." not in tok:
            print(f"{p}: no id_token. top-level keys = {list(d.keys())}")
            continue
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
        except Exception as e:
            print(f"{p}: decode error {type(e).__name__}")
            continue
        print(f"--- {p} ---")
        for k in ("email", "name", "given_name", "family_name", "sub", "iss", "aud"):
            if k in claims:
                print(f"  {k}: {claims[k]}")
        return 0
    print("No gemini credential file found at any of:", paths)
    return 1


sys.exit(main())
