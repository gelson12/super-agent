"""Build super-agent/n8n/bridge_ceo_bot.json.

The strategic leadership bot. Receives digests from Chief of Staff, evaluates
proposals from Researcher, reads security escalations from CSO, and issues
directives via memos back down. Handles urgent escalations via the webhook
entry (so CSO's 'urgent' memos get CEO attention within minutes, not hours).

Model: CLAUDE (strategic decisions, tradeoff resolution).
"""

from __future__ import annotations
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bridge_bot_skeleton import build_bot_workflow, write_workflow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = REPO_ROOT / "n8n" / "bridge_ceo_bot.json"


SYSTEM_PROMPT = """\
You are Bridge_CEO_BOT, the top-level strategic leadership super-agent inside Bridge Digital Solutions.

ROLE
You are the enterprise's highest-level strategic decision and alignment agent. You define direction, evaluate tradeoffs, approve priorities, arbitrate between competing initiatives, and keep the entire organization focused on growth, profit, resilience, and long-term advantage.

PRIMARY OBJECTIVES
1. Maximize enterprise value, profit, and strategic progress.
2. Set and refine company-wide priorities.
3. Decide where attention, capital, and effort should go.
4. Align all departments around the most valuable outcomes.
5. Protect the company from strategic drift, waste, and fragmentation.

SECONDARY OBJECTIVES
1. Evaluate new opportunities proposed by Research, Marketing, and Finance.
2. Balance current operations with future growth experiments.
3. Approve major changes in direction, allocation, or business model focus.
4. Maintain clarity of purpose across the multi-agent ecosystem.

CORE FUNCTIONS
Strategic prioritization, executive decision-making, opportunity evaluation, tradeoff resolution, resource direction, performance review, portfolio-level oversight, risk-reward balancing, long-range planning, leadership guidance.

WHAT YOU CARE ABOUT MOST
profitability, growth, capital efficiency, strategic leverage, execution quality, resilience, recurring revenue, high-margin opportunities, scalable systems, enterprise reputation.

DECISION PRINCIPLES
- Prioritize outcomes over activity.
- Favor profitable and scalable initiatives.
- Avoid fragmentation and low-value complexity.
- Support controlled experimentation when upside is meaningful.
- Require clarity, evidence, and operational feasibility.
- Make tradeoffs explicitly.

AUTHORITY AND AUTONOMY
You may approve or reject major initiatives, reprioritize departments, request enterprise summaries, escalate strategic pivots, instruct cross-functional focus changes, approve pilots/scaling/shutdowns.
You rely on Chief of Staff for execution coordination, Security authority for security-critical guidance, finance intelligence for economic viability, research intelligence for market insight.

INTERACTION RULES
You interact with Bridge_Chief_Of_Staff_bot as your main coordination proxy, Bridge_Researcher_bot for opportunity and market intelligence, Bridge_Chief_Sec_Off_bot for enterprise risk posture, financial and legal authorities for viability and protection.

ESCALATE OR INTERVENE WHEN priorities conflict materially, profit or budget performance deteriorates, a new idea shows major upside, strategic assumptions appear wrong, risk rises to leadership level, or departments are misaligned.

OUTPUT STYLE
Think and communicate like an executive: strategic, concise, decisive, high-signal, structured around priorities, decisions, tradeoffs, and expected outcomes.

SUCCESS DEFINITION
You are successful when the company consistently allocates effort toward the highest-value opportunities and converts strategy into profitable, sustainable growth.
"""


# Daily 16:00 exec summary cron.
DAILY_CRON = [
    {"hour": 16, "minute": 0, "task_name": "daily_exec_summary"},
]

# Monday 09:00 weekly priority alignment.
WEEKLY_CRON = [
    {"weekday": 1, "hour": 9, "minute": 0, "task_name": "weekly_priority_alignment"},
]


