"""Build super-agent/n8n/bridge_cleaner_bot.json.

Lifecycle hygiene bot. Owns the 12-hour demo TTL sweep (which was planned but
never built into bridge_marketing.json — the Cleaner now owns it from the
start). Daily 22:00 nightly cleanup; Friday 18:00 weekly archive cycle.

Distinguishing behaviour: the CONTEXT_QUERY runs reversible UPDATEs (sets
archived_at = NOW() on stale rows) inside a CTE — the LLM sees what was just
archived and reports it. Actual destructive GitHub file deletion stays
medium-risk (user-approved via Telegram); the Cleaner never deletes files
without explicit approval.

Model: GEMINI (cheap pattern-matching, no strategic reasoning needed).
"""

from __future__ import annotations
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bridge_bot_skeleton import build_bot_workflow, write_workflow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = REPO_ROOT / "n8n" / "bridge_cleaner_bot.json"


SYSTEM_PROMPT = """\
You are Bridge_Cleaner_bot, the enterprise maintenance, hygiene, reset, and lifecycle management super-agent inside Bridge Digital Solutions.

ROLE
You are responsible for keeping the ecosystem clean, orderly, efficient, and recoverable. You remove clutter, archive stale items, recycle temporary resources, reset expired states, and preserve operational hygiene across workflows, data, storage, environments, and temporary assets.

PRIMARY OBJECTIVES
1. Keep the enterprise free from stale, expired, duplicated, or wasteful operational residue.
2. Recycle temporary resources efficiently.
3. Improve system clarity, performance, and cost discipline through maintenance hygiene.
4. Support lifecycle completion after campaigns, workflows, builds, meetings, and project stages.

SECONDARY OBJECTIVES
1. Prevent resource leakage and state confusion.
2. Support finance and PM by reducing unnecessary operational overhead.
3. Maintain archival discipline and closure integrity.

CORE FUNCTIONS
Archive stale records, reset expired states, recycle temporary resources, remove duplicate or abandoned artifacts, close completed maintenance loops, clean demo-site and temporary deployment residue, keep storage and workflow states organized, mark unresolved clutter for escalation, support end-of-day and end-of-week cleanup cycles.

WHAT YOU MUST CLEAN OR MANAGE
expired demo websites, temporary deployment resources, stale queue items, abandoned drafts, duplicated temporary files, old cache-like workflow residues, inactive or expired work states, no-longer-needed artifacts after PM or finance decisions, archival routing for historical traceability.

DECISION PRINCIPLES
- Do not delete what should be archived.
- Do not archive what is still active.
- Prefer recoverable cleanup over destructive cleanup.
- Respect security, legal, and finance retention rules.
- Improve clarity, reduce waste, and preserve traceability.

AUTHORITY AND AUTONOMY
You may archive expired or approved-for-closure assets, reset stale states according to policy, reclaim temporary resources, flag clutter and maintenance risks, request PM review for ambiguous cleanup cases.
You may not destroy protected data without approved policy, remove legally or financially important records, override active project ownership without escalation.

INTERACTION RULES
You work with Bridge_Chief_Of_Staff_bot for cleanup timing and closure authority, Bridge_CEO_BOT only through escalated operational summaries when waste becomes strategic, Bridge_Chief_Sec_Off_bot for secure cleanup and retention compliance, finance and project workflows for budget-saving resource reclamation.

ESCALATE WHEN an item is stale but ownership is ambiguous, cleanup might affect legal/financial/security obligations, resource leakage is recurring, multiple abandoned states indicate process failure, or temporary assets are consuming unnecessary budget.

OUTPUT STYLE
Always organize outputs as: items cleaned, items archived, items recycled, items needing approval, risks from clutter, savings or efficiency impact.

NOTE FOR THIS RUNTIME
The CONTEXT_QUERY you receive already auto-archives stale website_projects rows older than 12 hours whose lead is not 'Interested'. Reflect this in your reply_text (count and a short list of slugs). For destructive GitHub file deletion of those archived demos, emit a 'cleanup' action with payload {slug, reason} — that goes through user approval, never auto-executes.

SUCCESS DEFINITION
You are successful when the enterprise stays lean, orderly, cost-efficient, and free from stale operational buildup.
"""


# Daily 22:00 nightly cleanup.
DAILY_CRON = [
    {"hour": 22, "minute": 0, "task_name": "nightly_cleanup"},
]

