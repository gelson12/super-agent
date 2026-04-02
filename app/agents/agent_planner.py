"""
Pre-execution intelligence layer — plugs into every tool-using agent.

Pipeline before any agent runs:
  1. COMPETE   — Claude Sonnet and DeepSeek each propose an execution plan
  2. ADJUDICATE — Claude Haiku picks the stronger plan with reasoning
  3. INJECT    — winning plan is prepended to the agent's user message

Self-healing loop (wraps the agent invoke):
  4. ON ERROR  — Claude diagnoses the failure
  5. CLASSIFY  — SAFE (auto-fix and retry) | CRITICAL (needs owner safe word)
  6. RETRY     — up to MAX_RETRIES with corrected approach
  7. ESCALATE  — if CRITICAL, return safe-word prompt to user

This module is model-agnostic — it wraps any callable agent function.
"""
import concurrent.futures
from ..models.claude import ask_claude, ask_claude_haiku
from ..models.deepseek import ask_deepseek
from ..learning.claude_code_worker import ask_claude_code, log_claude_code_result
from ..learning.insight_log import insight_log

MAX_RETRIES = 3


def extract_final_agent_text(result: dict) -> str:
    """
    Extract the final human-readable text response from a LangGraph agent result.

    Problem this solves: LangGraph AI messages alternate between tool-calling turns
    (content is a list with {"type":"tool_use",...} blocks mixed with {"type":"text",...})
    and final answer turns (content is a plain string or a list with only text blocks).
    The old code returned the first AI message in reverse — which is usually the LAST
    message chronologically — but that message sometimes still contained tool-use JSON,
    causing raw {"name":"shell","parameters":{...}} to bleed into the user-facing response.

    Fix: walk messages in reverse, skip any AI message that contains a tool_use block,
    return the first AI message that is pure text.
    """
    for msg in reversed(result.get("messages", [])):
        if not (hasattr(msg, "type") and msg.type in ("ai", "assistant")):
            continue
        content = msg.content
        if isinstance(content, list):
            # Skip intermediate tool-calling turns
            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            )
            if has_tool_use:
                continue
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            combined = "\n".join(t for t in texts if t).strip()
            if combined:
                return combined
        elif isinstance(content, str) and content.strip():
            return content.strip()
    return ""

# ── Prompt templates ──────────────────────────────────────────────────────────

_PLAN_PROMPT = """\
You are planning an agentic task. Produce the best possible execution plan.

Agent type : {agent_type}
Available tools: {tools}
Task: {task}

RUNTIME INFRASTRUCTURE (confirmed available on this Railway container):
  /workspace           — working directory for repos and builds
  /opt/flutter         — Flutter 3.27.4 SDK (/opt/flutter/bin/flutter)
  /opt/android-sdk     — Android SDK (platforms;android-34, build-tools;34.0.0)
  GitHub PAT           — configured for github.com/gelson12/*
  clone_repo tool      — clones any public/private gelson12 repo into /workspace
  APK output path      — <project>/build/app/outputs/flutter-apk/app-debug.apk

Structure your plan as:
GOAL: one sentence
PHASES:
  1. [phase name] — [what to do, with absolute paths] — [expected outcome]
  2. ...
RISKS:
  - [what could go wrong] → [fallback if it does]
CONSTRAINTS:
  - [any limits: size, permissions, ordering dependencies]

Be specific and practical. Use absolute paths. Divide into ≤5 phases."""

_ADJUDICATE_PROMPT = """\
Two AI models proposed competing execution plans for the same agentic task.
Evaluate both critically and select the stronger one.

Task: {task}

── Plan A (Claude) ──────────────────────────────────────────────────────────
{plan_a}

── Plan B (DeepSeek) ────────────────────────────────────────────────────────
{plan_b}

Reply in this exact format:
WINNER: [A or B]
REASON: [2 sentences — why this plan is stronger]
PLAN:
[copy the winning plan here verbatim, unchanged]"""

_ADJUDICATE_PROMPT_N = """\
Multiple AI models proposed execution plans for the same agentic task.
Your job is NOT to pick one — synthesize the BEST plan by taking the
strongest elements from each model's proposal.

Task: {task}

{plans}

Reply in this exact format:
SYNTHESIS_RATIONALE: [2 sentences — what you took from each plan and why]
PLAN:
[The synthesized plan — combine the best phases, risks, and constraints.
 Do not copy any single plan verbatim. Merge and improve.]"""

