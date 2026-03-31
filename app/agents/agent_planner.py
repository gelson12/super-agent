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

MAX_RETRIES = 3

# ── Prompt templates ──────────────────────────────────────────────────────────

_PLAN_PROMPT = """\
You are planning an agentic task. Produce the best possible execution plan.

Agent type : {agent_type}
Available tools: {tools}
Task: {task}

Structure your plan as:
GOAL: one sentence
PHASES:
  1. [phase name] — [what to do] — [expected outcome]
  2. ...
RISKS:
  - [what could go wrong] → [fallback if it does]
CONSTRAINTS:
  - [any limits: size, permissions, ordering dependencies]

Be specific and practical. If the task is large, divide it into ≤5 phases of manageable size."""

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
    Run Claude and DeepSeek in parallel, Haiku adjudicates.
    Returns the winning execution plan as a plain string.
    Falls back gracefully if either model is unavailable.
    """
    prompt = _PLAN_PROMPT.format(
        agent_type=agent_type,
        tools=", ".join(tools),
        task=task,
    )

    # Run both models in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(ask_claude, prompt)
        future_b = pool.submit(ask_deepseek, prompt)
        plan_a = future_a.result()
        plan_b = future_b.result()

    a_failed = plan_a.startswith("[")
    b_failed = plan_b.startswith("[")

    if a_failed and b_failed:
        # Both unavailable — proceed without a pre-plan
        return f"Execute directly: {task}"
    if a_failed:
        return plan_b
    if b_failed:
        return plan_a

    # Adjudicate with Haiku (fast, cheap)
    try:
        adjudication = ask_claude_haiku(
            _ADJUDICATE_PROMPT.format(task=task, plan_a=plan_a, plan_b=plan_b)
        )
        if "PLAN:" in adjudication:
            return adjudication.split("PLAN:", 1)[1].strip()
        return adjudication
    except Exception:
        return plan_a  # Default to Claude's plan on adjudication failure


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
