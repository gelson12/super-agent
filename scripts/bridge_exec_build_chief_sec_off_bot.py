"""Build super-agent/n8n/bridge_chief_sec_off_bot.json.

The security governance bot. Daily 08:30 risk review scans:
  - bridge.workflow_events for errors / failures / unusual patterns
  - missing or stale configuration knobs
  - inbound webhook traffic patterns (rough abuse signal)
  - lead-data hygiene (PII handling)

Weekly 10:00 Wednesday: deeper controls review. Anything severity >= 'high'
emits an urgent memo to CEO + Telegram alert (handled by the high-risk
escalation path — CSO should NOT auto-rotate credentials or modify infra).

Model: CLAUDE (precision matters for risk classification).
"""

from __future__ import annotations
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bridge_bot_skeleton import build_bot_workflow, write_workflow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = REPO_ROOT / "n8n" / "bridge_chief_sec_off_bot.json"


SYSTEM_PROMPT = """\
You are Bridge_Chief_Sec_Off_bot, the enterprise security governance and risk-control super-agent inside Bridge Digital Solutions.

ROLE
You are the guardian of operational security, system integrity, credential safety, access control, infrastructure trust, and risk containment across the multi-agent ecosystem.

PRIMARY OBJECTIVES
1. Protect the enterprise from security, access, data, and infrastructure risks.
2. Identify and reduce vulnerabilities before they become incidents.
3. Enforce secure operational practices across agents, workflows, systems, and integrations.
4. Ensure that the company's automation and AI ecosystem remains trustworthy, controlled, and resilient.

SECONDARY OBJECTIVES
1. Support safe scaling of the agent ecosystem.
2. Advise leadership on security tradeoffs and operational exposure.
3. Maintain security awareness in architecture, automation, and workflow design.

CORE FUNCTIONS
Security monitoring, risk assessment, credential and access governance, policy enforcement, security review of workflows and automations, incident triage and response coordination, data handling oversight, infrastructure exposure review, vendor/tool integration risk review, security escalation reporting.

WHAT YOU MUST MONITOR
suspicious behavior, excessive permissions, insecure storage of credentials, exposed endpoints, risky integrations, automation misconfigurations, unauthorized access patterns, weak operational controls, insecure data flow between agents, missing auditability.

DECISION PRINCIPLES
- Prioritize prevention over reaction.
- Assume risk exists where controls are weak.
- Prefer least privilege, compartmentalization, and traceability.
- Be strict with security-sensitive actions.
- Escalate material risk quickly and clearly.
- Balance security with business continuity, but do not ignore high-impact threats.

AUTHORITY AND AUTONOMY
You may flag and classify security risks, recommend controls and remediation, require review before high-risk deployment, request credential rotation or access reduction, trigger incident workflows, notify leadership of serious exposure.
You may not unilaterally redefine company strategy, approve major budget reallocations, suppress material incidents from leadership.

INTERACTION RULES
You collaborate with Bridge_CEO_BOT for strategic risk visibility, Bridge_Chief_Of_Staff_bot for cross-functional remediation tracking, infrastructure/finance/legal/operations agents where applicable, Bridge_Cleaner_bot for safe archival and lifecycle hygiene.

ESCALATE IMMEDIATELY WHEN credentials may be exposed, client or enterprise data may be compromised, an agent or workflow behaves outside expected bounds, access control is bypassed, infrastructure is materially exposed, or legal/compliance risk may arise from security posture.

OUTPUT STYLE
Always structure outputs as: risk summary, severity (low|medium|high|critical), affected systems, probable cause, immediate actions, long-term remediation, required owner, escalation urgency.

SUCCESS DEFINITION
You are successful when risks are caught early, sensitive assets remain protected, and the agent ecosystem scales without unacceptable security exposure.
"""


# Daily 08:30 risk review.
DAILY_CRON = [
    {"hour": 8, "minute": 30, "task_name": "daily_risk_review"},
]

# Wednesday 10:00 deeper controls review.
WEEKLY_CRON = [
    {"weekday": 3, "hour": 10, "minute": 0, "task_name": "weekly_controls_review"},
]


# CSO context: error/anomaly events last 24h, suppression list size,
# webhook traffic patterns (proxy via outreach_messages count by hour),
# any audit-relevant indicators in workflow_events.
CONTEXT_QUERY = r"""
SELECT jsonb_build_object(
    'now_utc', NOW(),
    'error_events_24h', (
        SELECT COALESCE(jsonb_agg(row_to_json(e)), '[]'::jsonb)
        FROM (
            SELECT event_id, workflow_name, event_type, created_at,
                   details_json
            FROM bridge.workflow_events
            WHERE created_at >= NOW() - INTERVAL '24 hours'
              AND (
                  event_type ILIKE '%error%'
                  OR event_type ILIKE '%fail%'
                  OR event_type ILIKE '%denied%'
                  OR event_type ILIKE '%retry%'
              )
            ORDER BY created_at DESC
            LIMIT 30
        ) e
    ),
    'workflow_event_volume_24h', (
        SELECT COALESCE(jsonb_agg(row_to_json(v)), '[]'::jsonb)
        FROM (
            SELECT workflow_name, COUNT(*)::int AS n
            FROM bridge.workflow_events
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY workflow_name
            ORDER BY n DESC
        ) v
    ),
    'suppression_list_size', (
        SELECT COUNT(*)::int FROM bridge.suppression_list
    ),
    'outreach_volume_24h', (
        SELECT jsonb_build_object(
            'outbound', (SELECT COUNT(*)::int FROM bridge.outreach_messages
                         WHERE direction='outbound' AND sent_at >= NOW() - INTERVAL '24 hours'),
            'inbound',  (SELECT COUNT(*)::int FROM bridge.outreach_messages
                         WHERE direction='inbound'  AND sent_at >= NOW() - INTERVAL '24 hours')
        )
    ),
    'expense_anomalies', (
        SELECT COALESCE(jsonb_agg(row_to_json(a)), '[]'::jsonb)
        FROM (
            SELECT vendor, ROUND(SUM(amount_usd)::numeric, 4) AS usd_24h,
                   COUNT(*)::int AS n_calls
            FROM bridge.expenses
            WHERE occurred_at >= NOW() - INTERVAL '24 hours'
            GROUP BY vendor
            ORDER BY usd_24h DESC
            LIMIT 5
        ) a
    ),
    'system_limits', (
        SELECT COALESCE(jsonb_agg(row_to_json(l)), '[]'::jsonb)
        FROM (
            SELECT key, value FROM bridge.system_limits
            ORDER BY key
        ) l
    )
) AS context;
"""


# CSO must NEVER auto-rotate credentials, change schema, or delete data.
# Its primary tool is memos (with severity); high-risk actions always escalate.
ACTION_RISK_MAP = {
    "memo":     "low",     # findings to CoS / CEO, severity inside body_json
    "archive":  "low",     # close its own resolved findings
    "no_op":    "low",
    "escalate": "low",     # high/critical severity -> CEO urgent memo
    "query":    "medium",  # ad-hoc forensic SELECT: user-approved
    "cleanup":  "high",    # NEVER auto-deletes data
}


def main() -> int:
    wf = build_bot_workflow(
        bot_name="cso",
        workflow_display_name="bridge_chief_sec_off_bot",
        telegram_env_var="BRIDGE_CHIEF_SEC_OFF_BOT_TOKEN",
        telegram_cred_name="Bridge Chief Security Officer Bot",
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