# Friday 18:00 weekly archive cycle.
WEEKLY_CRON = [
    {"weekday": 5, "hour": 18, "minute": 0, "task_name": "weekly_archive_cycle"},
]


# Cleaner CONTEXT_QUERY: auto-archives stale website_projects + expired memos
# (reversible UPDATEs inside a CTE), then surfaces what was just archived to
# the LLM. Also reports remaining clutter that needs human judgement.
CONTEXT_QUERY = r"""
WITH
ttl_demos AS (
    UPDATE bridge.website_projects p
       SET archived_at = NOW()
      FROM bridge.leads l
     WHERE p.lead_id = l.lead_id
       AND p.archived_at IS NULL
       AND p.created_at < NOW() - INTERVAL '12 hours'
       AND COALESCE(l.marketing_status, '') NOT IN ('Interested')
    RETURNING p.website_project_id, p.slug, p.lead_id
),
expired_memos AS (
    UPDATE bridge.agent_memos m
       SET status = 'expired'
      WHERE m.status = 'open'
        AND m.priority IN ('low','normal')
        AND m.created_at < NOW() - INTERVAL '14 days'
    RETURNING m.memo_id, m.subject, m.from_agent, m.to_agent
)
SELECT jsonb_build_object(
    'now_utc', NOW(),
    'ttl_demos_archived_this_run', (
        SELECT COALESCE(jsonb_agg(row_to_json(t)), '[]'::jsonb) FROM ttl_demos t
    ),
    'memos_expired_this_run', (
        SELECT COALESCE(jsonb_agg(row_to_json(e)), '[]'::jsonb) FROM expired_memos e
    ),
    'pending_demo_deletions', (
        SELECT COALESCE(jsonb_agg(row_to_json(d)), '[]'::jsonb)
        FROM (
            SELECT slug, lead_id, archived_at
            FROM bridge.website_projects
            WHERE archived_at IS NOT NULL
              AND archived_at >= NOW() - INTERVAL '24 hours'
            ORDER BY archived_at DESC
            LIMIT 30
        ) d
    ),
    'queue_residue', (
        SELECT jsonb_build_object(
            'leads_in_queue_over_7d', (
                SELECT COUNT(*)::int FROM bridge.leads
                WHERE research_status = 'Queued' AND updated_at < NOW() - INTERVAL '7 days'
            ),
            'website_in_progress_over_24h', (
                SELECT COUNT(*)::int FROM bridge.leads
                WHERE website_status = 'In Progress' AND updated_at < NOW() - INTERVAL '24 hours'
            ),
            'awaiting_reply_over_30d', (
                SELECT COUNT(*)::int FROM bridge.leads
                WHERE marketing_status = 'Awaiting Reply' AND updated_at < NOW() - INTERVAL '30 days'
            )
        )
    ),
    'archivable_completed_leads_over_90d', (
        SELECT COUNT(*)::int FROM bridge.leads
        WHERE project_status IN ('Completed','Archived')
          AND updated_at < NOW() - INTERVAL '90 days'
    ),
    'duplicate_workflow_events_last_24h', (
        SELECT COUNT(*)::int FROM (
            SELECT workflow_name, event_type, COUNT(*) AS n
            FROM bridge.workflow_events
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY workflow_name, event_type, lead_id
            HAVING COUNT(*) > 5
        ) dups
    )
) AS context;
"""


# Cleaner risk map: it CAN auto-execute reversible cleanup (already done in
# CONTEXT_QUERY's CTE). Destructive github_delete_demo stays medium-risk so
# the user approves before files leave the repo.
ACTION_RISK_MAP = {
    "memo":     "low",     # cleanup summaries to CoS
    "archive":  "low",     # close its own resolved memos
    "no_op":    "low",
    "escalate": "low",     # ambiguous ownership -> CEO
    "query":    "medium",  # ad-hoc SQL: user-approved
    "cleanup":  "medium",  # destructive GitHub file deletion: user-approved
}


def main() -> int:
    wf = build_bot_workflow(
        bot_name="cleaner",
        workflow_display_name="bridge_cleaner_bot",
        telegram_env_var="BRIDGE_CLEANER_BOT_TOKEN",
        telegram_cred_name="Bridge Cleaner Bot",
        system_prompt=SYSTEM_PROMPT,
        llm_model="GEMINI",
        daily_cron=DAILY_CRON,
        weekly_cron=WEEKLY_CRON,
        context_query=CONTEXT_QUERY.strip(),
        action_risk_map=ACTION_RISK_MAP,
    )
    write_workflow(wf, OUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
