"""Build super-agent/n8n/bridge_chief_of_staff_bot.json.

Chief of Staff = central coordinator of the executive-agent layer. Runs the
most cadences (day-open / midday / day-close + weekly profit review +
weekly strategic summary) and reads every other agent's inbox to build a
coherent picture for the CEO and the user.

Model: CLAUDE (strategic reasoning, synthesis, SLA arbitration).
"""

from __future__ import annotations
from pathlib import Path
import sys

# Allow this script to import the sibling helper regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bridge_bot_skeleton import build_bot_workflow, write_workflow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = REPO_ROOT / "n8n" / "bridge_chief_of_staff_bot.json"


SYSTEM_PROMPT = """\
You are Bridge_Chief_Of_Staff_bot, the executive orchestration and cross-functional coordination super-agent inside Bridge Digital Solutions.

ROLE
You are the central execution coordinator for the enterprise. You translate strategy into action, keep departments aligned, track dependencies, surface blockers, enforce follow-through, and maintain operational rhythm across the multi-agent ecosystem.

PRIMARY OBJECTIVES
1. Turn executive goals into coordinated action.
2. Keep all super-agents aligned to priorities, deadlines, and dependencies.
3. Prevent drift, duplication, delay, and confusion.
4. Maintain operational accountability across the organization.
5. Make sure the right information reaches the right agent at the right time.

SECONDARY OBJECTIVES
1. Summarize state across the business for leadership.
2. Escalate blockers, risks, and missed deadlines early.
3. Improve execution speed, coordination quality, and clarity.
4. Support budget-aware prioritization with leadership and finance.

CORE FUNCTIONS
1. Cross-agent coordination
2. Task routing and follow-up
3. Priority tracking
4. Dependency management
5. Meeting orchestration
6. Decision logging
7. Action-item enforcement
8. Executive summarization
9. Escalation handling
10. Operational rhythm management

WHAT YOU MUST DO
- maintain visibility over all major initiatives
- know which agent owns which task
- know what is blocked, overdue, or waiting for input
- push unresolved items toward a decision
- ensure inter-agent communication happens in correct sequence
- request status updates when needed
- summarize complex situations for leadership
- coordinate daily and weekly operating cadence

DECISION PRINCIPLES
- Optimize for clarity, speed, alignment, and execution.
- Do not allow silent drift.
- Convert vague goals into concrete next actions.
- Keep teams focused on high-priority and high-impact outcomes.
- Escalate early, not late.
- Prefer decisive coordination over passive observation.

AUTHORITY AND AUTONOMY
You may: request updates from any agent, consolidate reports, schedule check-ins and review cycles, re-surface unresolved issues, recommend reprioritization, escalate blockers to the CEO.
You may not: override core security decisions, override final strategic decisions from the CEO, override hard budget constraints from finance — unless explicitly delegated.

INTERACTION RULES
You work closely with Bridge_CEO_BOT for strategic direction, Bridge_Researcher_bot for intelligence intake, Bridge_Chief_Sec_Off_bot for risk visibility, operational/financial agents for project movement, and Bridge_Cleaner_bot for cleanup cycles.

ESCALATE WHEN priorities conflict, multiple agents are blocked, deadlines slip, budgets constrain execution, risk rises materially, decisions are pending too long, or coordination failure is likely.

OUTPUT STYLE
Use concise executive language. Always organize outputs as: current state, blockers, required decisions, next actions, owners, deadlines.

SUCCESS DEFINITION
You are successful when the company stays aligned, blockers are resolved early, and important work moves reliably from strategy to execution.
"""


# Cadences from the master scheduler (UTC).
DAILY_CRON = [
    {"hour": 8,  "minute": 0,  "task_name": "day_open_alignment"},
    {"hour": 12, "minute": 30, "task_name": "midday_blocker_review"},
    {"hour": 20, "minute": 0,  "task_name": "day_close_reconciliation"},
]

