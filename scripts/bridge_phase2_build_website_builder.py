"""Build super-agent/n8n/bridge_website_builder.json.

Workflow C — Website Team. Picks the next lead with
`research_status='Ready for Website Team'`, generates a full polished
index.html via the v0.dev API (Vercel AI), commits {slug}/index.html to
the bridge_websites_demos repo via the super-agent /tools/github_commit_demo
endpoint, waits for GitHub Pages to propagate, and hands the lead off to
Marketing.

Per-team Telegram bot pattern:
  - Website bot  → per-stage progress (starting, built, live)
  - Finance bot  → cost breakdown after commit
  - PM bot       → handoff to Marketing

Expenses logged to bridge.expenses at each billable step (v0.dev API).
"""

from __future__ import annotations
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = REPO_ROOT / "n8n" / "bridge_website_builder.json"
PG_CRED = {"id": "Ae3yvuqjgMnVRrCo", "name": "Postgres account"}

# ────────────────────────────  SQL  ────────────────────────────

# Claim next lead AND run capacity gate in one query. Uses SKIP LOCKED so
# concurrent workers never grab the same lead. The capacity check guards
# against runaway builds while Marketing catches up.
PICK_LEAD_QUERY = r"""
WITH
  limits AS (
    SELECT
      (SELECT value::int FROM bridge.system_limits WHERE key='max_websites_in_progress') AS cap,
      (SELECT COUNT(*)::int FROM bridge.leads
         WHERE website_status IN ('In Progress','Draft Ready')) AS in_flight
  ),
  picked AS (
    SELECT l.lead_id, l.business_name, l.normalized_business_name, l.category,
           l.city, l.region, l.postcode, l.country, l.address_line_1,
           l.phone_raw, l.email, l.website_url_found, l.website_domain,
           l.reviews_rating, l.reviews_count, l.research_status,
           l.website_presence_status, l.website_quality_score,
           l.opening_hours, l.service_list, l.logo_url
    FROM bridge.leads l, limits
    WHERE l.research_status = 'Ready for Website Team'
      AND l.website_status IN ('Queued','Cannot Build Yet')
      AND l.archived_flag = FALSE
      AND limits.in_flight < limits.cap
    ORDER BY l.updated_at ASC
    LIMIT 1
    FOR UPDATE OF l SKIP LOCKED
  ),
  advanced AS (
    UPDATE bridge.leads
    SET website_status = 'In Progress',
        updated_at = NOW()
    WHERE lead_id IN (SELECT lead_id FROM picked)
    RETURNING lead_id
  )
SELECT p.*,
       (SELECT cap FROM limits) AS max_websites_in_progress,
       (SELECT in_flight FROM limits) AS current_in_flight
FROM picked p;
""".strip()


INSERT_WEBSITE_PROJECT_QUERY = r"""
WITH p AS (SELECT $1::jsonb AS d)
INSERT INTO bridge.website_projects (
  lead_id, slug, brief_json, copy_json,
  build_method, preview_url, repo_name, repo_commit_sha, deployment_status
)
SELECT
  (d->>'lead_id')::uuid,
  d->>'slug',
  (d->'brief_json')::jsonb,
  (d->'copy_json')::jsonb,
  COALESCE(d->>'build_method', 'static_template'),
  d->>'preview_url',
  COALESCE(d->>'repo_name', 'bridge_websites_demos'),
  d->>'repo_commit_sha',
  COALESCE(d->>'deployment_status', 'live')
FROM p
ON CONFLICT (slug) DO UPDATE SET
  brief_json        = EXCLUDED.brief_json,
  copy_json         = EXCLUDED.copy_json,
  preview_url       = EXCLUDED.preview_url,
  repo_commit_sha   = EXCLUDED.repo_commit_sha,
  deployment_status = EXCLUDED.deployment_status,
  updated_at        = NOW()
RETURNING website_project_id, slug, preview_url;
""".strip()


UPDATE_LEAD_READY_FOR_MKT = r"""
-- Commit-gated status transition: only advance to Ready for Marketing
-- when the GitHub commit actually succeeded. On commit failure, the lead
-- rolls back to 'Cannot Build Yet' so a future run will retry (the
-- ON CONFLICT (slug) DO UPDATE on bridge.website_projects means re-running
-- is safe — the new commit overwrites the empty/broken one).
WITH p AS (SELECT $1::jsonb AS d)
UPDATE bridge.leads
SET website_status = CASE
      WHEN (d->>'commit_ok')::boolean THEN 'Ready for Marketing'
      ELSE 'Cannot Build Yet'
    END,
    updated_at = NOW()
FROM p
WHERE lead_id = (d->>'lead_id')::uuid
RETURNING lead_id, website_status, business_name;
""".strip()


