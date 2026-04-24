"""Build super-agent/n8n/bridge_researcher.json.

Generates the Phase 1 Researcher workflow:

  [Webhook POST /bridge-research-trigger]
    -> [PG: pick campaign]
    -> [HTTP: Places API searchText]
    -> [Code: normalize + split]       (one item per place)
        -> [PG: INSERT lead ON CONFLICT DO NOTHING RETURNING lead_id]
        -> [IF: was inserted?]
            yes:
              -> [HTTP: super-agent /chat (qualify)]
              -> [Code: parse JSON]
              -> [PG: UPDATE bridge.leads with decision + research_status]
              -> [PG: INSERT bridge.workflow_events audit row]
            no: (dupe; skip the expensive AI call)
    -> [PG: aggregate campaign run stats]
    -> [HTTP: Telegram PM bot sendMessage (summary)]
    -> [Respond to Webhook]

Design notes:
- Phase 1 MVP: MANUAL trigger only (no cron). No Playwright enrichment yet.
  Qualification relies on Places fields alone. Both are wired in Phase 1.5.
- Dedup: `ON CONFLICT DO NOTHING RETURNING lead_id` catches conflicts on any
  of the three unique indexes (phone, domain, name+city) — if `lead_id` is
  returned the row is new, otherwise it was a duplicate.
- Every qualified lead stops at `research_status='Needs Review'` — we do NOT
  auto-advance to `Ready for Website Team`. That is the plan's human approval
  checkpoint. A separate `/webhook/bridge-lead-approve` endpoint (Phase 1.5)
  will flip the status when the user taps APPROVE.
"""

from __future__ import annotations
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = REPO_ROOT / "n8n" / "bridge_researcher.json"

# Credentials reused from the existing n8n install (see memory/project_railway_infra.md)
PG_CRED = {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}


# ─────────────────────────── NODE DEFINITIONS ────────────────────────────

def webhook_trigger():
    return {
        "parameters": {
            "httpMethod": "POST",
            "path": "bridge-research-trigger",
            "responseMode": "responseNode",
            "options": {},
        },
        "id": "node_trigger",
        "name": "Trigger",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [240, 400],
        "webhookId": "bridge-research-trigger",
    }


PICK_CAMPAIGN_QUERY = r"""
-- Webhook body may provide campaign_target_id (UUID), OR niche+city, OR neither.
-- We pad unused params with the sentinel string '__BRIDGE_NONE__' in the
-- queryReplacement expression because n8n drops leading empty values when
-- comma-splitting queryReplacement — that would cause "there is no parameter $3".
-- Comparisons use NULLIF(..., '__BRIDGE_NONE__') so the sentinel becomes NULL.
-- We compare UUID via column::text rather than CAST($1 AS UUID) — the latter
-- raises "invalid input syntax for type uuid" for any non-UUID string, even
-- when guarded by NULLIF.
WITH
  explicit_by_id AS (
    SELECT *
    FROM bridge.campaign_targets
    WHERE NULLIF($1, '__BRIDGE_NONE__') IS NOT NULL
      AND campaign_target_id::text = NULLIF($1, '__BRIDGE_NONE__')
    LIMIT 1
  ),
  explicit_by_nc AS (
    SELECT *
    FROM bridge.campaign_targets
    WHERE NULLIF($2, '__BRIDGE_NONE__') IS NOT NULL
      AND NULLIF($3, '__BRIDGE_NONE__') IS NOT NULL
      AND LOWER(niche) = LOWER($2)
      AND LOWER(city)  = LOWER($3)
    LIMIT 1
  ),
  auto_pick AS (
    SELECT ct.*
    FROM bridge.campaign_targets ct
    WHERE ct.active_flag = TRUE
      AND NOT EXISTS (SELECT 1 FROM explicit_by_id)
      AND NOT EXISTS (SELECT 1 FROM explicit_by_nc)
    ORDER BY ct.priority DESC, ct.updated_at ASC
    LIMIT 1
  ),
  combined AS (
    SELECT * FROM explicit_by_id
    UNION ALL SELECT * FROM explicit_by_nc
    UNION ALL SELECT * FROM auto_pick
  )
SELECT c.*,
       (SELECT COUNT(*)::int FROM bridge.leads l
          WHERE LOWER(l.city) = LOWER(c.city)
            AND l.category = c.niche
            AND l.created_at >= date_trunc('day', NOW())) AS leads_today
FROM combined c
LIMIT 1;
""".strip()


