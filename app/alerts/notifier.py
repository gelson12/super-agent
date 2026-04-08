"""
Super Agent Alert Notifier — rate-limited email alerts for CLI failures,
Gemini failures, Anthropic credit consumption, and proactive warnings.

Alert hierarchy:
  INFO     — CLI recovered, token refreshed
  WARNING  — Claude CLI down (Gemini taking over), daily limit hit
  CRITICAL — Both CLIs down (Anthropic credits being consumed), CLI down 2h+

Throttle: same alert_key fires at most once per TTL (default 1 hour).
Recovery and CRITICAL alerts always bypass throttle (force=True).

SMTP config: uses smtp_user / smtp_password / notify_email from settings
(same Gmail App Password used for bridge website notifications).
"""
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_THROTTLE_DIR = Path("/tmp/sa_alerts")
_DEFAULT_TTL  = 3600   # 1 hour between same non-forced alert
_WARN_TTL     = 1800   # 30 min for escalation alerts


def _throttle_path(alert_key: str) -> Path:
    _THROTTLE_DIR.mkdir(parents=True, exist_ok=True)
    return _THROTTLE_DIR / f"{alert_key}.flag"


def _is_throttled(alert_key: str, ttl: int) -> bool:
    """Return True if the same alert fired within ttl seconds."""
    try:
        flag = _throttle_path(alert_key)
        if flag.exists() and (time.time() - flag.stat().st_mtime) < ttl:
            return True
        flag.touch()
        return False
    except Exception:
        return False


def _clear_throttle(alert_key: str) -> None:
    """Remove throttle flag so the next event fires immediately."""
    try:
        _throttle_path(alert_key).unlink(missing_ok=True)
    except Exception:
        pass


def _flag_age_minutes(alert_key: str) -> float:
    """Return how many minutes since this alert key last fired (0 if never)."""
    try:
        flag = _throttle_path(alert_key)
        if flag.exists():
            return (time.time() - flag.stat().st_mtime) / 60
    except Exception:
        pass
    return 0.0


def _vscode_url() -> str:
    domain = os.environ.get("CLI_WORKER_URL", "").rstrip("/")
    return domain or "https://inspiring-cat-production.up.railway.app"


def _build_email(subject: str, body: str, level: str = "WARNING") -> MIMEMultipart:
    icons = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}
    icon  = icons.get(level, "⚠️")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{icon} [Super Agent] {subject}"

    vscode = _vscode_url()
    plain = (
        f"{body}\n\n"
        f"---\n"
        f"Super Agent Alert | Level: {level}\n"
        f"VS Code: {vscode}\n"
        f"Health:  {vscode}/health\n"
        f"Fix Claude: VS Code terminal → 'claude login' → update CLAUDE_SESSION_TOKEN in Railway"
    )
    msg.attach(MIMEText(plain, "plain"))
    return msg


def send_alert(
    subject: str,
    body: str,
    alert_key: str = "",
    level: str = "WARNING",
    force: bool = False,
    ttl: int = _DEFAULT_TTL,
) -> bool:
    """
    Send an email alert.

    Args:
        subject:   Short subject line (icon + [Super Agent] prepended automatically)
        body:      Alert body text
        alert_key: Throttle key — same key fires at most once per ttl seconds
        level:     "INFO" | "WARNING" | "CRITICAL"
        force:     True bypasses throttle (use for recovery and CRITICAL events)
        ttl:       Throttle window in seconds (default 3600 = 1 hour)

    Returns True if sent, False if throttled / not configured / send failed.
    Never raises.
    """
    try:
        if alert_key and not force and _is_throttled(alert_key, ttl):
            return False

        from ..config import settings
        if not settings.smtp_user or not settings.smtp_password or not settings.notify_email:
            return False

        msg            = _build_email(subject, body, level)
        msg["From"]    = settings.smtp_user
        msg["To"]      = settings.notify_email

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        return True
    except Exception:
        return False


# ── Convenience helpers ────────────────────────────────────────────────────────

def alert_claude_cli_down(reason: str, gemini_ok: bool = True) -> bool:
    """Alert when Claude CLI auth fails or binary is missing."""
    fallback = "Gemini CLI is now primary backup (free tier)." if gemini_ok else \
               "⚠️ Gemini CLI status unknown — Anthropic credits may be consumed."
    return send_alert(
        subject="Claude Pro CLI is DOWN",
        body=(
            f"Claude CLI failed: {reason}\n\n"
            f"{fallback}\n\n"
            f"Anthropic credits will only be used if Gemini also fails.\n\n"
            f"To fix:\n"
            f"  1. Open VS Code: {_vscode_url()}\n"
            f"  2. Open terminal (Ctrl+`)\n"
            f"  3. Run: claude login\n"
            f"  4. Approve in browser\n"
            f"  5. Run: cat /root/.claude/.credentials.json | base64 -w0\n"
            f"  6. Update CLAUDE_SESSION_TOKEN in Railway Variables for both services\n"
            f"  7. Redeploy both services\n\n"
            f"The watchdog probes every 5 min and will auto-recover when auth is restored."
        ),
        alert_key="claude_cli_down",
        level="WARNING",
    )