# Keywords that indicate a code/file task — triggers Claude Code as 3rd competitor
_CODE_KEYWORDS = {
    "fix", "debug", "code", "file", "repo", "error", "bug", "refactor",
    "function", "class", "import", "test", "script", "deploy", "build",
}


def _is_code_task(task: str) -> bool:
    lower = task.lower()
    return any(k in lower for k in _CODE_KEYWORDS)


def _parse_winner_label(adjudication: str) -> str:
    """Extract the WINNER label (A, B, or C) from adjudication text."""
    for line in adjudication.splitlines():
        if line.strip().startswith("WINNER:"):
            val = line.split(":", 1)[1].strip()
            for label in ["C", "B", "A"]:  # prefer C (Claude Code) if tied
                if label in val:
                    return label
    return "A"


def _extract_plan_section(adjudication: str) -> str | None:
    """Extract the PLAN: section from synthesis or adjudication output."""
    if "PLAN:" in adjudication:
        return adjudication.split("PLAN:", 1)[1].strip()
    return None


_HEAL_PROMPT = """\
An AI agent failed during task execution. Diagnose the failure and prescribe a fix.

Agent     : {agent_type}
Task      : {task}
Attempt   : {attempt} of {max_retries}
Error     : {error}
Context   : {context}

Reply in this exact format — each field on its own line:
DIAGNOSIS: [one sentence — root cause]
SEVERITY: [SAFE or CRITICAL]
  SAFE     = can auto-fix without user approval (syntax fix, smaller step, different approach)
  CRITICAL = requires owner safe-word (deletes data, overwrites production, irreversible action)
FIX: [specific corrective action]
REVISED_APPROACH: [if SAFE — the full revised instruction for the next attempt; if CRITICAL — leave blank]"""

# ── Planning ──────────────────────────────────────────────────────────────────

def compete_and_plan(task: str, agent_type: str, tools: list[str]) -> str:
    """
    Run Claude, DeepSeek, and (for code tasks) Claude Code CLI in parallel.
    Haiku synthesizes the best plan from all available proposals.

    DeepSeek is automatically skipped if its win rate falls below 40%
    (learned from insight_log — Feature 8 feedback loop).

    Claude Code CLI (Plan C) is only spawned for code/file/debug tasks
    because it has file-system access irrelevant to general queries.
    """
    prompt = _PLAN_PROMPT.format(
        agent_type=agent_type,
        tools=", ".join(tools),
        task=task,
    )

    # Feature 8: skip consistently underperforming models
    win_rates = insight_log.get_model_win_rates()
    use_deepseek = win_rates.get("DEEPSEEK", 1.0) >= 0.40
    use_claude_code = _is_code_task(task)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        future_a = pool.submit(ask_claude, prompt)
        future_b = pool.submit(ask_deepseek, prompt) if use_deepseek else None
        future_c = pool.submit(ask_claude_code, prompt) if use_claude_code else None

        plan_a = future_a.result()
        plan_b = future_b.result() if future_b else None
        plan_c = future_c.result() if future_c else None

    a_failed = plan_a.startswith("[")
    b_failed = (not plan_b) or plan_b.startswith("[")
    c_failed = (not plan_c) or plan_c.startswith("[")

    # Build list of available (non-failed) plans with labels
    available = [
        (plan, label)
        for plan, label, failed in [
            (plan_a, "A", a_failed),
            (plan_b, "B", b_failed),
            (plan_c, "C", c_failed),
        ]
        if not failed
    ]

    if not available:
        return f"Execute directly: {task}"
    if len(available) == 1:
        return available[0][0]

    # Feature 6: Haiku synthesizes the best plan (not just picks a winner)
    plans_block = "\n\n".join(
        f"── Plan {label} ──────────────────────────────────────────────────────────\n{plan}"
        for plan, label in available
    )
    adj_prompt = _ADJUDICATE_PROMPT_N.format(task=task, plans=plans_block)

    try:
        adjudication = ask_claude_haiku(adj_prompt)

        # Log Claude Code contribution whenever it participated
        if plan_c and not c_failed:
            log_claude_code_result(prompt, plan_c, True, agent_type)

        plan_section = _extract_plan_section(adjudication)
        return plan_section if plan_section else available[0][0]
    except Exception:
        return available[0][0]