def pg_pick_campaign():
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": PICK_CAMPAIGN_QUERY,
            "options": {
                # Pad empties with __BRIDGE_NONE__ — n8n drops leading empty
                # values when comma-splitting, which breaks $3.
                "queryReplacement": "={{$json.body?.campaign_target_id || '__BRIDGE_NONE__'}},{{$json.body?.niche || '__BRIDGE_NONE__'}},{{$json.body?.city || '__BRIDGE_NONE__'}}"
            },
        },
        "id": "node_pick_campaign",
        "name": "Pick campaign",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [460, 400],
        "credentials": {"postgres": PG_CRED},
    }


# Legacy Places API (Text Search + Place Details) — used instead of Places API
# (New) because the new product isn't reachable with the user's current API-key
# restriction and the "Places API (New)" entry doesn't appear in their project's
# restrictions dropdown. Legacy is covered by the existing "Places API"
# restriction ticked on their key.
#
# Text Search returns basic fields (name, place_id, address, rating, reviews).
# Website + phone + opening hours require a follow-up Place Details call per
# result. Cost: ~$32/1k textsearch + ~$17/1k details-with-website =
# ~$0.08 for a 3-lead smoke test. Still pennies.


def http_text_search():
    return {
        "parameters": {
            "method": "GET",
            "url": "https://maps.googleapis.com/maps/api/place/textsearch/json",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "query", "value":
                    "={{$json.niche}} in {{$json.city}}, {{$json.country === 'GB' ? 'United Kingdom' : $json.country}}"},
                {"name": "region", "value": "={{$json.country === 'GB' ? 'uk' : ($json.country || '').toLowerCase()}}"},
                {"name": "language", "value": "en"},
                {"name": "key", "value": "={{$env.GOOGLE_PLACES_API_KEY}}"},
            ]},
            "options": {"timeout": 30000},
        },
        "id": "node_text_search",
        "name": "Places textsearch (legacy)",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [680, 400],
    }


SPLIT_LIMIT_CODE = r"""
// Input: one item with $json = { status, results: [...] }.
// Output: one item per place, capped at (daily_lead_target - leads_today).
const campaign = $('Pick campaign').first().json;
const input = $input.first().json;
const results = Array.isArray(input.results) ? input.results : [];
if (input.status && input.status !== 'OK' && input.status !== 'ZERO_RESULTS') {
  throw new Error('Google Places textsearch returned status=' + input.status + ' ' + (input.error_message || ''));
}
const cap = Math.max(0, (campaign.daily_lead_target || 20) - (campaign.leads_today || 0));
const limited = results.slice(0, cap);
return limited.map(r => ({ json: r }));
""".strip()


def code_split_limit():
    return {
        "parameters": {"jsCode": SPLIT_LIMIT_CODE, "mode": "runOnceForAllItems"},
        "id": "node_split_limit",
        "name": "Split + limit results",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [900, 400],
    }


def http_place_details():
    return {
        "parameters": {
            "method": "GET",
            "url": "https://maps.googleapis.com/maps/api/place/details/json",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "place_id", "value": "={{$json.place_id}}"},
                {"name": "fields", "value":
                    "place_id,name,formatted_address,address_components,website,formatted_phone_number,international_phone_number,opening_hours/weekday_text,business_status,price_level,rating,user_ratings_total,types,url"},
                {"name": "language", "value": "en"},
                {"name": "key", "value": "={{$env.GOOGLE_PLACES_API_KEY}}"},
            ]},
            "options": {"timeout": 30000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_place_details",
        "name": "Place Details (legacy)",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [1120, 400],
    }