def alert_claude_daily_limit(reset_hours: float) -> bool:
    """Alert when Claude hits its daily message cap."""
    return send_alert(
        subject=f"Claude Daily Limit Hit — resumes in {reset_hours:.1f}h",
        body=(
            f"Claude Pro CLI hit its daily message cap.\n"
            f"Estimated reset in {reset_hours:.1f} hours.\n\n"
            f"Gemini CLI is now primary. Anthropic credits used only if Gemini fails.\n"
            f"Pro CLI will resume automatically after reset — no action needed."
        ),
        alert_key="claude_daily_limit",
        level="WARNING",
        ttl=_DEFAULT_TTL,
    )


def alert_gemini_cli_down(error: str) -> bool:
    """Alert when Gemini CLI fails — Anthropic credits are next fallback."""
    return send_alert(
        subject="Gemini CLI FAILED — Anthropic credits now active",
        body=(
            f"Gemini CLI returned an error: {error[:300]}\n\n"
            f"🚨 Anthropic credits are now being consumed as last resort fallback.\n\n"
            f"To fix Gemini:\n"
            f"  1. Open VS Code: {_vscode_url()}\n"
            f"  2. Terminal → run: gemini auth login\n"
            f"  3. Approve with Google account\n"
            f"  4. Run: base64 -w0 /root/.gemini/credentials.json\n"
            f"  5. Update GEMINI_SESSION_TOKEN in Railway Variables → redeploy\n\n"
            f"Also check Claude CLI status — if both recover, credits stop being consumed."
        ),
        alert_key="gemini_cli_down",
        level="CRITICAL",
        force=True,  # always alert when credits are at risk
    )


def alert_anthropic_credits_active(context: str = "") -> bool:
    """Alert when Anthropic API key fallback is confirmed active."""
    return send_alert(
        subject="Anthropic Credits Being Consumed",
        body=(
            f"Both Claude CLI and Gemini CLI are unavailable.\n"
            f"Anthropic API key (credits) is now handling all requests.\n"
            + (f"\nContext: {context}\n" if context else "") +
            f"\nTo stop credit consumption:\n"
            f"  Fix Claude CLI: claude login in VS Code terminal\n"
            f"  Fix Gemini CLI: gemini auth login in VS Code terminal\n"
            f"  VS Code: {_vscode_url()}"
        ),
        alert_key="anthropic_credits_active",
        level="CRITICAL",
        ttl=_WARN_TTL,  # re-alert every 30 min while active
    )


def alert_claude_recovered(subscription: str = "") -> bool:
    """Alert when Claude CLI recovers — always sent (force=True)."""
    _clear_throttle("claude_cli_down")  # reset so next failure alerts immediately
    sub_str = f" (subscription: {subscription})" if subscription else ""
    return send_alert(
        subject="Claude Pro CLI RECOVERED ✓",
        body=(
            f"Claude CLI is back online{sub_str}.\n"
            f"Pro subscription is now primary again.\n"
            f"Anthropic credits fallback deactivated.\n\n"
            f"Health check: {_vscode_url()}/health"
        ),
        alert_key="claude_recovery",
        level="INFO",
        force=True,
    )


def alert_cli_still_down_escalation(minutes_down: float) -> bool:
    """Escalating alert when CLI has been down for extended time."""
    level   = "CRITICAL" if minutes_down >= 120 else "WARNING"
    subject = f"Claude CLI still DOWN ({int(minutes_down)}min) — action required"
    return send_alert(
        subject=subject,
        body=(
            f"Claude CLI has been unavailable for {int(minutes_down)} minutes.\n\n"
            f"{'🚨 CRITICAL: ' if minutes_down >= 120 else ''}Gemini is covering as backup.\n"
            f"Anthropic credits will be consumed if Gemini also fails.\n\n"
            f"To fix now:\n"
            f"  1. VS Code: {_vscode_url()}\n"
            f"  2. Terminal → claude login → approve → update CLAUDE_SESSION_TOKEN in Railway"
        ),
        alert_key=f"cli_down_escalation_{int(minutes_down // 30) * 30}min",
        level=level,
        force=True,
        ttl=_DEFAULT_TTL,
    )


def alert_token_refresh_failed(token_type: str, error: str) -> bool:
    """Alert when the nightly token keeper fails to refresh a token."""
    return send_alert(
        subject=f"{token_type} token auto-refresh FAILED",
        body=(
            f"The nightly {token_type} token keeper failed to refresh the session token.\n"
            f"Error: {error[:300]}\n\n"
            f"⚠️ The token will expire soon and CLI may go down.\n\n"
            f"Manual fix:\n"
            f"  1. VS Code: {_vscode_url()}\n"
            f"  2. Terminal → {'claude login' if 'claude' in token_type.lower() else 'gemini auth login'}\n"
            f"  3. Update {'CLAUDE' if 'claude' in token_type.lower() else 'GEMINI'}_SESSION_TOKEN in Railway Variables"
        ),
        alert_key=f"{token_type.lower()}_refresh_failed",
        level="WARNING",
    )
