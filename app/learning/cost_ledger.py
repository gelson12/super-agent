"""
Token cost ledger — tracks estimated API spend per model per day.

Records estimated token counts from insight_log response lengths.
Exposes daily/weekly spend totals and fires a budget alert via activity_log
when daily spend crosses 80% of DAILY_BUDGET_USD (env var, default $5).

Pricing (per 1M tokens, input+output blended estimate — update as needed):
  claude-sonnet-4-6 : $4.50
  claude-haiku-4-5  : $0.40
  gemini-*          : $0.50
  deepseek-*        : $0.30

API: GET /credits/spend
"""
import json, os, time, datetime
from pathlib import Path

_DIR = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
LEDGER_PATH = _DIR / "cost_ledger.json"

# Cost per 1M tokens (blended input+output estimate)
_COST_PER_M: dict[str, float] = {
    "CLAUDE":   4.50,
    "HAIKU":    0.40,
    "GEMINI":   0.50,
    "DEEPSEEK": 0.30,
    "ENSEMBLE": 3.00,
    "AGENT":    4.50,
    "UNKNOWN":  2.00,
}
# Rough chars-per-token estimate
_CHARS_PER_TOKEN = 4.0
_MAX_ENTRIES = 5000


def _model_key(model: str) -> str:
    m = (model or "UNKNOWN").upper()
    for key in _COST_PER_M:
        if key in m:
            return key
    return "UNKNOWN"


def record_call(model: str, input_chars: int, output_chars: int) -> None:
    """Record one API call. input/output in characters."""
    tokens = (input_chars + output_chars) / _CHARS_PER_TOKEN
    cost = tokens / 1_000_000 * _COST_PER_M.get(_model_key(model), 2.0)
    entry = {
        "ts": round(time.time(), 1),
        "model": _model_key(model),
        "tokens": round(tokens),
        "cost_usd": round(cost, 6),
    }
    try:
        existing = _load()
        existing.append(entry)
        if len(existing) > _MAX_ENTRIES:
            existing = existing[-_MAX_ENTRIES:]
        LEDGER_PATH.write_text(json.dumps(existing), encoding="utf-8")
    except Exception:
        pass
    _check_budget(cost)


def _load() -> list:
    try:
        if LEDGER_PATH.exists():
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _check_budget(just_spent: float) -> None:
    """Alert via activity_log if daily spend crosses 80% of budget."""
    try:
        budget = float(os.environ.get("DAILY_BUDGET_USD", "5.0"))
        today_total = get_spend(hours=24)["total_usd"]
        pct = today_total / budget * 100 if budget else 0
        if pct >= 80:
            from ..activity_log import bg_log
            bg_log(
                f"BUDGET ALERT: daily spend ${today_total:.3f} = {pct:.0f}% of ${budget:.2f} budget. "
                "Cheaper models (Haiku/DeepSeek) will be preferred.",
                source="cost_ledger",
            )
            # Flip a flag that the dispatcher can read to prefer cheaper models
            try:
                flag = _DIR / ".budget_alert"
                flag.write_text(str(today_total), encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass


def get_spend(hours: float = 24.0) -> dict:
    cutoff = time.time() - hours * 3600
    entries = [e for e in _load() if e.get("ts", 0) >= cutoff]
    total = sum(e.get("cost_usd", 0) for e in entries)
    by_model: dict[str, float] = {}
    for e in entries:
        m = e.get("model", "UNKNOWN")
        by_model[m] = round(by_model.get(m, 0) + e.get("cost_usd", 0), 6)
    return {
        "window_hours": hours,
        "total_usd": round(total, 4),
        "by_model": by_model,
        "call_count": len(entries),
        "budget_usd": float(os.environ.get("DAILY_BUDGET_USD", "5.0")),
        "budget_pct_used": round(total / float(os.environ.get("DAILY_BUDGET_USD", "5.0")) * 100, 1),
    }


def is_over_budget() -> bool:
    """True if daily spend is >= 80% of DAILY_BUDGET_USD. Cheap check for dispatcher."""
    try:
        flag = _DIR / ".budget_alert"
        if not flag.exists():
            return False
        ts = flag.stat().st_mtime
        # Alert flag is only valid for today
        return (time.time() - ts) < 86400
    except Exception:
        return False


def spend_summary() -> dict:
    """Full spend report: today + last 7 days + last 30 days."""
    return {
        "today": get_spend(24),
        "last_7_days": get_spend(168),
        "last_30_days": get_spend(720),
        "budget_usd_daily": float(os.environ.get("DAILY_BUDGET_USD", "5.0")),
        "over_budget": is_over_budget(),
    }
