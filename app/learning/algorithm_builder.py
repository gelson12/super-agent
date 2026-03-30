"""
Algorithm Builder — analyses wisdom store + insight logs to identify
recurring patterns and generate reusable Python algorithms.

Generated algorithms are committed to the dedicated GitHub repo
'super-agent-algorithms' (gelson12/super-agent-algorithms) so
Super Agent can call them as tools in future interactions.

Build triggers:
  - Every 200 interactions (via tick() in adapter)
  - On demand via POST /algorithms/build endpoint

Pattern types detected:
  1. Dominant routing patterns (which model wins most for a query type)
  2. Criticism patterns (what kinds of flaws get caught by red team / peer review)
  3. Complexity distribution patterns (word count → expected complexity)
  4. Error recovery patterns (what recovery strategies work after model failures)

Each algorithm is a pure Python function stored as a .py file in the repo
with a structured docstring describing its purpose, inputs, and outputs.
"""
import json
import os
import time
from typing import Optional

from ..learning.insight_log import LOG_PATH
from ..learning.wisdom_store import wisdom_store

# ── GitHub repo name for self-built algorithms ────────────────────────────────
ALGO_REPO = "super-agent-algorithms"
ALGO_BRANCH = "main"

# Minimum interactions before attempting to build a meaningful algorithm
_MIN_ENTRIES = 50


def _load_insight_entries() -> list[dict]:
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _ensure_repo_exists() -> bool:
    """Create the repo if it doesn't exist. Returns True if ready."""
    try:
        from ..tools.github_tools import _client
        from github import GithubException

        gh = _client()
        user = gh.get_user()
        try:
            gh.get_repo(f"{user.login}/{ALGO_REPO}")
            return True
        except GithubException as e:
            if e.status == 404:
                user.create_repo(
                    ALGO_REPO,
                    description=(
                        "Self-generated algorithms from Super Agent's "
                        "collective intelligence layer. "
                        "Auto-committed by the algorithm builder."
                    ),
                    private=False,
                    auto_init=True,
                )
                return True
            return False
    except Exception:
        return False


def _commit_algorithm(filename: str, code: str, description: str) -> Optional[str]:
    """
    Commit a generated algorithm file to the algorithms repo.
    Returns the file path on success, None on failure.
    """
    try:
        from ..tools.github_tools import _client
        from github import GithubException
        import base64

        repo = _client().get_repo(f"gelson12/{ALGO_REPO}")
        filepath = f"algorithms/{filename}"
        encoded = code.encode("utf-8")
        commit_msg = f"[AutoBuild] {description}"

        try:
            existing = repo.get_contents(filepath, ref=ALGO_BRANCH)
            repo.update_file(filepath, commit_msg, encoded, existing.sha, branch=ALGO_BRANCH)
        except GithubException as e:
            if e.status == 404:
                repo.create_file(filepath, commit_msg, encoded, branch=ALGO_BRANCH)
            else:
                raise
        return filepath
    except Exception:
        return None


# ── Algorithm generators ──────────────────────────────────────────────────────

def _build_routing_heuristic(entries: list[dict], win_rates: dict) -> Optional[str]:
    """
    Generate an algorithm that recommends a model based on query characteristics
    derived from historical win-rate data.
    """
    # Build category → best model mapping from win rates
    best: dict[str, tuple[str, float]] = {}
    for model, cats in win_rates.items():
        for category, data in cats.items():
            total = data.get("total", 0)
            if total < 10:
                continue
            rate = data.get("wins", 0) / total
            if category not in best or rate > best[category][1]:
                best[category] = (model, rate)

    if len(best) < 2:
        return None  # Not enough data

    # Complexity distribution from insight log
    complexity_model: dict[int, dict[str, int]] = {}
    for e in entries[-500:]:
        c = e.get("complexity", 0)
        m = e.get("model", "")
        err = e.get("error", False)
        if not err and m:
            bucket = complexity_model.setdefault(c, {})
            bucket[m] = bucket.get(m, 0) + 1

    best_by_complexity: dict[int, str] = {}
    for c, counts in complexity_model.items():
        if counts:
            best_by_complexity[c] = max(counts, key=lambda k: counts[k])

    # Format the learned routing table as a Python dict literal
    routing_table_lines = []
    for cat, (model, rate) in sorted(best.items()):
        routing_table_lines.append(f'    "{cat}": "{model}",  # {rate:.0%} win rate')
    routing_table_str = "\n".join(routing_table_lines)

    complexity_table_lines = []
    for c in sorted(best_by_complexity):
        m = best_by_complexity[c]
        complexity_table_lines.append(f"    {c}: \"{m}\",")
    complexity_table_str = "\n".join(complexity_table_lines)

    ts = int(time.time())
    code = f'''"""
Auto-generated routing heuristic algorithm.
Generated: {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))}
Source: {len(entries)} interactions, {len(best)} categories analysed.

Purpose:
    Recommends the best model for a query based on historically observed
    win rates per category and complexity score.

Inputs:
    category (str): e.g. "writing/analysis", "code/math"
    complexity (int): 1–5 complexity score

Output:
    str: recommended model name ("CLAUDE", "DEEPSEEK", "GEMINI", "HAIKU")
"""

_CATEGORY_ROUTING = {{
{routing_table_str}
}}

_COMPLEXITY_ROUTING = {{
{complexity_table_str}
}}

_FALLBACK = "HAIKU"


def recommend_model(category: str, complexity: int) -> str:
    """
    Recommend the historically best-performing model for a given
    category and complexity level.

    Args:
        category: The query category label.
        complexity: Integer 1–5 complexity score.

    Returns:
        Model name string.
    """
    if category in _CATEGORY_ROUTING:
        return _CATEGORY_ROUTING[category]
    if complexity in _COMPLEXITY_ROUTING:
        return _COMPLEXITY_ROUTING[complexity]
    return _FALLBACK
'''
    return code


