"""
L5: DevBrowser-CDP — code-first Chrome DevTools Protocol recovery.

Unlike L4 (which relies on Playwright's launcher), L5 spawns chromium as
its own subprocess with --remote-debugging-port=9222, then connects via
Playwright's connect_over_cdp. This sidesteps launcher bugs that sometimes
break L4 — the failure surface changes from "Playwright's launcher" to
"can we open a TCP port". If even that fails we surface `cdp_*` errors and
chain.py flips the account to EXHAUSTED.

Runs only when L4 returned a *recoverable* failure signature. Terminal
signatures (account_locked, invalid_credentials) cause chain.py to skip
L5 and finalise LOCKED.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import httpx

from app.healing.diagnostics import DiagnosticBundle
from app.healing.l4_playwright import (
    CREDS_PATH,
    PAGE_TIMEOUT_MS,
    FailureSignature,
    L4Result,
    _captcha_present,
    _text_indicates_lockout,
)
from app.verification_queue import get as queue_get

log = logging.getLogger("legion.healing.l5")

CDP_PORT = int(os.environ.get("LEGION_CDP_PORT", "9222"))
CDP_PROFILE_DIR = Path("/workspace/legion/cdp-profile-b")
MAGIC_LINK_TIMEOUT_S = 240


async def _wait_for_cdp_port(port: int, deadline_s: float = 10.0) -> bool:
    ends = time.monotonic() + deadline_s
    while time.monotonic() < ends:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"http://127.0.0.1:{port}/json/version", timeout=0.5)
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False


async def login_via_cdp(account_email: str, l4_signature: str | None, account_id: str = "B") -> L4Result:
    if not account_email:
        return L4Result(False, "no_account_email")

    start = time.monotonic()
    diag = DiagnosticBundle(account=account_id)
    chromium_proc: subprocess.Popen | None = None

    try:
        from playwright.async_api import TimeoutError as PWTimeout
        from playwright.async_api import async_playwright
    except ImportError:
        return L4Result(False, "playwright_not_installed")

    try:
        # Find Playwright's bundled chromium binary — stable across images
        # built by our Dockerfile (`playwright install --with-deps chromium`).
        async with async_playwright() as pw_probe:
            chrome_exec = pw_probe.chromium.executable_path

        CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        chromium_proc = subprocess.Popen(
            [
                chrome_exec,
                "--headless=new",
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={CDP_PROFILE_DIR}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if not await _wait_for_cdp_port(CDP_PORT):
            diag.record_layer("cdp_bind", time.monotonic() - start, "cdp_port_unreachable")
            return L4Result(False, "cdp_port_unreachable", diag)

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{CDP_PORT}"
                )
            except Exception as exc:
                diag.record_layer("cdp_connect", time.monotonic() - start, type(exc).__name__)
                return L4Result(False, f"cdp_connect_{type(exc).__name__}", diag)

            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                try:
                    await page.goto("https://claude.ai/login", timeout=PAGE_TIMEOUT_MS)
                except PWTimeout:
                    diag.record_layer("goto", time.monotonic() - start, FailureSignature.TIMEOUT)
                    return L4Result(False, FailureSignature.TIMEOUT, diag)

                if await _captcha_present(page):
                    diag.record_layer("captcha", time.monotonic() - start, FailureSignature.CAPTCHA)
                    return L4Result(False, FailureSignature.CAPTCHA, diag)

                email_field = page.locator(
                    '[data-testid="login-email"], input[type="email"]'
                ).first
                try:
                    await email_field.wait_for(timeout=PAGE_TIMEOUT_MS)
                    await email_field.fill(account_email)
                except PWTimeout:
                    diag.record_layer("email_field", time.monotonic() - start,
                                      FailureSignature.SELECTOR_NOT_FOUND)
                    return L4Result(False, FailureSignature.SELECTOR_NOT_FOUND, diag)

                submit = page.locator(
                    'button[type="submit"], button:has-text("Continue with email")'
                ).first
                await submit.click(timeout=PAGE_TIMEOUT_MS)

                msg = await queue_get(timeout_s=MAGIC_LINK_TIMEOUT_S)
                if msg is None:
                    diag.record_layer("wait_magic_link", time.monotonic() - start,
                                      FailureSignature.TIMEOUT)
                    return L4Result(False, FailureSignature.TIMEOUT, diag)

                if msg.code:
                    code_field = page.locator(
                        'input[name="verification_code"], input[autocomplete="one-time-code"]'
                    ).first
                    try:
                        await code_field.wait_for(timeout=PAGE_TIMEOUT_MS)
                        await code_field.fill(msg.code)
                    except PWTimeout:
                        diag.record_layer("code_field", time.monotonic() - start,
                                          FailureSignature.SELECTOR_NOT_FOUND)
                        return L4Result(False, FailureSignature.SELECTOR_NOT_FOUND, diag)
                elif msg.url:
                    await page.goto(msg.url, timeout=PAGE_TIMEOUT_MS)

                await asyncio.sleep(2)
                lockout = _text_indicates_lockout(await page.content())
                if lockout:
                    diag.record_layer("post_submit", time.monotonic() - start, lockout)
                    return L4Result(False, lockout, diag)

                storage = await context.storage_state()
                CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
                CREDS_PATH.write_text(json.dumps(storage, indent=2))
                CREDS_PATH.chmod(0o600)
                diag.record_layer("complete", time.monotonic() - start, None)
                return L4Result(True, None, diag)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except Exception as exc:
        log.warning("L5 unexpected: %s", type(exc).__name__)
        return L4Result(False, f"l5_{type(exc).__name__}", diag)
    finally:
        if chromium_proc is not None and chromium_proc.poll() is None:
            chromium_proc.terminate()
            try:
                chromium_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chromium_proc.kill()
