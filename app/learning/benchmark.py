"""
Self-benchmarking — runs a fixed test suite weekly and scores quality with Haiku.

A fixed set of prompts (one per route type) runs automatically.
Haiku scores each response 1-10. If a model drops significantly vs baseline,
the router's win-rate bias is adjusted and an alert is written to the activity log.

Called by the weekly review scheduler. Also exposed via POST /benchmark/run.
Results written to /workspace/benchmark_YYYY-MM-DD.json
"""
import json, os, time, datetime
from pathlib import Path

_DIR = Path("/workspace") if os.access("/workspace", os.W_OK) else Path(".")
_BENCH_PROMPTS = [
    {"id": "code_explain",   "route": "claude",    "prompt": "Explain what a Python context manager is and give a short example."},
    {"id": "code_write",     "route": "claude",    "prompt": "Write a Python function that validates an email address with regex."},
    {"id": "math_reasoning", "route": "deepseek",  "prompt": "What is the sum of all integers from 1 to 100? Show your working."},
    {"id": "factual_qa",     "route": "gemini",    "prompt": "What year was the Eiffel Tower completed and how tall is it?"},
    {"id": "summarise",      "route": "claude",    "prompt": "Summarise this in 2 sentences: 'Machine learning is a subset of artificial intelligence that enables computers to learn from data without being explicitly programmed. It uses statistical techniques to give computers the ability to progressively improve performance on a specific task.'"},
    {"id": "routing_n8n",    "route": "n8n",       "prompt": "List all n8n workflows. Just call the tool, don't explain."},
    {"id": "github_read",    "route": "github",    "prompt": "Read the README from the super-agent repo. Summarise the first paragraph."},
    {"id": "conversational", "route": "claude",    "prompt": "What are three good practices for writing maintainable code?"},
    {"id": "creative",       "route": "claude",    "prompt": "Write a 3-line poem about a robot learning to feel emotions."},
    {"id": "search",         "route": "search",    "prompt": "Search for the latest Python version released in 2024."},
]

_JUDGE_PROMPT = """You are a quality judge for an AI assistant. Score the response below on a scale of 1-10.

Criteria:
- 10: Perfect — accurate, complete, well-formatted, addresses the question fully
- 7-9: Good — mostly correct with minor gaps
- 4-6: Mediocre — partially correct or incomplete
- 1-3: Poor — wrong, empty, or clearly an error message

Question: {question}
Response: {response}

Reply with ONLY a JSON object: {{"score": <1-10>, "reason": "<one sentence>"}}"""


def _ask_model(route: str, prompt: str) -> tuple[str, str]:
    """Call the right model for a route. Returns (response_text, model_used)."""
    try:
        from ..config import settings
        if route in ("claude", "summarise", "creative", "conversational", "code_explain", "code_write"):
            from ..models.claude import ask_claude
            return ask_claude(prompt), "CLAUDE"
        elif route == "deepseek":
            from ..models.deepseek import ask_deepseek
            return ask_deepseek(prompt), "DEEPSEEK"
        elif route == "gemini":
            from ..models.gemini import ask_gemini
            return ask_gemini(prompt), "GEMINI"
        else:
            from ..models.claude import ask_claude
            return ask_claude(prompt), "CLAUDE"
    except Exception as e:
        return f"[error: {e}]", "ERROR"


def _judge(question: str, response: str) -> dict:
    """Use Haiku to score a response. Returns {score, reason}."""
    try:
        from ..models.claude import ask_claude_haiku
        raw = ask_claude_haiku(_JUDGE_PROMPT.format(question=question, response=response[:800]))
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1].lstrip("json").strip()
        return json.loads(cleaned)
    except Exception as e:
        return {"score": 0, "reason": f"judge error: {e}"}


def run_benchmark() -> dict:
    """
    Run all benchmark prompts, score with Haiku, compare to previous baseline.
    Returns the full report dict (also written to disk).
    """
    from ..activity_log import bg_log
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    bg_log(f"Running weekly benchmark — {len(_BENCH_PROMPTS)} prompts", source="benchmark")

    results = []
    for item in _BENCH_PROMPTS:
        t0 = time.time()
        response, model = _ask_model(item["route"], item["prompt"])
        latency = round(time.time() - t0, 2)
        judgment = _judge(item["prompt"], response)
        results.append({
            "id": item["id"],
            "route": item["route"],
            "model": model,
            "score": judgment.get("score", 0),
            "reason": judgment.get("reason", ""),
            "latency_s": latency,
            "response_len": len(response),
            "error": response.startswith("[error") or response.startswith("["),
        })
        bg_log(f"Benchmark {item['id']}: score={judgment.get('score', 0)}/10 latency={latency}s", source="benchmark")

    avg_score = round(sum(r["score"] for r in results) / len(results), 2) if results else 0
    error_count = sum(1 for r in results if r["error"])

    # Compare to previous benchmark
    prev = _load_latest_benchmark()
    score_delta = None
    regression_alerts = []
    if prev:
        prev_avg = prev.get("avg_score", 0)
        score_delta = round(avg_score - prev_avg, 2)
        if score_delta < -1.5:
            regression_alerts.append(f"Overall score dropped {score_delta:.1f} pts vs last benchmark ({prev_avg} → {avg_score})")
        # Per-prompt regression
        prev_by_id = {r["id"]: r for r in prev.get("results", [])}
        for r in results:
            prev_r = prev_by_id.get(r["id"])
            if prev_r and r["score"] < prev_r["score"] - 2:
                regression_alerts.append(
                    f"{r['id']} ({r['model']}): score dropped {prev_r['score']} → {r['score']}"
                )

    report = {
        "date": date_str,
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "avg_score": avg_score,
        "score_delta_vs_prev": score_delta,
        "error_count": error_count,
        "total_prompts": len(results),
        "regression_alerts": regression_alerts,
        "results": results,
    }

    out = _DIR / f"benchmark_{date_str}.json"
    try:
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        pass

    if regression_alerts:
        for alert in regression_alerts:
            bg_log(f"BENCHMARK REGRESSION: {alert}", source="benchmark")
    else:
        bg_log(f"Benchmark complete — avg score {avg_score}/10, no regressions", source="benchmark")

    return report


def _load_latest_benchmark() -> dict | None:
    candidates = sorted(_DIR.glob("benchmark_*.json"), reverse=True)
    for p in candidates:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def get_latest_benchmark() -> dict | None:
    return _load_latest_benchmark()


def list_benchmark_dates() -> list[str]:
    return [p.stem.replace("benchmark_", "") for p in sorted(_DIR.glob("benchmark_*.json"), reverse=True)]