def _build_complexity_predictor(entries: list[dict]) -> Optional[str]:
    """
    Generate an algorithm that predicts complexity from message features,
    calibrated on real historical data.
    """
    if len(entries) < _MIN_ENTRIES:
        return None

    # Bucket: word count range → average complexity
    word_complexity: list[tuple[int, int]] = []
    for e in entries[-500:]:
        wc = e.get("msg_words", 0)
        c = e.get("complexity", 0)
        if wc > 0 and c > 0:
            word_complexity.append((wc, c))

    if len(word_complexity) < 20:
        return None

    # Find 5 breakpoints (20th/40th/60th/80th percentiles of word count)
    word_counts_sorted = sorted(p[0] for p in word_complexity)
    n = len(word_counts_sorted)
    p20 = word_counts_sorted[n // 5]
    p40 = word_counts_sorted[2 * n // 5]
    p60 = word_counts_sorted[3 * n // 5]
    p80 = word_counts_sorted[4 * n // 5]

    ts = int(time.time())
    code = f'''"""
Auto-generated complexity predictor algorithm.
Generated: {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))}
Calibrated on {len(word_complexity)} historical interactions.

Purpose:
    Predicts the complexity score (1–5) of a user message
    based on word count and structural features. Calibrated
    from real Super Agent interaction data.

Inputs:
    message (str): The raw user message.

Output:
    int: Predicted complexity score 1–5.
"""

# Word-count thresholds (calibrated from {len(word_complexity)} interactions)
_WC_THRESHOLDS = ({p20}, {p40}, {p60}, {p80})  # 20th/40th/60th/80th percentiles

_HIGH_COMPLEXITY_SIGNALS = {{
    "implement", "design", "architect", "analyse", "analyze",
    "compare", "evaluate", "explain", "refactor", "debug",
    "optimize", "strategy", "algorithm", "proof", "derive",
}}

_TRIVIAL_SIGNALS = {{
    "hi", "hello", "thanks", "thank you", "ok", "sure", "yes", "no",
    "what is", "who is", "when is",
}}


def predict_complexity(message: str) -> int:
    """
    Predict complexity score for a message.

    Args:
        message: Raw user query string.

    Returns:
        Integer 1–5.
    """
    words = message.lower().split()
    wc = len(words)

    # Trivial override
    first_two = " ".join(words[:2]) if len(words) >= 2 else message.lower()
    if wc <= 5 or first_two in _TRIVIAL_SIGNALS:
        return 1

    # Base from word count percentile
    t1, t2, t3, t4 = _WC_THRESHOLDS
    if wc <= t1:
        base = 1
    elif wc <= t2:
        base = 2
    elif wc <= t3:
        base = 3
    elif wc <= t4:
        base = 4
    else:
        base = 5

    # Boost for high-complexity signal words
    word_set = set(words)
    signal_count = len(word_set & _HIGH_COMPLEXITY_SIGNALS)
    return min(5, base + (1 if signal_count >= 2 else 0))
'''
    return code


def _build_error_recovery_guide(entries: list[dict]) -> Optional[str]:
    """
    Generate an algorithm that recommends a fallback model when the
    primary model returns an error, based on observed error patterns.
    """
    # Count errors by model
    error_counts: dict[str, int] = {}
    success_counts: dict[str, int] = {}
    for e in entries[-500:]:
        m = e.get("model", "")
        if not m:
            continue
        if e.get("error"):
            error_counts[m] = error_counts.get(m, 0) + 1
        else:
            success_counts[m] = success_counts.get(m, 0) + 1

    # Build fallback chain: models ordered by success rate descending
    all_models = set(list(error_counts.keys()) + list(success_counts.keys()))
    rates: dict[str, float] = {}
    for m in all_models:
        total = success_counts.get(m, 0) + error_counts.get(m, 0)
        if total < 5:
            continue
        rates[m] = success_counts.get(m, 0) / total

    if len(rates) < 2:
        return None

    fallback_chain = sorted(rates, key=lambda k: rates[k], reverse=True)
    chain_repr = repr(fallback_chain)
    rate_repr = "\n".join(
        f'    "{m}": {rates[m]:.2f},' for m in fallback_chain
    )

    ts = int(time.time())
    code = f'''"""
Auto-generated error recovery algorithm.
Generated: {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))}
Derived from {sum(error_counts.values())} observed errors across {len(entries[-500:])} interactions.

Purpose:
    Recommends the best fallback model when the primary model fails,
    using a reliability chain derived from historical error rates.

Inputs:
    failed_model (str): The model that returned an error.
    excluded (list[str], optional): Additional models to skip.

Output:
    str: Recommended fallback model name.
"""

# Reliability chain (highest → lowest success rate from historical data)
_FALLBACK_CHAIN = {chain_repr}

# Historical success rates
_SUCCESS_RATES = {{
{rate_repr}
}}


def recommend_fallback(failed_model: str, excluded: list = None) -> str:
    """
    Recommend a fallback model when the primary model fails.

    Args:
        failed_model: The model name that produced an error.
        excluded: Optional list of additional models to skip.

    Returns:
        Best available fallback model name.
    """
    skip = {{failed_model.upper()}}
    if excluded:
        skip.update(m.upper() for m in excluded)

    for model in _FALLBACK_CHAIN:
        if model not in skip:
            return model

    # Last resort fallback
    return "HAIKU"
'''
    return code


# ── Public API ────────────────────────────────────────────────────────────────

def build_and_commit_algorithms() -> dict:
    """
    Main entry point. Analyses current wisdom + insight data, generates
    algorithms, and commits them to the algorithms repo.

    Returns a summary dict:
        {built: int, skipped: int, failed: int, algorithms: list[str]}
    """
    summary = {"built": 0, "skipped": 0, "failed": 0, "algorithms": []}

    entries = _load_insight_entries()
    if len(entries) < _MIN_ENTRIES:
        summary["skipped"] = 3
        return summary

    pool = wisdom_store.wisdom_dict()
    win_rates = pool.get("win_rates", {})

    if not _ensure_repo_exists():
        summary["failed"] = 3
        return summary

    # ── Algorithm 1: Routing heuristic ───────────────────────────────────────
    code = _build_routing_heuristic(entries, win_rates)
    if code:
        path = _commit_algorithm(
            "routing_heuristic.py",
            code,
            "Update routing heuristic from latest win-rate data",
        )
        if path:
            summary["built"] += 1
            summary["algorithms"].append(path)
        else:
            summary["failed"] += 1
    else:
        summary["skipped"] += 1

    # ── Algorithm 2: Complexity predictor ────────────────────────────────────
    code = _build_complexity_predictor(entries)
    if code:
        path = _commit_algorithm(
            "complexity_predictor.py",
            code,
            "Update complexity predictor from calibrated word-count data",
        )
        if path:
            summary["built"] += 1
            summary["algorithms"].append(path)
        else:
            summary["failed"] += 1
    else:
        summary["skipped"] += 1

    # ── Algorithm 3: Error recovery guide ────────────────────────────────────
    code = _build_error_recovery_guide(entries)
    if code:
        path = _commit_algorithm(
            "error_recovery.py",
            code,
            "Update error recovery fallback chain from error rate data",
        )
        if path:
            summary["built"] += 1
            summary["algorithms"].append(path)
        else:
            summary["failed"] += 1
    else:
        summary["skipped"] += 1

    # ── Update index README ───────────────────────────────────────────────────
    _update_readme(entries, win_rates, summary)

    return summary


def _update_readme(entries: list[dict], win_rates: dict, summary: dict) -> None:
    """Commit an updated README.md index to the algorithms repo."""
    total = len(entries)
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    model_lines = []
    for model, cats in win_rates.items():
        for cat, data in cats.items():
            t = data.get("total", 0)
            if t < 5:
                continue
            rate = data.get("wins", 0) / t
            model_lines.append(f"| {model} | {cat} | {rate:.0%} | {t} |")
    table = "\n".join(model_lines) if model_lines else "| (insufficient data) | | | |"

    content = f"""# Super Agent — Self-Built Algorithms

Auto-generated by Super Agent's collective intelligence layer.
Last updated: **{ts_str}**
Total interactions analysed: **{total}**
Algorithms committed this run: **{summary['built']}**

## Available Algorithms

| File | Purpose |
|------|---------|
| `algorithms/routing_heuristic.py` | Recommend best model per query category |
| `algorithms/complexity_predictor.py` | Predict query complexity from text features |
| `algorithms/error_recovery.py` | Fallback model chain when primary fails |

## Model Win Rates (current)

| Model | Category | Win Rate | Samples |
|-------|----------|----------|---------|
{table}

---
*This repository is managed automatically. Do not edit manually.*
"""
    _commit_algorithm("../README.md", content, f"Update README — {ts_str}")