LOG_LLM_EXPENSE = r"""
INSERT INTO bridge.expenses
  (workflow_name, lead_id, category, vendor, description, units, unit_label, amount_usd, details_json)
VALUES
  ('website', $1::uuid, 'ai_generation', 'v0_dev',
   $2, 1, 'calls', CAST($3 AS NUMERIC(10,4)),
   $4::jsonb)
RETURNING expense_id, amount_usd;
""".strip()


LOG_WORKFLOW_EVENT = r"""
WITH p AS (SELECT $1::jsonb AS d)
INSERT INTO bridge.workflow_events
  (lead_id, workflow_name, event_type, old_status, new_status, details_json)
SELECT
  (d->>'lead_id')::uuid, 'website', d->>'event_type',
  d->>'old_status', d->>'new_status',
  jsonb_build_object('slug', d->>'slug', 'preview_url', d->>'preview_url',
                     'repo_commit_sha', d->>'repo_commit_sha',
                     'llm_cost_usd', d->>'llm_cost_usd')
FROM p
RETURNING event_id;
""".strip()


# ────────────────────────────  JS code nodes  ────────────────────────────

BUILD_BRIEF_CODE = r"""
// Input: claimed lead row. Output: brief + Lovable Build-with-URL + swap callback URL.
const lead = $input.first().json;
if (!lead || !lead.lead_id) {
  return { json: { __no_work__: true, reason: 'no lead available or capacity reached' } };
}

function slugify(s) {
  return (s || '')
    .toString().toLowerCase()
    .normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

const shortId = String(lead.lead_id).slice(0, 8);
const slug = [slugify(lead.category), slugify(lead.city), shortId]
  .filter(Boolean).join('-') || ('demo-' + shortId);

const brief = {
  business_name: lead.business_name,
  category: lead.category,
  city: lead.city,
  region: lead.region,
  country: lead.country,
  address: lead.address_line_1,
  phone: lead.phone_raw,
  email: lead.email,
  current_website: lead.website_url_found,
  rating: lead.reviews_rating,
  reviews: lead.reviews_count,
  website_quality_score: lead.website_quality_score,
  slug,
};

// Lovable Build-with-URL: clicking generates a project in the logged-in
// Lovable workspace with this prompt auto-submitted. Prompt stays tight —
// Lovable's own agent fills in layout/styling.
const lovablePrompt =
  'Build a modern, conversion-focused local-service website for ' + (lead.business_name || 'this business') +
  ', a ' + (lead.category || 'service') + ' company based in ' + (lead.city || '') +
  ', ' + (lead.country || 'United Kingdom') + '. ' +
  'Sections required: (1) hero with strong headline + subheading + CTA, ' +
  '(2) services grid with 4-5 cards, (3) why-choose-us bullets, ' +
  '(4) service areas, (5) contact block. ' +
  'Rating signal: ' + (lead.reviews_rating ?? 'n/a') + ' from ' + (lead.reviews_count ?? 0) + ' reviews. ' +
  'Tone: trustworthy, clear, UK local-service. ' +
  'Contact details to display (no other contact info): ' +
  'WhatsApp +44 7345 787028, email bridge.digital.solution@gmail.com. ' +
  'Use the city and service words naturally for local SEO. No stock photos of people; ' +
  'use simple color blocks or abstract shapes instead. Do NOT invent awards, years in ' +
  'business, or testimonials that are not provided.';

const lovableBuildUrl = 'https://lovable.dev/?autosubmit=true#prompt=' + encodeURIComponent(lovablePrompt);

// Pre-filled swap callback — user pastes the Lovable preview URL after 'preview_url='
// and opens it in a browser to switch bridge.website_projects.preview_url.
const swapBase = 'https://outstanding-blessing-production-1d4b.up.railway.app/webhook/bridge-lovable-swap';
const swapCallbackUrl = swapBase + '?lead_id=' + lead.lead_id + '&preview_url=PASTE_LOVABLE_URL_HERE';

return { json: {
  ...lead,
  slug,
  brief_json: brief,
  lovable_prompt: lovablePrompt,
  lovable_build_url: lovableBuildUrl,
  swap_callback_url: swapCallbackUrl,
}};
""".strip()


