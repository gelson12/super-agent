"""
Credit & usage tracker — zero wasted API calls.

Strategy:
- Token usage is extracted from every model response (already returned free).
- Cost is estimated locally using known pricing tables.
- DeepSeek actual balance is fetched via their lightweight GET /user/balance
  endpoint at most once per hour (not per request).
- Everything is stored in SQLite and exposed via /credits.
- Warnings fire at LOW (< 20% budget) and CRITICAL (< 5% budget) thresholds.
"""

import time
import sqlite3
import httpx
from dataclasses import dataclass, field
from threading import Lock

# ── Pricing (USD per 1M tokens, March 2026) ─────────────────────────────────
PRICING = {
    "CLAUDE": {"input": 3.00, "output": 15.00},   # Claude Sonnet 4.6
    "GEMINI": {"input": 0.15, "output": 0.60},    # Gemini 2.5 Flash
    "DEEPSEEK": {"input": 0.14, "output": 0.28},  # DeepSeek Chat (cache miss)
}

# Warn when estimated remaining budget drops below these fractions
LOW_THRESHOLD = 0.20       # 20%
CRITICAL_THRESHOLD = 0.05  # 5%

# How often to actually call DeepSeek balance API (seconds)
DEEPSEEK_BALANCE_TTL = 3600  # 1 hour

DB_PATH = "agent_memory.db"


@dataclass
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    call_count: int = 0


class UsageTracker:
    """
    Thread-safe singleton that records token usage from model responses
    and exposes credit health without making unnecessary API calls.
    """

    def __init__(self):
        self._lock = Lock()
        self._usage: dict[str, ModelUsage] = {
            "CLAUDE": ModelUsage(),
            "GEMINI": ModelUsage(),
            "DEEPSEEK": ModelUsage(),
        }
        self._deepseek_balance: float | None = None
        self._deepseek_balance_fetched_at: float = 0.0
        self._budgets: dict[str, float] = {}  # set via configure()
        self._init_db()
        self._load_from_db()

    # ── Public API ────────────────────────────────────────────────────────────

    def configure(self, budgets: dict[str, float]) -> None:
        """Set per-model budgets in USD. Call once at startup."""
        with self._lock:
            self._budgets = budgets

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """
        Called after every model response. Calculates cost from token counts
        returned by the API — no extra network call needed.
        """
        if model not in PRICING:
            return
        p = PRICING[model]
        cost = (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000
        with self._lock:
            u = self._usage[model]
            u.input_tokens += input_tokens
            u.output_tokens += output_tokens
            u.estimated_cost_usd += cost
            u.call_count += 1
        self._persist(model)

    def get_status(self, deepseek_api_key: str = "") -> dict:
        """
        Return credit health for all models.
        Only calls DeepSeek balance API if TTL has expired.
        """
        with self._lock:
            deepseek_balance = self._get_deepseek_balance(deepseek_api_key)
            result = {}
            for model, usage in self._usage.items():
                budget = self._budgets.get(model, 0)
                spent = usage.estimated_cost_usd

                if model == "DEEPSEEK" and deepseek_balance is not None:
                    remaining = deepseek_balance
                    source = "api_balance"
                else:
                    remaining = max(budget - spent, 0) if budget else None
                    source = "estimated"

                level = self._warning_level(remaining, budget)
                result[model] = {
                    "calls": usage.call_count,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "estimated_spent_usd": round(spent, 6),
                    "budget_usd": budget or None,
                    "remaining_usd": round(remaining, 6) if remaining is not None else None,
                    "remaining_source": source,
                    "status": level,
                }
            return result

    # ── Internals ─────────────────────────────────────────────────────────────

    def _warning_level(self, remaining: float | None, budget: float) -> str:
        if remaining is None or budget == 0:
            return "UNKNOWN"
        fraction = remaining / budget
        if fraction <= CRITICAL_THRESHOLD:
            return "CRITICAL"
        if fraction <= LOW_THRESHOLD:
            return "LOW"
        return "OK"

    def _get_deepseek_balance(self, api_key: str) -> float | None:
        """Fetch DeepSeek balance at most once per TTL window."""
        if not api_key:
            return None
        now = time.time()
        if now - self._deepseek_balance_fetched_at < DEEPSEEK_BALANCE_TTL:
            return self._deepseek_balance
        try:
            resp = httpx.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                balance_info = data.get("balance_infos", [{}])[0]
                self._deepseek_balance = float(balance_info.get("total_balance", 0))
                self._deepseek_balance_fetched_at = now
        except Exception:
            pass
        return self._deepseek_balance

    # ── SQLite persistence ────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_usage (
                    model TEXT PRIMARY KEY,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0.0,
                    call_count INTEGER DEFAULT 0
                )
            """)

    def _load_from_db(self) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute("SELECT * FROM model_usage").fetchall()
                for model, inp, out, cost, calls in rows:
                    if model in self._usage:
                        self._usage[model] = ModelUsage(inp, out, cost, calls)
        except Exception:
            pass

    def _persist(self, model: str) -> None:
        u = self._usage[model]
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    INSERT INTO model_usage (model, input_tokens, output_tokens, estimated_cost_usd, call_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(model) DO UPDATE SET
                        input_tokens=excluded.input_tokens,
                        output_tokens=excluded.output_tokens,
                        estimated_cost_usd=excluded.estimated_cost_usd,
                        call_count=excluded.call_count
                """, (model, u.input_tokens, u.output_tokens, u.estimated_cost_usd, u.call_count))
        except Exception:
            pass


# Singleton — imported everywhere
tracker = UsageTracker()