# Thursday 10:00 profit/opportunity review + Friday 18:00 weekly strategic summary.
WEEKLY_CRON = [
    {"weekday": 4, "hour": 10, "minute": 0, "task_name": "weekly_profit_review"},
    {"weekday": 5, "hour": 18, "minute": 0, "task_name": "weekly_strategic_summary"},
]


# Context the Chief of Staff needs every run: queue counts, SLA breaches,
# active memos across *all* agents (not just its own inbox), expenses today.
CONTEXT_QUERY = r"""
SELECT jsonb_build_object(
    'now_utc', NOW(),
    'queue_counts', (
        SELECT jsonb_build_object(
            'research_queue',   (SELECT COUNT(*)::int FROM bridge.leads WHERE research_status IN ('New','Researched','Needs Review')),
            'website_queue',    (SELECT COUNT(*)::int FROM bridge.leads WHERE website_status IN ('Queued','In Progress','Draft Ready')),
            'marketing_queue',  (SELECT COUNT(*)::int FROM bridge.leads WHERE marketing_status IN ('Queued','Awaiting Reply')),
            'finance_queue',    (SELECT COUNT(*)::int FROM bridge.leads WHERE finance_status IN ('Queued','Invoice Sent','Awaiting Payment'))
        )
    ),
    'sla_breaches', (
        SELECT jsonb_build_object(
            'research_over_48h',  (SELECT COUNT(*)::int FROM bridge.leads
                                   WHERE research_status IN ('Qualified','Needs Review')
                                     AND created_at < NOW() - INTERVAL '48 hours'),
            'website_over_2h',    (SELECT COUNT(*)::int FROM bridge.leads
                                   WHERE website_status = 'In Progress'
                                     AND updated_at < NOW() - INTERVAL '2 hours'),
            'outreach_over_72h',  (SELECT COUNT(*)::int FROM bridge.leads
                                   WHERE marketing_status = 'Awaiting Reply'
                                     AND updated_at < NOW() - INTERVAL '72 hours'),
            'invoice_over_14d',   (SELECT COUNT(*)::int FROM bridge.leads
                                   WHERE finance_status = 'Invoice Sent'
                                     AND updated_at < NOW() - INTERVAL '14 days')
        )
    ),
    'memos_by_agent_24h', (
        SELECT COALESCE(jsonb_agg(row_to_json(v)), '[]'::jsonb)
        FROM (
            SELECT from_agent, to_agent, priority, memo_type, n
            FROM bridge.v_agent_memo_24h
        ) v
    ),
    'expenses_today_usd', (
        SELECT ROUND(COALESCE(SUM(amount_usd), 0)::numeric, 4)
        FROM bridge.expenses
        WHERE occurred_at >= date_trunc('day', NOW())
    ),
    'leads_touched_today', (
        SELECT COUNT(*)::int FROM bridge.workflow_events
        WHERE created_at >= date_trunc('day', NOW())
    )
) AS context;
"""


# Which action types Chief of Staff is allowed to auto-execute.
# CoS coordinates — it writes memos, archives closed ones, posts briefings.
# It does NOT delete data, modify schemas, or rotate credentials.
ACTION_RISK_MAP = {
    "memo":     "low",     # route work between agents
    "archive":  "low",     # close resolved memos addressed to me
    "no_op":    "low",
    "escalate": "low",     # raise urgent memo to CEO
    "query":    "medium",  # ad-hoc SQL: approve before running
    "cleanup":  "high",    # CoS should not be deleting content
}


def main() -> int:
    wf = build_bot_workflow(
        bot_name="chief_of_staff",
        workflow_display_name="bridge_chief_of_staff_bot",
        telegram_env_var="BRIDGE_CHIEF_OF_STAFF_BOT_TOKEN",
        telegram_cred_name="Bridge Chief of Staff Bot",
        system_prompt=SYSTEM_PROMPT,
        llm_model="CLAUDE",
        daily_cron=DAILY_CRON,
        weekly_cron=WEEKLY_CRON,
        context_query=CONTEXT_QUERY.strip(),
        action_risk_map=ACTION_RISK_MAP,
    )
    write_workflow(wf, OUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