V0_PROMPT_CODE = r"""
// Build the v0.dev prompt from the brief.
const brief = $('Build brief').first().json.brief_json;
const biz   = brief.business_name || 'Local Business';
const cat   = brief.category || 'service';
const city  = brief.city || '';
const country = brief.country || 'United Kingdom';
const rating  = brief.reviews_rating ?? null;
const reviews = brief.reviews_count ?? 0;

const prompt = `Generate a complete, polished, production-ready single-file index.html for a local service business website.

Business details:
- Name: ${biz}
- Category: ${cat}
- Location: ${city}, ${country}
- Rating: ${rating !== null ? rating + '/5 from ' + reviews + ' reviews' : 'no rating yet'}
- Phone: WhatsApp +44 7345 787028
- Email: bridge.digital.solution@gmail.com

Requirements:
1. Self-contained single file: all CSS inline in <style> and all JS inline in <script>. NO external imports, NO CDN links.
2. Sections: hero with H1 + subheading + CTA button, services grid (4-5 cards), why-choose-us bullets, service areas, contact block (show ONLY the phone and email above — no other contact info), footer.
3. Mobile-first responsive design, professional colour scheme (blues/greens or neutrals — no garish colours).
4. Do NOT fabricate testimonials, awards, years in business, or certifications. Use rating/reviews only if provided.
5. Local SEO: use city and service words naturally in headings and meta tags.
6. No stock photos of people; use CSS shapes, gradients, or simple SVG icons instead.
7. Contact details to display: WhatsApp +44 7345 787028, email bridge.digital.solution@gmail.com.
8. Output ONLY the raw HTML. Start with <!DOCTYPE html>. No markdown fences, no explanation.`;

return { json: {
  ...$input.first().json,
  v0_prompt: prompt,
}};
""".strip()


PARSE_V0_CODE = r"""
// Extract HTML from v0.dev OpenAI-compatible response and build the commit payload.
const brief = $('Build brief').first().json.brief_json;
const slug  = $('Build brief').first().json.slug;
const resp  = $json;

// v0.dev returns OpenAI-compatible JSON: choices[0].message.content
let raw = '';
try {
  raw = (resp.choices?.[0]?.message?.content || resp.content || '').trim();
} catch(e) { raw = ''; }

// Strip markdown fences if present
let html = raw
  .replace(/^```html\s*/i, '')
  .replace(/^```\s*/,      '')
  .replace(/```\s*$/,      '')
  .trim();

// Fallback: if v0.dev returned nothing useful, build a minimal static page
if (!html || !html.includes('<html')) {
  const biz  = brief.business_name || 'Local Business';
  const cat  = brief.category || 'service';
  const city = brief.city || '';
  html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${biz} — ${cat} in ${city}</title>
<style>
body{font-family:sans-serif;margin:0;color:#222}
.hero{background:#1e3a5f;color:#fff;padding:4rem 2rem;text-align:center}
.hero h1{margin:0 0 1rem;font-size:2.2rem}
.hero p{font-size:1.1rem;margin:0 0 2rem}
.btn{display:inline-block;padding:.8rem 2rem;background:#f59e0b;color:#fff;text-decoration:none;border-radius:4px;font-weight:bold}
section{max-width:900px;margin:3rem auto;padding:0 1rem}
footer{text-align:center;padding:2rem;background:#f3f4f6;color:#666}
</style>
</head>
<body>
<div class="hero">
  <h1>${biz}</h1>
  <p>Professional ${cat} services in ${city}.</p>
  <a class="btn" href="tel:+447345787028">Get in touch</a>
</div>
<section>
  <h2>Contact</h2>
  <p>📞 <a href="tel:+447345787028">+44 7345 787028</a></p>
  <p>✉️ <a href="mailto:bridge.digital.solution@gmail.com">bridge.digital.solution@gmail.com</a></p>
</section>
<footer>${biz} — ${city}</footer>
</body>
</html>`;
}

// Rough cost estimate: v0.dev charges per generation (~$0.10 per call at team tier)
const llm_cost_usd = 0.10;
const charCount = html.length;

return {
  json: {
    lead_id: $('Build brief').first().json.lead_id,
    slug,
    brief_json: brief,
    copy_json: { v0_generated: true, char_count: charCount },
    rendered_html: html,
    preview_url: 'https://gelson12.github.io/bridge_websites_demos/' + slug + '/',
    llm_cost_usd,
    llm_meta: { model: 'v0-1.5-md', char_count: charCount, fallback: !raw || !raw.includes('<html') },
  }
};
""".strip()