NORMALIZE_CODE = r"""
// Input (per item): $json is Place Details response: { status, result: {...} }.
// We also have access to the original search result via the upstream node so we
// keep the place_id + address + types + rating even if Details skipped fields.

const campaign = $('Pick campaign').first().json;
const searchHit = $('Split + limit results').itemMatching($itemIndex).json;
const details  = ($json && $json.status === 'OK' && $json.result) ? $json.result : {};

function digits(s) { return (s || '').replace(/\D+/g, ''); }
function lower(s) { return (s || '').toString().toLowerCase(); }
function stripCompanyFluff(name) {
  return lower(name || '')
    .replace(/\b(ltd|limited|llc|plc|inc|co|company|the|services?|group)\b/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}
function extractDomain(url) {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.hostname.replace(/^www\./, '').toLowerCase();
  } catch { return null; }
}
function findAddr(components, type) {
  if (!Array.isArray(components)) return null;
  const m = components.find(c => Array.isArray(c.types) && c.types.includes(type));
  return m ? (m.long_name || m.short_name || null) : null;
}

const name = details.name || searchHit.name || null;
const addr = details.formatted_address || searchHit.formatted_address || null;
const comps = details.address_components || [];
const phoneIntl = details.international_phone_number || details.formatted_phone_number || null;
const website = details.website || null;
const mapsUrl = details.url || null;

// runOnceForEachItem: return a bare {json: {...}} object, NOT an array wrapper.
return {
  json: {
    // Campaign context
    campaign_target_id: campaign.campaign_target_id,
    campaign_niche: campaign.niche,
    campaign_city: campaign.city,
    campaign_country: campaign.country || 'GB',

    // Identifiers
    place_id: details.place_id || searchHit.place_id || null,
    raw_payload: { search: searchHit, details },

    // Lead fields
    business_name: name,
    normalized_business_name: stripCompanyFluff(name),
    category: campaign.niche,
    subcategory: Array.isArray(details.types) ? details.types[0] : null,
    address_line_1: addr,
    city:   findAddr(comps, 'postal_town') || findAddr(comps, 'locality') || campaign.city,
    region: findAddr(comps, 'administrative_area_level_2') || null,
    postcode: findAddr(comps, 'postal_code') || null,
    country: findAddr(comps, 'country')
               ? String(findAddr(comps, 'country')).substring(0,2).toUpperCase()
               : (campaign.country || 'GB'),
    phone_raw: phoneIntl,
    phone_normalized: phoneIntl ? digits(phoneIntl) : null,
    email: null,
    website_url_found: website,
    website_domain: extractDomain(website),
    maps_url: mapsUrl,
    source_listing_url: mapsUrl,
    reviews_count: details.user_ratings_total ?? searchHit.user_ratings_total ?? null,
    reviews_rating: details.rating ?? searchHit.rating ?? null,
    business_status: details.business_status || searchHit.business_status || null,
    price_level: details.price_level ?? null,
    has_opening_hours: !!(details.opening_hours && Array.isArray(details.opening_hours.weekday_text)),
    business_description: null,
    service_list: null,
    social_links: null,
    logo_url: null,
  }
};
""".strip()


def code_normalize():
    return {
        "parameters": {"jsCode": NORMALIZE_CODE, "mode": "runOnceForEachItem"},
        "id": "node_normalize",
        "name": "Normalize places",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [1340, 400],
    }


