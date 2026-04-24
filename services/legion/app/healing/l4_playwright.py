"""
L4: Playwright magic-link login for Claude Account B.

Flow
  1. Launch headless chromium via Playwright's normal launcher.
  2. Navigate to claude.ai/login.
  3. Fill email, submit, wait for "magic link sent" confirmation.
  4. Wait up to MAGIC_LINK_TIMEOUT_S on the verification-code queue
     (n8n workflow POSTs to /webhook/verification-code, which enqueues).
  5. Apply the code or follow the URL.
  6. Extract storage_state from the authenticated context, write to
     /workspace/legion/claude-b/credentials.json.

Classifies failures into `FailureSignature` so chain.py can decide whether
to escalate to L5 (DevBrowser-CDP) or stop.

Selectors are best-effort and will drift as claude.ai's UI changes; they
match the patterns used by inspiring-cat's existing L4 implementation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app.healing.diagnostics import DiagnosticBundle
from app.verification_queue import get as queue_get

log = logging.getLogger("legion.healing.l4")

CREDS_PATH = Path("/workspace/legion/claude-b/credentials.json")
MAGIC_LINK_TIMEOUT_S = 240
PAGE_TIMEOUT_MS = 30_000


class FailureSignature:
    TIMEOUT = "playwright_timeout"
    SELECTOR_NOT_FOUND = "selector_not_found"
    NETWORK = "network_error"
    CAPTCHA = "captcha_detected"
    LOCKED = "account_locked"
    INVALID_CREDENTIALS = "invalid_credentials"
    LAUNCHER_FAILED = "launcher_failed"
    UNKNOWN = "unknown"


# Signatures for which L5 retry would not help. chain.py skips L5 on these.
TERMINAL_SIGNATURES = frozenset({
    FailureSignature.LOCKED,
    FailureSignature.INVALID_CREDENTIALS,
})


@dataclass
class L4Result:
    success: bool
    failure_signature: str | None = None
    diag: DiagnosticBundle | None = None


async def _captcha_present(page) -> bool:
    try:
        for f in page.frames:
            url = f.url or ""
            if ("challenges.cloudflare.com" in url
                    or "turnstile" in url.lower()
                    or "recaptcha" in url.lower()):
                return True
    except Exception:
        pass
    return False


def _classify_goto_error(exc: BaseException) -> str:
    msg = str(exc)
    if "net::ERR_" in msg or "NS_ERROR_" in msg:
        return FailureSignature.NETWORK
    return FailureSignature.UNKNOWN


def _text_indicates_lockout(html: str) -> str | None:
    lower = html.lower()
    if "account locked" in lower or "account has been locked" in lower:
        return FailureSignature.LOCKED
    if "invalid" in lower and ("email" in lower or "credentials" in lower):
        return FailureSignature.INVALID_CREDENTIALS
    return None


async def login_via_playwright(account_email: str, account_id: str = "B") -> L4Result:
    if not account_email:
        return L4Result(False, "no_account_email")

    start = time.monotonic()
    diag = DiagnosticBundle(account=account_id)

    try:
        from playwright.async_api import TimeoutError as PWTimeout
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("L4: playwright not installed")
        return L4Result(False, "playwright_not_installed")

    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except Exception as exc:
                log.warning("L4: launcher failed: %s", type(exc).__name__)
                diag.record_layer("launch", time.monotonic() - start, type(exc).__name__)
                return L4Result(False, FailureSignature.LAUNCHER_FAILED, diag)

            try:
                context = await browser.new_context()
                page = await context.new_page()

                try:
                    await page.goto("https://claude.ai/login", timeout=PAGE_TIMEOUT_MS)
                except PWTimeout:
                    diag.record_layer("goto", time.monotonic() - start, FailureSignature.TIMEOUT)
                    return L4Result(False, FailureSignature.TIMEOUT, diag)
                except Exception as exc:
                    sig = _classify_goto_error(exc)
                    diag.record_layer("goto", time.monotonic() - start, sig)
                    return L4Result(False, sig, diag)

                if await _captcha_present(page):
                    diag.dom = await page.content()
                    try:
                        diag.screenshot_bytes = await page.screenshot(full_page=False)
                    except Exception:
                        pass
                    diag.record_layer("captcha", time.monotonic() - start, FailureSignature.CAPTCHA)
                    return L4Result(False, FailureSignature.CAPTCHA, diag)

                email_field = page.locator(
                    '[data-testid="login-email"], input[type="email"]'
                ).first
                try:
                    await email_field.wait_for(timeout=PAGE_TIMEOUT_MS, state="visible")
                    await email_field.fill(account_email)
                except PWTimeout:
                    diag.dom = await page.content()
                    diag.record_layer("email_field", time.monotonic() - start,
                                      FailureSignature.SELECTOR_NOT_FOUND)
                    return L4Result(False, FailureSignature.SELECTOR_NOT_FOUND, diag)

                submit = page.locator(
                    'button[type="submit"], button:has-text("Continue with email")'
                ).first
                try:
                    await submit.click(timeout=PAGE_TIMEOUT_MS)
                except PWTimeout:
                    diag.record_layer("submit_click", time.monotonic() - start,
                                      FailureSignature.SELECTOR_NOT_FOUND)
                    return L4Result(False, FailureSignature.SELECTOR_NOT_FOUND, diag)

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
                    try:
                        await page.goto(msg.url, timeout=PAGE_TIMEOUT_MS)
                    except PWTimeout:
                        diag.record_layer("goto_magic_link", time.monotonic() - start,
                                          FailureSignature.TIMEOUT)
                        return L4Result(False, FailureSignature.TIMEOUT, diag)

                await asyncio.sleep(2)
                html = await page.content()
                lockout = _text_indicates_lockout(html)
                if lockout:
                    diag.dom = html
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
        log.warning("L4 unexpected: %s", type(exc).__name__)
        diag.record_layer("unexpected", time.monotonic() - start, type(exc).__name__)
        return L4Result(False, FailureSignature.UNKNOWN, diag)