COMMIT_CODE = r"""
// Prepare the payload for super-agent /tools/github_commit_demo.
// One file per demo: {slug}/index.html. CSS is shared from _template/.
const prev = $input.first().json;
return {
  json: {
    ...prev,
    commit_payload: {
      repo: 'bridge_websites_demos',
      branch: 'main',
      commit_message: 'demo: ' + prev.slug + ' (lead ' + prev.lead_id.slice(0,8) + ')',
      files: [
        { path: prev.slug + '/index.html',  content: prev.rendered_html },
        { path: prev.slug + '/brief.json',  content: JSON.stringify(prev.brief_json, null, 2) },
      ],
    }
  }
};
""".strip()


PARSE_COMMIT_CODE = r"""
const prev = $('Build commit payload').first().json;
const resp = $json || {};
const ok = !!resp.ok;
return {
  json: {
    ...prev,
    repo_commit_sha: (resp.committed && resp.committed.length) ? resp.committed.join(',') : null,
    commit_ok: ok,
    commit_errors: resp.errors || [],
  }
};
""".strip()


# ────────────────────────────  nodes  ────────────────────────────

def webhook_trigger():
    return {
        "parameters": {
            "httpMethod": "POST",
            "path": "bridge-website-trigger",
            "responseMode": "responseNode",
            "options": {},
        },
        "id": "node_trigger",
        "name": "Trigger",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [240, 400],
        "webhookId": "bridge-website-trigger",
    }


def pg_pick_lead():
    return {
        "parameters": {"operation": "executeQuery", "query": PICK_LEAD_QUERY, "options": {}},
        "id": "node_pick_lead",
        "name": "Pick lead + gate",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [460, 400],
        "credentials": {"postgres": PG_CRED},
    }


def code_build_brief():
    return {
        "parameters": {"jsCode": BUILD_BRIEF_CODE, "mode": "runOnceForAllItems"},
        "id": "node_build_brief",
        "name": "Build brief",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [680, 400],
    }


def http_website_bot_start():
    # Persona: Web Designer/Dev — creative, end with "— Web Team".
    text = (
        "🎨  New demo on the table\n"
        "\n"
        "  Client:    {{ $json.brief_json.business_name }}\n"
        "  Location:  {{ $json.brief_json.city }}\n"
        "  Slug:      {{ $json.slug }}\n"
        "\n"
        "Calling v0.dev to generate a polished website (up to 2 min).\n"
        "— Web Team"
    )
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{$env.BRIDGE_WEBSITE_BOT_TOKEN}}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json; charset=utf-8"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "chat_id": {{$env.BRIDGE_ADMIN_TELEGRAM_CHAT_ID}}, '
                '"text": ' + json.dumps(text) + ' }'
            ),
            "options": {"timeout": 15000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_bot_start",
        "name": "Website bot: starting",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [900, 400],
    }


def code_build_v0_prompt():
    return {
        "parameters": {"jsCode": V0_PROMPT_CODE, "mode": "runOnceForAllItems"},
        "id": "node_build_v0_prompt",
        "name": "Build v0 prompt",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [1120, 400],
    }


def http_v0_generate():
    # v0.dev OpenAI-compatible endpoint. Model v0-1.5-md generates full websites.
    body = (
        '={ "model": "v0-1.5-md", '
        '"messages": [{"role": "user", "content": {{ JSON.stringify($json.v0_prompt) }}}], '
        '"stream": false }'
    )
    return {
        "parameters": {
            "method": "POST",
            "url": "https://api.v0.dev/v1/chat/completions",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "=Bearer {{$env.V0_API_KEY}}"},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {"timeout": 120000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_v0_generate",
        "name": "v0.dev generate",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [1340, 400],
    }


def code_parse_v0():
    return {
        "parameters": {"jsCode": PARSE_V0_CODE, "mode": "runOnceForAllItems"},
        "id": "node_parse_v0",
        "name": "Parse v0 HTML",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [1560, 400],
    }


def code_build_commit():
    return {
        "parameters": {"jsCode": COMMIT_CODE, "mode": "runOnceForAllItems"},
        "id": "node_build_commit",
        "name": "Build commit payload",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [1780, 400],
    }


def http_commit_to_repo():
    return {
        "parameters": {
            "method": "POST",
            "url": "https://super-agent-production.up.railway.app/tools/github_commit_demo",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "X-Token", "value": "={{$env.SUPER_AGENT_PASSWORD}}"},
                {"name": "Content-Type", "value": "application/json"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.commit_payload) }}",
            "options": {"timeout": 90000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_commit",
        "name": "github_commit_demo",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [2000, 400],
    }