# Passes ALL lead fields as ONE JSONB parameter ($1) to avoid n8n's
# queryReplacement comma-split breaking on fields that contain commas
# (addresses, descriptions). Read fields in SQL via d->>'key'.
INSERT_LEAD_QUERY = r"""
WITH p AS (SELECT $1::jsonb AS d),
ins AS (
  INSERT INTO bridge.leads (
    business_name, normalized_business_name, category, subcategory,
    address_line_1, city, region, postcode, country,
    phone_raw, phone_normalized, email, email_status,
    maps_url, source_listing_url, website_url_found, website_domain,
    reviews_count, reviews_rating, opening_hours,
    research_status, website_status, marketing_status, finance_status, project_status
  )
  SELECT
    d->>'business_name',
    d->>'normalized_business_name',
    d->>'category',
    d->>'subcategory',
    d->>'address_line_1',
    d->>'city',
    NULLIF(d->>'region', ''),
    NULLIF(d->>'postcode', ''),
    UPPER(SUBSTRING(COALESCE(NULLIF(d->>'country',''), 'GB'), 1, 2)),
    NULLIF(d->>'phone_raw', ''),
    NULLIF(d->>'phone_normalized', ''),
    NULLIF(d->>'email', ''),
    CASE WHEN NULLIF(d->>'email','') IS NULL THEN 'missing' ELSE 'present' END,
    NULLIF(d->>'maps_url', ''),
    NULLIF(d->>'source_listing_url', ''),
    NULLIF(d->>'website_url_found', ''),
    NULLIF(d->>'website_domain', ''),
    CAST(NULLIF(d->>'reviews_count','') AS INTEGER),
    CAST(NULLIF(d->>'reviews_rating','') AS NUMERIC(3,2)),
    CASE WHEN (d->>'has_opening_hours')::boolean THEN '{"present": true}'::jsonb ELSE NULL END,
    'New', 'Queued', 'Queued', 'N/A', 'Planned'
  FROM p
  ON CONFLICT DO NOTHING
  RETURNING lead_id, 'inserted' AS outcome
)
SELECT * FROM ins;
""".strip()


def pg_insert_lead():
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": INSERT_LEAD_QUERY,
            # Single JSONB param = whole normalized lead payload.
            "options": {"queryReplacement": "={{JSON.stringify($json)}}"},
        },
        "id": "node_insert_lead",
        "name": "Insert lead (skip dupes)",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [1560, 400],
        "credentials": {"postgres": PG_CRED},
    }


def if_inserted():
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                "conditions": [
                    {
                        "id": "c_inserted",
                        "leftValue": "={{$json.outcome}}",
                        "rightValue": "inserted",
                        "operator": {"type": "string", "operation": "equals"},
                    }
                ],
                "combinator": "and",
            },
        },
        "id": "node_if_inserted",
        "name": "If inserted",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": [1340, 400],
    }


QUALIFY_SYSTEM_PROMPT = (
    "You are a lead qualification analyst for UK local service businesses "
    "(plumbing, HVAC, roofing, solar, pest control, tree service, concrete, "
    "pool companies, property management). Classify each prospect as one of: "
    "no_website | weak_website | present | needs_review | rejected. Be "
    "conservative. Output ONLY a single JSON object matching the schema "
    "below — no prose, no code fences. "
    "Schema: {\\\"decision\\\": \\\"no_website|weak_website|present|needs_review|rejected\\\", "
    "\\\"confidence_0_to_100\\\": <int>, "
    "\\\"website_presence_status\\\": \\\"no_website|weak_website|present|uncertain\\\", "
    "\\\"website_quality_score_0_to_100\\\": <int>, "
    "\\\"reasons\\\": [\\\"...\\\", ...], "
    "\\\"recommended_next_step\\\": \\\"qualify_for_demo|manual_review|skip\\\"}. "
    "Rules: 'no_website' = no website_url AND no domain. 'weak_website' = website "
    "exists but (rating<3.5 with reviews>5) OR (domain on wixsite/godaddysites/"
    "weebly/blogspot/facebook) OR (no https) OR (reviews<3 with parked-looking "
    "domain). 'present' = legitimate domain + rating>=4.0 + reviews>=5 → skip. "
    "'rejected' = not a local-service business. Always fill reasons with "
    "short, specific justifications."
)


