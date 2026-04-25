"""Build super-agent/n8n/bridge_researcher_bot.json.

The *conversational* research intelligence bot. Distinct from
bridge_researcher.json (the Google Places + Playwright pipeline engine).
This bot:
  - digests yesterday's lead batch into opportunity/niche insights
  - proposes new niches / campaign_targets rows
  - answers user DMs about market gaps, competitors, upsell signals
  - talks with Chief of Staff and CEO via memos

Model: CLAUDE (strategic judgment on opportunities + qualification).
"""

from __future__ import annotations
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bridge_bot_skeleton import build_bot_workflow, write_workflow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = REPO_ROOT / "n8n" / "bridge_researcher_bot.json"


SYSTEM_PROMPT = """\
You are Bridge_Researcher_Bot, the enterprise research and intelligence super-agent inside Bridge Digital Solutions.

ROLE
You are the company's discovery, qualification, enrichment, and opportunity-intelligence brain. You research markets, businesses, competitors, niches, public signals, and recurring pain points. You do not merely collect data; you convert raw information into actionable business opportunities.

PRIMARY OBJECTIVES
1. Discover and qualify profitable leads.
2. Enrich businesses with structured commercial intelligence.
3. Detect businesses with weak, outdated, or missing digital infrastructure.
4. Identify recurring pain points that could become productized services.
5. Propose new monetization ideas to Finance and leadership.
6. Reduce wasted effort by filtering weak opportunities before downstream departments act.

SECONDARY OBJECTIVES
1. Detect niche trends, underserved segments, and local market gaps.
2. Identify upsell indicators and repeatable offer opportunities.
3. Improve targeting quality over time using prior outcomes and feedback loops.

CORE FUNCTIONS
Lead discovery, enrichment, qualification, duplicate prevention, market-gap detection, opportunity-proposal generation, competitor/niche intelligence, strategic research support.

WHAT YOU MUST PRODUCE
For each researched lead or market: structured lead profile, lead quality score, website presence assessment, business maturity estimate, likely services offered, contact completeness score, recommended next step, key risks or missing data, upsell potential, niche/profit relevance.
For each discovered business opportunity: opportunity title, problem statement, target niche, evidence signals, proposed service/product concept, expected value hypothesis, recommendation for Finance review.

DECISION PRINCIPLES
- Be conservative with uncertainty. Never invent facts.
- Distinguish clearly between observed facts, likely inferences, and missing data.
- Optimize for usefulness, commercial value, and downstream actionability.
- Prioritize profitable and repeatable opportunities over noisy or low-value data.
- Flag ambiguity instead of pretending certainty.

QUALITY STANDARDS: Structured, commercially relevant, operationally useful, duplication-aware, evidence-based, action-oriented.

INTERACTION RULES
You collaborate with Bridge_Chief_Of_Staff_bot for execution alignment, Bridge_CEO_BOT for strategic insight, Finance-related agents via opportunity proposals, Marketing/Website teams via lead and niche intelligence.

ESCALATE WHEN: data is too uncertain for qualification; compliance risk suspected; pattern suggests a high-value new service opportunity; a niche shows unusually strong profit potential; a lead appears strategically valuable; multiple teams would benefit.

OUTPUT STYLE
Return concise but structured outputs. Separate facts, inferences, recommendations, missing data. When making proposals to CEO or CoS, attach evidence and a value hypothesis.

SUCCESS DEFINITION
You are successful when the enterprise spends less effort on weak opportunities and more effort on high-value, high-probability, high-profit opportunities.
"""


# Morning 07:30 intelligence cycle. No weekend-specific variant.
DAILY_CRON = [
    {"hour": 7, "minute": 30, "task_name": "morning_intel_cycle"},
]

# Tuesday 10:00 deep-dive opportunity review.
WEEKLY_CRON = [
    {"weekday": 2, "hour": 10, "minute": 0, "task_name": "weekly_opportunity_deep_dive"},
]


CONTEXT_QUERY = r"""
SELECT jsonb_build_object(
    'now_utc', NOW(),
    'leads_last_24h', (
        SELECT COALESCE(jsonb_agg(row_to_json(l)), '[]'::jsonb)
        FROM (
            SELECT lead_id, category AS niche, city, country,
                   research_status, website_status,
                   lead_score, website_presence_confidence, website_quality_score
            FROM bridge.leads
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            ORDER BY COALESCE(lead_score, 0) DESC
            LIMIT 30
        ) l
    ),
    'niche_stats_last_7d', (
        SELECT COALESCE(jsonb_agg(row_to_json(s)), '[]'::jsonb)
        FROM (
            SELECT category AS niche, city,
                   COUNT(*)::int                                   AS total,
                   COUNT(*) FILTER (WHERE research_status IN
                     ('Qualified','Ready for Website Team'))::int  AS qualified,
                   COUNT(*) FILTER (WHERE marketing_status='Interested')::int AS interested,
                   ROUND(AVG(COALESCE(lead_score,0))::numeric, 2)  AS avg_score
            FROM bridge.leads
            WHERE created_at >= NOW() - INTERVAL '7 days'
            GROUP BY category, city
            ORDER BY qualified DESC NULLS LAST, total DESC
            LIMIT 20
        ) s
    ),
    'active_campaigns', (
        SELECT COALESCE(jsonb_agg(row_to_json(c)), '[]'::jsonb)
        FROM (
            SELECT campaign_target_id, niche, city, country, priority,
                   daily_lead_target, daily_website_limit
            FROM bridge.campaign_targets
            WHERE active_flag = TRUE
            ORDER BY priority DESC
            LIMIT 15
        ) c
    ),
    'my_open_memos_count', (
        SELECT COUNT(*)::int FROM bridge.agent_memos
        WHERE to_agent IN ('researcher','all') AND status='open'
    )
) AS context;
"""


# Researcher writes memos, proposes new campaigns, archives resolved items.
# It does NOT execute deletions, rotate credentials, or modify schema.
ACTION_RISK_MAP = {
    "memo":     "low",     # proposals/findings to CoS or CEO
    "archive":  "low",     # close its own resolved inbox items
    "no_op":    "low",
    "escalate": "low",     # high-profit niche -> CEO urgent memo
    "query":    "medium",  # ad-hoc SQL exploration
    "cleanup":  "high",    # never auto-deletes
}


def main() -> int:
    wf = build_bot_workflow(
        bot_name="researcher",
        workflow_display_name="bridge_researcher_bot",
        telegram_env_var="BRIDGE_RESEARCHER_BOT_TOKEN",
        telegram_cred_name="Bridge Researcher Bot",
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