def code_parse_commit():
    return {
        "parameters": {"jsCode": PARSE_COMMIT_CODE, "mode": "runOnceForAllItems"},
        "id": "node_parse_commit",
        "name": "Parse commit result",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [2220, 400],
    }


def wait_pages_propagate():
    return {
        "parameters": {"amount": 75, "unit": "seconds"},
        "id": "node_wait",
        "name": "Wait for Pages (75s)",
        "type": "n8n-nodes-base.wait",
        "typeVersion": 1.1,
        "position": [2440, 400],
        "webhookId": "bridge-website-wait",
    }


def pg_insert_project():
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": INSERT_WEBSITE_PROJECT_QUERY,
            "options": {"queryReplacement": "={{ JSON.stringify({lead_id: $json.lead_id, slug: $json.slug, brief_json: $json.brief_json, copy_json: $json.copy_json, preview_url: $json.preview_url, repo_commit_sha: $json.repo_commit_sha, deployment_status: $json.commit_ok ? 'live' : 'failed'}) }}"},
        },
        "id": "node_insert_project",
        "name": "Insert website_project",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [2660, 400],
        "credentials": {"postgres": PG_CRED},
    }


def pg_update_lead():
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": UPDATE_LEAD_READY_FOR_MKT,
            "options": {
                "queryReplacement": (
                    "={{ JSON.stringify({"
                    "lead_id: $('Parse commit result').first().json.lead_id, "
                    "commit_ok: $('Parse commit result').first().json.commit_ok"
                    "}) }}"
                )
            },
        },
        "id": "node_update_lead",
        "name": "Lead status (gated by commit_ok)",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [2880, 400],
        "credentials": {"postgres": PG_CRED},
    }


def pg_log_llm_expense():
    qr = (
        "={{ $('Parse commit result').first().json.lead_id }},"
        "v0.dev website generation for {{ $('Parse commit result').first().json.slug }},"
        "{{ $('Parse commit result').first().json.llm_cost_usd }},"
        "{{ JSON.stringify($('Parse commit result').first().json.llm_meta) }}"
    )
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": LOG_LLM_EXPENSE,
            "options": {"queryReplacement": qr},
        },
        "id": "node_log_expense",
        "name": "Log LLM expense",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [3100, 400],
        "credentials": {"postgres": PG_CRED},
    }


def pg_log_event():
    # Event reflects the actual outcome — 'demo_built' on success,
    # 'demo_build_failed' on commit failure.
    payload_expr = (
        "={{ JSON.stringify({"
        "lead_id: $('Parse commit result').first().json.lead_id, "
        "event_type: ($('Parse commit result').first().json.commit_ok ? 'demo_built' : 'demo_build_failed'), "
        "old_status: 'In Progress', "
        "new_status: ($('Parse commit result').first().json.commit_ok ? 'Ready for Marketing' : 'Cannot Build Yet'), "
        "slug: $('Parse commit result').first().json.slug, "
        "preview_url: $('Parse commit result').first().json.preview_url, "
        "repo_commit_sha: $('Parse commit result').first().json.repo_commit_sha, "
        "llm_cost_usd: $('Parse commit result').first().json.llm_cost_usd"
        "}) }}"
    )
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": LOG_WORKFLOW_EVENT,
            "options": {"queryReplacement": payload_expr},
        },
        "id": "node_log_event",
        "name": "Log workflow_event",
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5,
        "position": [3320, 400],
        "credentials": {"postgres": PG_CRED},
    }