def http_qualify():
    # We embed the structured lead facts directly in the message, alongside the
    # system preamble. super-agent's /chat concatenates system + message, so
    # prompt-engineering lives in `message`.
    message_expr = (
        f"={QUALIFY_SYSTEM_PROMPT}"
        " CLASSIFY THIS BUSINESS: "
        "business_name={{$('Normalize places').itemMatching($itemIndex).json.business_name}} "
        "category={{$('Normalize places').itemMatching($itemIndex).json.category}} "
        "city={{$('Normalize places').itemMatching($itemIndex).json.city}} "
        "address={{$('Normalize places').itemMatching($itemIndex).json.address_line_1}} "
        "phone={{$('Normalize places').itemMatching($itemIndex).json.phone_raw}} "
        "website={{$('Normalize places').itemMatching($itemIndex).json.website_url_found ?? 'null'}} "
        "rating={{$('Normalize places').itemMatching($itemIndex).json.reviews_rating ?? 'null'}} "
        "reviews_count={{$('Normalize places').itemMatching($itemIndex).json.reviews_count ?? 0}} "
        "types={{JSON.stringify($('Normalize places').itemMatching($itemIndex).json.raw_payload?.types ?? [])}} "
        "business_status={{$('Normalize places').itemMatching($itemIndex).json.business_status ?? 'UNKNOWN'}} "
        "Return single JSON object only."
    )
    # /chat/direct + force_model=HAIKU bypasses dispatcher keyword-classification
    # (which misroutes "classify this business" to the github_agent chain).
    # HAIKU is fast (~5-20s), cheap, and obedient about returning pure JSON.
    return {
        "parameters": {
            "method": "POST",
            "url": "https://super-agent-production.up.railway.app/chat/direct",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-Token", "value": "={{$env.SUPER_AGENT_PASSWORD}}"},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "message": "' + message_expr.lstrip("=") + '", '
                '"model": "HAIKU", '
                '"session_id": "bridge-qualify-{{$json.lead_id}}" }'
            ),
            "options": {"timeout": 60000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_qualify",
        "name": "super-agent qualify",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [1560, 300],
    }


PARSE_QUALIFY_CODE = r"""
// The super-agent response shape is { response: "<json-string>", ... }.
// Parse the inner JSON; on failure mark the lead Needs Review with reasons.
const api = $json;
const leadId = $('Insert lead (skip dupes)').itemMatching($itemIndex).json.lead_id;
let parsed = null;
let parseError = null;
try {
  const raw = (api.response || '').trim()
    .replace(/^```json\s*/, '').replace(/```$/, '').trim();
  parsed = JSON.parse(raw);
} catch (e) { parseError = e.message; }

if (!parsed) {
  return { json: {
    lead_id: leadId,
    decision: 'needs_review',
    confidence_0_to_100: 0,
    website_presence_status: 'uncertain',
    website_quality_score_0_to_100: 0,
    reasons: ['qualifier response not JSON: ' + (parseError || 'empty')],
    recommended_next_step: 'manual_review',
    research_status: 'Needs Review',   // NOT NULL in bridge.leads
    api_meta: { model_used: api.model_used, complexity: api.complexity }
  }};
}

// Map decision -> research_status.
const map = {
  no_website:    'No Website Detected',
  weak_website:  'Weak Website Detected',
  needs_review:  'Needs Review',
  present:       'Rejected',    // good website already -> skip
  rejected:      'Rejected',
};
const research_status = map[parsed.decision] || 'Needs Review';

return { json: {
  lead_id: leadId,
  decision: parsed.decision,
  confidence_0_to_100: parsed.confidence_0_to_100 ?? 0,
  website_presence_status: parsed.website_presence_status ?? 'uncertain',
  website_quality_score_0_to_100: parsed.website_quality_score_0_to_100 ?? 0,
  reasons: parsed.reasons ?? [],
  recommended_next_step: parsed.recommended_next_step ?? 'manual_review',
  research_status,
  api_meta: { model_used: api.model_used, complexity: api.complexity }
}};
""".strip()


def code_parse_qualify():
    return {
        "parameters": {"jsCode": PARSE_QUALIFY_CODE, "mode": "runOnceForEachItem"},
        "id": "node_parse_qualify",
        "name": "Parse qualify JSON",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [1780, 300],
    }


UPDATE_LEAD_QUERY = r"""
WITH p AS (SELECT $1::jsonb AS d)
UPDATE bridge.leads SET
  research_status              = d->>'research_status',
  website_presence_status      = d->>'website_presence_status',
  website_presence_confidence  = CAST(NULLIF(d->>'confidence_0_to_100','') AS INTEGER),
  website_quality_score        = CAST(NULLIF(d->>'website_quality_score_0_to_100','') AS INTEGER),
  lead_score                   = CAST(NULLIF(d->>'confidence_0_to_100','') AS INTEGER),
  assigned_workflow            = 'researcher'
FROM p
WHERE lead_id = (d->>'lead_id')::uuid
RETURNING lead_id, research_status;
""".strip()


def pg_update_lead():
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": UPDATE_LEAD_QUERY,
            "options": {"queryReplacement": "={{JSON.stringify($json)}}"},
        },
        "id": "node_update_lead",
        "name": "Update lead status",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [2000, 300],
        "credentials": {"postgres": PG_CRED},
    }


