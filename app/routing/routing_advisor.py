"""
Routing Advisor (G1 + G5 + G8)
=============================
A read-only, non-binding recommendation engine that closes the learning loop.

The dispatcher and tiered_agent_invoke have always picked models from
hardcoded keyword lists, ignoring everything the learning subsystem knew.
This module is the read side: it consults

  - wisdom_store        → per-model win rates by category
  - agent_status_tracker → recent STRIKE state per worker
  - credit_throttle      → current budget tier
  - cost_ledger          → daily spend for cost-aware tier downgrade
  - algorithm_store      → learned heuristics (routing_heuristic, complexity_predictor)

…and produces a `RouteHint` the caller may use as a soft preference.

The advisor NEVER raises and NEVER blocks the request path. Every dependency
is wrapped in try/except: any sub-system being down or empty just means that
signal is dropped from the recommendation. This is by design — routing
correctness must not regress when learning subsystems hiccup.

Caller pattern:

    from app.routing.routing_advisor import recommend
    hint = recommend(message, classification="shell", session_id=sid)
    # hint.preferred_model, hint.deprioritize, hint.budget_tier, hint.reason

The dispatcher uses `preferred_model` as a tie-breaker when multiple tiers
are available, and skips models in `deprioritize` when an alternative exists.
The full 4-tier fallback chain still owns availability — the advisor only
expresses preference within available options.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RouteHint:
    preferred_model: Optional[str] = None
    deprioritize: list[str] = field(default_factory=list)
    budget_tier: str = "full"            # full | reduced | minimal | critical
    complexity_hint: Optional[int] = None
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no signal"

    def as_dict(self) -> dict:
        return {
            "preferred_model": self.preferred_model,
            "deprioritize": list(self.deprioritize),
            "budget_tier": self.budget_tier,
            "complexity_hint": self.complexity_hint,
            "reason": self.reason,
        }


# ── Subsystem readers (each returns None / safe default on any error) ────────

def _read_budget_tier() -> str:
    try:
        from ..learning.credit_throttle import get_status
        return get_status().get("tier", "full")
    except Exception:
        return "full"


def _read_strikes() -> set[str]:
    """Workers currently in STRIKE state — caller deprioritizes them."""
    try:
        from ..learning.agent_status_tracker import _live_status as _ls  # type: ignore
        # _live_status keys are worker ids; values include {status, strike_count, ...}
        out: set[str] = set()
        for worker, info in (_ls.items() if hasattr(_ls, "items") else []):
            try:
                if info and info.get("status") == "STRIKE":
                    out.add(str(worker))
            except Exception:
                continue
        return out
    except Exception:
        return set()


def _read_best_model(category: str) -> Optional[str]:
    try:
        from ..learning.wisdom_store import wisdom_store
        return wisdom_store.get_best_model_for_category(category)
    except Exception:
        return None


def _detect_category(classification: Optional[str], message: str) -> str:
    """Map a classification (agent name) to the wisdom_store category space."""
    try:
        from ..learning.wisdom_store import wisdom_store
        return wisdom_store._detect_category(classification or "", "")
    except Exception:
        # Cheap keyword fallback so caller still gets a useful category.
        m = (message or "").lower()
        if any(k in m for k in ("code", "function", "bug", "error", "compile")):
            return "code/math"
        if any(k in m for k in ("write", "essay", "summarize", "draft", "letter")):
            return "writing/analysis"
        return "general"


def _algorithm_run(name: str, *args) -> Optional[object]:
    """Call algorithm_store algorithms safely. Returns None on miss."""
    try:
        from ..learning.algorithm_store import algorithm_store
        algo = algorithm_store.get_algorithm(name)
        if algo and hasattr(algo, "run"):
            return algo.run(*args)
    except Exception:
        return None
    return None


# ── Public API ────────────────────────────────────────────────────────────────

# Operational keywords roughly mirror agent_routing._OPERATIONAL_KEYWORDS but
# kept local so this module has zero coupling back into the dispatcher.
_OPERATIONAL_HINTS = (
    "build", "create", "deploy", "fix", "push", "delete", "restart",
    "execute", "write", "commit", "redeploy", "merge", "update",
)


def _looks_operational(message: str) -> bool:
    m = (message or "").lower()
    return any(k in m for k in _OPERATIONAL_HINTS)


def recommend(
    message: str,
    classification: Optional[str] = None,
    session_id: str = "default",
) -> RouteHint:
    """
    Return a soft routing recommendation. Never raises.

    Order of signals (later signals refine earlier ones):
      1. Budget tier  → may downgrade preferred_model under spend pressure
      2. Wisdom store → per-category best model, if data exists
      3. Algorithm store → learned routing_heuristic + complexity_predictor
      4. STRIKE state → deprioritize crashed workers
    """
    hint = RouteHint()

    # 1. Budget tier — sets the cost-aware floor (G8).
    tier = _read_budget_tier()
    hint.budget_tier = tier
    if tier in ("minimal", "critical"):
        hint.preferred_model = "DEEPSEEK"  # cheapest tool-using path
        hint.deprioritize.append("CLAUDE")  # Sonnet is most expensive
        hint.reasons.append(f"budget_tier={tier} → prefer DeepSeek, deprioritize Sonnet")

    # 2. Wisdom store — per-category best model from observed win rates.
    category = _detect_category(classification, message)
    best = _read_best_model(category)
    if best and tier == "full":
        # Only let wisdom override when budget is comfortable.
        hint.preferred_model = best
        hint.reasons.append(f"wisdom_store best for {category!r} = {best}")
    elif best:
        hint.reasons.append(f"wisdom_store best for {category!r} = {best} (overridden by budget)")

    # 3. Algorithm store — learned heuristics (G5).
    cx = _algorithm_run("complexity_predictor", message)
    if isinstance(cx, (int, float)):
        cx_i = max(1, min(5, int(cx)))
        hint.complexity_hint = cx_i
        hint.reasons.append(f"complexity_predictor → {cx_i}")

    learned_pref = _algorithm_run("routing_heuristic", message, hint.complexity_hint or 3)
    if isinstance(learned_pref, str) and learned_pref:
        # Algorithm output overrides wisdom only when budget allows.
        if tier == "full":
            hint.preferred_model = learned_pref.upper()
            hint.reasons.append(f"routing_heuristic → {learned_pref}")
        else:
            hint.reasons.append(f"routing_heuristic → {learned_pref} (overridden by budget)")

    # 4. STRIKE state — deprioritize models recently crashed.
    strikes = _read_strikes()
    for worker in strikes:
        upper = worker.upper()
        # Map worker labels to model names where they overlap
        for model_name in ("CLAUDE", "GEMINI", "DEEPSEEK", "HAIKU", "SONNET"):
            if model_name in upper and model_name not in hint.deprioritize:
                hint.deprioritize.append(model_name)
                hint.reasons.append(f"strike on {worker} → deprioritize {model_name}")
                break

    # Final sanity: if preferred_model is also in deprioritize, drop the preference.
    if hint.preferred_model and hint.preferred_model in hint.deprioritize:
        hint.reasons.append(
            f"preferred model {hint.preferred_model} also deprioritized → no preference"
        )
        hint.preferred_model = None

    return hint