def http_website_bot_done():
    # Persona: Web Designer/Dev — celebrate success, offer optional Lovable upgrade.
    text = (
        "{{ $('Parse commit result').first().json.commit_ok ? '✨  Demo ready!' : '⚠️  Build failed' }}\n"
        "\n"
        "  Client:    {{ $('Build brief').first().json.brief_json.business_name }}\n"
        "  Status:    {{ $('Parse commit result').first().json.commit_ok ? 'Ready for Marketing' : 'Cannot Build Yet (rolled back)' }}\n"
        "\n"
        "🌐  Static demo (live now):\n"
        "  {{ $('Parse commit result').first().json.preview_url }}\n"
        "\n"
        "🤖  Built with v0.dev (polished, production-ready HTML)\n"
        "\n"
        "  Contact card baked in:\n"
        "  📞  +44 7345 787028\n"
        "  ✉️  bridge.digital.solution@gmail.com\n"
        "\n"
        "— Web Team"
    )
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{$env.BRIDGE_WEBSITE_BOT_TOKEN}}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json; charset=utf-8"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "chat_id": {{$env.BRIDGE_ADMIN_TELEGRAM_CHAT_ID}}, '
                '"text": ' + json.dumps(text.lstrip("=")) + ' }'
            ),
            "options": {"timeout": 15000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_bot_done",
        "name": "Website bot: done",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [3540, 300],
    }


def http_finance_bot():
    # Persona: Accountant — numbers-first, clean ledger.
    text = (
        "🧾  Website build billed\n"
        "\n"
        "  Lead:      {{ $('Build brief').first().json.brief_json.business_name }}\n"
        "  Model:     v0.dev ({{ $('Parse commit result').first().json.llm_meta.char_count }} chars generated)\n"
        "  Cost:      ${{ $('Parse commit result').first().json.llm_cost_usd }}\n"
        "\n"
        "Logged to bridge.expenses.\n"
        "— Accounts"
    )
    return {
        "parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{$env.BRIDGE_FINANCE_BOT_TOKEN}}/sendMessage",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Content-Type", "value": "application/json; charset=utf-8"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": (
                '={ "chat_id": {{$env.BRIDGE_ADMIN_TELEGRAM_CHAT_ID}}, '
                '"text": ' + json.dumps(text.lstrip("=")) + ' }'
            ),
            "options": {"timeout": 15000, "response": {"response": {"neverError": True}}},
        },
        "id": "node_bot_finance",
        "name": "Finance bot: cost",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [3540, 500],
    }


def respond():
    body = (
        "={ \"status\": \"ok\", "
        "\"lead_id\": {{ JSON.stringify($('Parse commit result').first().json.lead_id) }}, "
        "\"slug\": {{ JSON.stringify($('Parse commit result').first().json.slug) }}, "
        "\"preview_url\": {{ JSON.stringify($('Parse commit result').first().json.preview_url) }}, "
        "\"llm_cost_usd\": {{ $('Parse commit result').first().json.llm_cost_usd }}, "
        "\"commit_ok\": {{ $('Parse commit result').first().json.commit_ok }} }"
    )
    return {
        "parameters": {"respondWith": "json", "responseBody": body, "options": {}},
        "id": "node_respond",
        "name": "Respond OK",
        "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1,
        "position": [3760, 400],
    }


# ────────────────────────────  assembly  ────────────────────────────

def build_workflow() -> dict:
    nodes = [
        webhook_trigger(),
        pg_pick_lead(),
        code_build_brief(),
        http_website_bot_start(),
        code_build_v0_prompt(),
        http_v0_generate(),
        code_parse_v0(),
        code_build_commit(),
        http_commit_to_repo(),
        code_parse_commit(),
        wait_pages_propagate(),
        pg_insert_project(),
        pg_update_lead(),
        pg_log_llm_expense(),
        pg_log_event(),
        http_website_bot_done(),
        http_finance_bot(),
        respond(),
    ]

    c = {}
    def link(a, b, idx=0):
        c.setdefault(a, {"main": [[]]})["main"][0].append({"node": b, "type": "main", "index": idx})

    link("Trigger", "Pick lead + gate")
    link("Pick lead + gate", "Build brief")
    link("Build brief", "Website bot: starting")
    link("Website bot: starting", "Build v0 prompt")
    link("Build v0 prompt", "v0.dev generate")
    link("v0.dev generate", "Parse v0 HTML")
    link("Parse v0 HTML", "Build commit payload")
    link("Build commit payload", "github_commit_demo")
    link("github_commit_demo", "Parse commit result")
    link("Parse commit result", "Wait for Pages (75s)")
    link("Wait for Pages (75s)", "Insert website_project")
    link("Insert website_project", "Lead status (gated by commit_ok)")
    link("Lead status (gated by commit_ok)", "Log LLM expense")
    link("Log LLM expense", "Log workflow_event")
    link("Log workflow_event", "Website bot: done")
    link("Website bot: done", "Finance bot: cost")
    link("Finance bot: cost", "Respond OK")

    return {
        "name": "bridge_website_builder",
        "nodes": nodes,
        "connections": c,
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