EVENT_INSERT_QUERY = r"""
WITH p AS (SELECT $1::jsonb AS d)
INSERT INTO bridge.workflow_events
  (lead_id, workflow_name, event_type, old_status, new_status, details_json)
SELECT
  (d->>'lead_id')::uuid,
  'researcher',
  'qualified',
  'New',
  d->>'research_status',
  jsonb_build_object(
    'decision', d->>'decision',
    'confidence', CAST(NULLIF(d->>'confidence_0_to_100','') AS INTEGER),
    'reasons', COALESCE(d->'reasons', '[]'::jsonb),
    'model', COALESCE(d#>>'{api_meta,model_used}', 'unknown')
  )
FROM p
RETURNING event_id;
""".strip()


def pg_event_insert():
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": EVENT_INSERT_QUERY,
            "options": {"queryReplacement": "={{JSON.stringify($json)}}"},
        },
        "id": "node_event_insert",
        "name": "Log workflow_events",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [2220, 300],
        "credentials": {"postgres": PG_CRED},
    }


SUMMARY_QUERY = r"""
SELECT
  jsonb_build_object(
    'campaign_niche', $1,
    'campaign_city',  $2,
    'total_seen',     (SELECT COUNT(*)::int FROM bridge.leads l
                         WHERE LOWER(l.city)=LOWER($2) AND l.category=$1
                           AND l.created_at >= date_trunc('day', NOW())),
    'no_website',     (SELECT COUNT(*)::int FROM bridge.leads l
                         WHERE LOWER(l.city)=LOWER($2) AND l.category=$1
                           AND l.research_status='No Website Detected'
                           AND l.created_at >= date_trunc('day', NOW())),
    'weak_website',   (SELECT COUNT(*)::int FROM bridge.leads l
                         WHERE LOWER(l.city)=LOWER($2) AND l.category=$1
                           AND l.research_status='Weak Website Detected'
                           AND l.created_at >= date_trunc('day', NOW())),
    'needs_review',   (SELECT COUNT(*)::int FROM bridge.leads l
                         WHERE LOWER(l.city)=LOWER($2) AND l.category=$1
                           AND l.research_status='Needs Review'
                           AND l.created_at >= date_trunc('day', NOW())),
    'rejected',       (SELECT COUNT(*)::int FROM bridge.leads l
                         WHERE LOWER(l.city)=LOWER($2) AND l.category=$1
                           AND l.research_status='Rejected'
                           AND l.created_at >= date_trunc('day', NOW()))
  ) AS summary;
""".strip()


def pg_summary():
    replacement = "{{$('Pick campaign').first().json.niche}},{{$('Pick campaign').first().json.city}}"
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": SUMMARY_QUERY,
            "options": {"queryReplacement": "=" + replacement},
        },
        "id": "node_summary",
        "name": "Run summary",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [2440, 400],
        "credentials": {"postgres": PG_CRED},
    }