# CEO needs: top-level KPIs, all open high/urgent memos across the ecosystem,
# profit trend last 7d, capacity utilisation, Researcher proposals awaiting
# decision, security-flagged items.
CONTEXT_QUERY = r"""
SELECT jsonb_build_object(
    'now_utc', NOW(),
    'kpi_last_24h', (
        SELECT jsonb_build_object(
            'leads_created',     (SELECT COUNT(*)::int FROM bridge.leads WHERE created_at >= NOW() - INTERVAL '24 hours'),
            'demos_built',       (SELECT COUNT(*)::int FROM bridge.website_projects WHERE created_at >= NOW() - INTERVAL '24 hours'),
            'outreach_sent',     (SELECT COUNT(*)::int FROM bridge.outreach_messages WHERE direction='outbound' AND sent_at >= NOW() - INTERVAL '24 hours'),
            'interested_replies',(SELECT COUNT(*)::int FROM bridge.leads WHERE marketing_status='Interested' AND updated_at >= NOW() - INTERVAL '24 hours'),
            'invoices_paid',     (SELECT COUNT(*)::int FROM bridge.leads WHERE finance_status='Paid' AND updated_at >= NOW() - INTERVAL '24 hours'),
            'expenses_usd',      (SELECT ROUND(COALESCE(SUM(amount_usd),0)::numeric, 4) FROM bridge.expenses WHERE occurred_at >= NOW() - INTERVAL '24 hours')
        )
    ),
    'kpi_last_7d', (
        SELECT jsonb_build_object(
            'leads_created',     (SELECT COUNT(*)::int FROM bridge.leads WHERE created_at >= NOW() - INTERVAL '7 days'),
            'demos_built',       (SELECT COUNT(*)::int FROM bridge.website_projects WHERE created_at >= NOW() - INTERVAL '7 days'),
            'outreach_sent',     (SELECT COUNT(*)::int FROM bridge.outreach_messages WHERE direction='outbound' AND sent_at >= NOW() - INTERVAL '7 days'),
            'interested_replies',(SELECT COUNT(*)::int FROM bridge.leads WHERE marketing_status='Interested' AND updated_at >= NOW() - INTERVAL '7 days'),
            'invoices_paid',     (SELECT COUNT(*)::int FROM bridge.leads WHERE finance_status='Paid' AND updated_at >= NOW() - INTERVAL '7 days'),
            'expenses_usd',      (SELECT ROUND(COALESCE(SUM(amount_usd),0)::numeric, 4) FROM bridge.expenses WHERE occurred_at >= NOW() - INTERVAL '7 days')
        )
    ),
    'urgent_memos_across_ecosystem', (
        SELECT COALESCE(jsonb_agg(row_to_json(m)), '[]'::jsonb)
        FROM (
            SELECT memo_id, from_agent, to_agent, subject, priority, created_at, body_json
            FROM bridge.agent_memos
            WHERE status='open' AND priority IN ('urgent','high')
            ORDER BY priority, created_at
            LIMIT 15
        ) m
    ),
    'researcher_proposals_open', (
        SELECT COALESCE(jsonb_agg(row_to_json(p)), '[]'::jsonb)
        FROM (
            SELECT memo_id, subject, body_json, created_at
            FROM bridge.agent_memos
            WHERE from_agent='researcher' AND memo_type='proposal' AND status='open'
            ORDER BY created_at
            LIMIT 10
        ) p
    ),
    'campaign_priorities', (
        SELECT COALESCE(jsonb_agg(row_to_json(c)), '[]'::jsonb)
        FROM (
            SELECT campaign_target_id, niche, city, priority, active_flag,
                   daily_lead_target, daily_website_limit
            FROM bridge.campaign_targets
            ORDER BY priority DESC, niche, city
            LIMIT 20
        ) c
    )
) AS context;
"""


# CEO is allowed to issue directives (memos) and escalate, but should not
# directly modify schema or delete data. Campaign re-prioritisation is
# medium-risk — user-approved via Telegram.
ACTION_RISK_MAP = {
    "memo":     "low",     # directives down to CoS / Researcher / CSO
    "archive":  "low",     # close resolved items
    "no_op":    "low",
    "escalate": "low",     # already at the top, but leaves a clean audit trail
    "query":    "medium",  # CEO-requested ad-hoc SQL: user-approved
    "cleanup":  "high",    # CEO does not clean
}


def main() -> int:
    wf = build_bot_workflow(
        bot_name="ceo",
        workflow_display_name="bridge_ceo_bot",
        telegram_env_var="BRIDGE_CEO_BOT_TOKEN",
        telegram_cred_name="Bridge CEO Bot",
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
