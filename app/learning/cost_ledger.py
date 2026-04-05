"""
Token cost ledger — tracks estimated API spend per model, per category, per day.

Categories:
  chat          — user-facing /chat endpoint calls
  auto_fix      — build_repair, n8n_repair autonomous fixes
  code_review   — diff-aware deploy review, nightly review applying changes
  voting        — improvement_vote 5-model consensus calls
  improvement   — auto_apply_safe_suggestions, weekly_review applying changes
  health_check  — scheduled infrastructure health checks
  n8n           — n8n agent workflow build/manage calls
  benchmark     — weekly benchmark test suite
  other         — anything not categorised above

Pricing (per 1M tokens, blended input+output — update as Anthropic pricing changes):
  claude-sonnet-4-6  : $4.50
  claude-haiku-4-5   : $0.40
  claude-opus-4-6    : $18.00
  gemini-*           : $0.50
  deepseek-*         : $0.30

API: GET /credits/spend       — summary (today / 7d / 30d)
     GET /credits/breakdown   — per-category daily + weekly table
"""
import json, os, time, datetime
from pathlib import Path

_DIR = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
LEDGER_PATH = _DIR / "cost_ledger.json"

_COST_PER_M: dict[str, float] = {
    "CLAUDE":   4.50,
    "SONNET":   4.50,
    "HAIKU":    0.40,
    "OPUS":     18.00,
    "GEMINI":   0.50,
    "DEEPSEEK": 0.30,
    "ENSEMBLE": 3.00,
    "AGENT":    4.50,
    "UNKNOWN":  2.00,
}
_CHARS_PER_TOKEN = 4.0
_MAX_ENTRIES = 10000

CATEGORIES = (
    "chat", "auto_fix", "code_review", "voting",
    "improvement", "health_check", "n8n", "benchmark", "other",
)


def _model_key(model: str) -> str:
    m = (model or "UNKNOWN").upper()
    for key in _COST_PER_M:
        if key in m:
            return key
    return "UNKNOWN"


def record_call(
    model: str,
    input_chars: int,
    output_chars: int,
    category: str = "chat",
) -> None:
    """
    Record one API call with estimated cost.

    Args:
        model:        Model name string (e.g. "CLAUDE", "HAIKU", "claude-sonnet-4-6")
        input_chars:  Character count of the input/prompt sent
        output_chars: Character count of the response received
        category:     One of CATEGORIES — which autonomous function caused this call
    """
    tokens = (input_chars + output_chars) / _CHARS_PER_TOKEN
    cost = tokens / 1_000_000 * _COST_PER_M.get(_model_key(model), 2.0)
    entry = {
        "ts": round(time.time(), 1),
        "model": _model_key(model),
        "tokens": round(tokens),
        "cost_usd": round(cost, 6),
        "category": category if category in CATEGORIES else "other",
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
    try:
        budget = float(os.environ.get("DAILY_BUDGET_USD", "5.0"))
        today_total = get_spend(hours=24)["total_usd"]
        pct = today_total / budget * 100 if budget else 0
        if pct >= 80:
            from ..activity_log import bg_log
            bg_log(
                f"BUDGET ALERT: daily spend ${today_total:.3f} = {pct:.0f}% of ${budget:.2f} budget — "
                "switching conversational routes to Haiku. Autonomous job frequency reduced.",
                source="cost_ledger",
            )
            try:
                (_DIR / ".budget_alert").write_text(str(today_total), encoding="utf-8")
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
    budget = float(os.environ.get("DAILY_BUDGET_USD", "5.0"))
    return {
        "window_hours": hours,
        "total_usd": round(total, 4),
        "by_model": by_model,
        "call_count": len(entries),
        "budget_usd": budget,
        "budget_pct_used": round(total / budget * 100, 1) if budget else 0,
    }


def get_breakdown() -> dict:
    """
    Per-category breakdown: daily (24h) + weekly (168h) spend.

    Returns a structure ready to display as a table:
    {
      "daily":  { category: usd, ... , "total": usd },
      "weekly": { category: usd, ... , "total": usd },
      "by_model_daily":  { model: usd, ... },
      "by_model_weekly": { model: usd, ... },
      "most_expensive_daily":  { "category": str, "usd": float },
      "most_expensive_weekly": { "category": str, "usd": float },
      "budget_usd": float,
      "budget_pct_used_today": float,
    }
    """
    now = time.time()
    day_cut  = now - 86400
    week_cut = now - 604800

    all_entries = _load()
    day_entries  = [e for e in all_entries if e.get("ts", 0) >= day_cut]
    week_entries = [e for e in all_entries if e.get("ts", 0) >= week_cut]

    def _by_cat(entries: list) -> dict[str, float]:
        totals: dict[str, float] = {c: 0.0 for c in CATEGORIES}
        for e in entries:
            cat = e.get("category", "other")
            if cat not in totals:
                cat = "other"
            totals[cat] = round(totals[cat] + e.get("cost_usd", 0), 6)
        return totals

    def _by_model(entries: list) -> dict[str, float]:
        totals: dict[str, float] = {}
        for e in entries:
            m = e.get("model", "UNKNOWN")
            totals[m] = round(totals.get(m, 0) + e.get("cost_usd", 0), 6)
        return totals

    daily_cat  = _by_cat(day_entries)
    weekly_cat = _by_cat(week_entries)
    daily_total  = round(sum(daily_cat.values()), 4)
    weekly_total = round(sum(weekly_cat.values()), 4)

    budget = float(os.environ.get("DAILY_BUDGET_USD", "5.0"))

    # Find priciest category
    def _top(cat_dict: dict) -> dict:
        if not cat_dict:
            return {"category": "none", "usd": 0.0}
        top = max(cat_dict, key=lambda k: cat_dict[k])
        return {"category": top, "usd": round(cat_dict[top], 4)}

    return {
        "daily":  {**{k: round(v, 4) for k, v in daily_cat.items()},  "total": daily_total},
        "weekly": {**{k: round(v, 4) for k, v in weekly_cat.items()}, "total": weekly_total},
        "by_model_daily":  _by_model(day_entries),
        "by_model_weekly": _by_model(week_entries),
        "most_expensive_daily":  _top(daily_cat),
        "most_expensive_weekly": _top(weekly_cat),
        "budget_usd": budget,
        "budget_pct_used_today": round(daily_total / budget * 100, 1) if budget else 0,
        "call_count_daily":  len(day_entries),
        "call_count_weekly": len(week_entries),
    }


def is_over_budget() -> bool:
    try:
        flag = _DIR / ".budget_alert"
        if not flag.exists():
            return False
        return (time.time() - flag.stat().st_mtime) < 86400
    except Exception:
        return False


def get_credit_pct_remaining() -> float:
    """
    Returns estimated % of daily budget remaining (100 = full, 0 = exhausted).
    Falls back to 100 if budget not configured.
    """
    try:
        budget = float(os.environ.get("DAILY_BUDGET_USD", "0"))
        if not budget:
            return 100.0
        spent = get_spend(hours=24)["total_usd"]
        return max(0.0, round((1 - spent / budget) * 100, 1))
    except Exception:
        return 100.0


def spend_summary() -> dict:
    return {
        "today":         get_spend(24),
        "last_7_days":   get_spend(168),
        "last_30_days":  get_spend(720),
        "breakdown":     get_breakdown(),
        "budget_usd_daily": float(os.environ.get("DAILY_BUDGET_USD", "5.0")),
        "over_budget":   is_over_budget(),
        "credit_pct_remaining": get_credit_pct_remaining(),
    }