def http_pm_notify():
    text_expr = (
        "=*Bridge Researcher run complete*\n"
        "Campaign: `{{$json.summary.campaign_niche}} / {{$json.summary.campaign_city}}`\n\n"
        "Seen today: {{$json.summary.total_seen}}\n"
        "  • No website: {{$json.summary.no_website}}\n"
        "  • Weak website: {{$json.summary.weak_website}}\n"
        "  • Needs review: {{$json.summary.needs_review}}\n"
        "  • Rejected: {{$json.summary.rejected}}\n\n"
        "Next: review leads in `bridge.leads` and APPROVE the ones to pass to "
        "Workflow C (website builder). Approval endpoint coming in Phase 1.5."
    )
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{$env.BRIDGE_PM_BOT_TOKEN}}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json; charset=utf-8"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "chat_id": {{$env.BRIDGE_ADMIN_TELEGRAM_CHAT_ID}}, '
                '"parse_mode": "Markdown", '
                '"text": ' + json.dumps(text_expr.lstrip("=")) + ' }'
            ),
            "options": {"timeout": 20000},
        },
        "id": "node_pm_notify",
        "name": "PM bot summary",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [2660, 400],
    }


def respond():
    # Reference the summary node explicitly — $json here is the Telegram
    # sendMessage response, not the summary row.
    return {
        "parameters": {
            "respondWith": "json",
            "responseBody": (
                '={ "status": "ok", "summary": {{ JSON.stringify($(\"Run summary\").first().json.summary) }} }'
            ),
            "options": {},
        },
        "id": "node_respond",
        "name": "Respond OK",
        "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1,
        "position": [2880, 400],
    }


# ─────────────────────────── ASSEMBLY ────────────────────────────

def build_workflow() -> dict:
    nodes = [
        webhook_trigger(),
        pg_pick_campaign(),
        http_text_search(),
        code_split_limit(),
        http_place_details(),
        code_normalize(),
        pg_insert_lead(),
        if_inserted(),
        http_qualify(),
        code_parse_qualify(),
        pg_update_lead(),
        pg_event_insert(),
        pg_summary(),
        http_pm_notify(),
        respond(),
    ]

    connections = {
        "Trigger": {"main": [[{"node": "Pick campaign", "type": "main", "index": 0}]]},
        "Pick campaign": {"main": [[{"node": "Places textsearch (legacy)", "type": "main", "index": 0}]]},
        "Places textsearch (legacy)": {"main": [[{"node": "Split + limit results", "type": "main", "index": 0}]]},
        "Split + limit results": {"main": [[{"node": "Place Details (legacy)", "type": "main", "index": 0}]]},
        "Place Details (legacy)": {"main": [[{"node": "Normalize places", "type": "main", "index": 0}]]},
        "Normalize places": {"main": [[{"node": "Insert lead (skip dupes)", "type": "main", "index": 0}]]},
        "Insert lead (skip dupes)": {"main": [[{"node": "If inserted", "type": "main", "index": 0}]]},
        # IF branch 0 = truthy (inserted). Branch 1 = dupe -> we DO NOT run summary
        # per-item; summary runs once at the end. Join is implicit via the IF
        # node's pass-through in n8n v2.
        "If inserted": {"main": [
            [{"node": "super-agent qualify", "type": "main", "index": 0}],
            []  # dupe branch: drop
        ]},
        "super-agent qualify": {"main": [[{"node": "Parse qualify JSON", "type": "main", "index": 0}]]},
        "Parse qualify JSON": {"main": [[{"node": "Update lead status", "type": "main", "index": 0}]]},
        "Update lead status": {"main": [[{"node": "Log workflow_events", "type": "main", "index": 0}]]},
        "Log workflow_events": {"main": [[{"node": "Run summary", "type": "main", "index": 0}]]},
        "Run summary": {"main": [[{"node": "PM bot summary", "type": "main", "index": 0}]]},
        "PM bot summary": {"main": [[{"node": "Respond OK", "type": "main", "index": 0}]]},
    }

    return {
        "name": "bridge_researcher",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
    }


def main() -> int:
    wf = build_workflow()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(wf, indent=2), encoding="utf-8")
    print(f"[OK]   Wrote {OUT_FILE} ({OUT_FILE.stat().st_size} bytes, {len(wf['nodes'])} nodes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