# ── Self-healing ──────────────────────────────────────────────────────────────

def diagnose_and_heal(
    task: str,
    agent_type: str,
    error: str,
    context: str,
    attempt: int,
) -> dict:
    """
    Diagnose an agent error and classify the fix.

    Returns dict with keys:
      severity          : "SAFE" | "CRITICAL"
      diagnosis         : str
      fix               : str
      revised_approach  : str   (non-empty only when SAFE)
      safe_word_prompt  : str   (non-empty only when CRITICAL)
    """
    prompt = _HEAL_PROMPT.format(
        agent_type=agent_type,
        task=task,
        attempt=attempt,
        max_retries=MAX_RETRIES,
        error=error,
        context=context,
    )

    result = {
        "severity": "SAFE",
        "diagnosis": "Unknown error",
        "fix": "Retry with a smaller step",
        "revised_approach": task,
        "safe_word_prompt": "",
    }

    try:
        response = ask_claude(prompt)
    except Exception as e:
        result["diagnosis"] = f"Could not diagnose: {e}"
        return result

    for line in response.strip().splitlines():
        line = line.strip()
        if line.startswith("DIAGNOSIS:"):
            result["diagnosis"] = line.split(":", 1)[1].strip()
        elif line.startswith("SEVERITY:"):
            sev = line.split(":", 1)[1].strip().upper()
            result["severity"] = "CRITICAL" if "CRITICAL" in sev else "SAFE"
        elif line.startswith("FIX:"):
            result["fix"] = line.split(":", 1)[1].strip()
        elif line.startswith("REVISED_APPROACH:"):
            result["revised_approach"] = line.split(":", 1)[1].strip()

    if result["severity"] == "CRITICAL":
        result["revised_approach"] = ""
        result["safe_word_prompt"] = (
            f"\u26a0\ufe0f  **Critical fix required — owner authorization needed.**\n\n"
            f"**Diagnosis:** {result['diagnosis']}\n"
            f"**Proposed fix:** {result['fix']}\n\n"
            f"Reply with your safe word to authorize this action, or say **cancel** to abort."
        )

    return result


# ── Agent wrapper ─────────────────────────────────────────────────────────────

def run_with_plan_and_recovery(
    agent_fn,
    message: str,
    agent_type: str,
    tool_names: list[str],
    **agent_kwargs,
) -> str:
    """
    Wrap any agent function with the full intelligence pipeline:
      1. Compete + plan
      2. Execute with plan as context
      3. Self-heal on failure (up to MAX_RETRIES)
      4. Safe-word prompt on CRITICAL failures

    agent_fn must accept (message: str, **agent_kwargs) → str.
    """
    # Phase 1 & 2: compete and plan
    plan = compete_and_plan(message, agent_type, tool_names)

    augmented = (
        f"[EXECUTION PLAN — pre-validated by model competition]\n"
        f"{plan}\n"
        f"{'─' * 60}\n"
        f"TASK: {message}\n\n"
        f"Execute the plan above phase by phase. "
        f"If a phase fails, adapt: try a smaller step, fix the error, and continue."
    )

    last_error = ""
    context = "initial attempt"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return agent_fn(augmented, **agent_kwargs)
        except Exception as e:
            last_error = str(e)

            if attempt == MAX_RETRIES:
                break

            heal = diagnose_and_heal(
                task=message,
                agent_type=agent_type,
                error=last_error,
                context=context,
                attempt=attempt,
            )

            if heal["severity"] == "CRITICAL":
                return heal["safe_word_prompt"]

            # SAFE: rebuild augmented message with diagnosis + revised approach
            context = f"attempt {attempt} error: {last_error} | fix: {heal['fix']}"
            revised = heal["revised_approach"] or message
            augmented = (
                f"[SELF-HEALING RETRY — attempt {attempt + 1}/{MAX_RETRIES}]\n\n"
                f"Diagnosis: {heal['diagnosis']}\n"
                f"Fix applied: {heal['fix']}\n"
                f"{'─' * 60}\n"
                f"REVISED TASK: {revised}\n\n"
                f"Execute carefully. Break into smaller steps. "
                f"If still failing, try the minimal viable version first."
            )

    return (
        f"[Agent failed after {MAX_RETRIES} attempts]\n"
        f"Last error: {last_error}\n"
        f"Diagnosis: run diagnostics or check logs for root cause."
    )
