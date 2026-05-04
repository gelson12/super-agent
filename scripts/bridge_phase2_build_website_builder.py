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
// Extract HTML from v0.dev response. On failure, call LEGION as fallback.
// If LEGION also fails, render a minimal static page so Vercel deploy still succeeds.
const https  = require('https');
const crypto = require('crypto');
const brief  = $('Build brief').first().json.brief_json;
const slug   = $('Build brief').first().json.slug;
const v0Prompt = $('Build v0 prompt').first().json.v0_prompt || '';
const resp   = $json;

// ── 1. Parse v0.dev response ─────────────────────────────────────────────────
let raw = '';
try { raw = (resp.choices?.[0]?.message?.content || resp.content || '').trim(); } catch(e) {}
let html = raw.replace(/^```html\s*/i,'').replace(/^```\s*/,'').replace(/```\s*$/,'').trim();
let engine = 'v0.dev';

// ── 2. LEGION fallback if v0 returned nothing usable ─────────────────────────
if (!html || !html.toLowerCase().includes('<html')) {
  const legBase   = ($env.LEGION_BASE_URL || '').replace(/\/+$/, '');
  const legSecret = $env.LEGION_API_SHARED_SECRET || '';

  if (legBase && legSecret) {
    try {
      const legBody = Buffer.from(JSON.stringify({
        query: v0Prompt,
        complexity: 4,
        task_kind: 'bridge_bots',
        deadline_ms: 90000,
      }));
      const ts  = Math.floor(Date.now() / 1000).toString();
      const sig = crypto.createHmac('sha256', legSecret)
                        .update(ts + '\n' + legBody.toString())
                        .digest('hex');
      const url = new URL(legBase + '/v1/respond');
      const legRes = await new Promise((resolve, reject) => {
        const req = https.request({
          hostname: url.hostname, path: url.pathname,
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': legBody.length,
            'X-Legion-Ts': ts, 'X-Legion-Sig': sig,
          }
        }, res => {
          const chunks = [];
          res.on('data', c => chunks.push(c));
          res.on('end', () => {
            try { resolve(JSON.parse(Buffer.concat(chunks).toString())); }
            catch(e) { resolve({}); }
          });
        });
        req.on('error', reject);
        req.setTimeout(95000, () => { req.destroy(); reject(new Error('timeout')); });
        req.write(legBody); req.end();
      });
      const legContent = (legRes.content || '').trim()
        .replace(/^```html\s*/i,'').replace(/^```\s*/,'').replace(/```\s*$/,'').trim();
      if (legContent && legContent.toLowerCase().includes('<html')) {
        html   = legContent;
        engine = 'legion/' + (legRes.winner_agent || '?');
      }
    } catch(legErr) { /* LEGION also failed — fall through to static */ }
  }
}

// ── 3. Last-resort static page if both v0 and LEGION failed ──────────────────
if (!html || !html.toLowerCase().includes('<html')) {
  const biz  = brief.business_name || 'Local Business';
  const cat  = brief.category || 'service';
  const city = brief.city || '';
  engine = 'static-fallback';
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

const llm_cost_usd = engine.startsWith('v0') ? 0.10 : 0.00;
const charCount = html.length;

return {
  json: {
    lead_id: $('Build brief').first().json.lead_id,
    slug,
    brief_json: brief,
    copy_json: { engine, char_count: charCount },
    rendered_html: html,
    preview_url: null,  // set by Deploy to Vercel node
    llm_cost_usd,
    llm_meta: { model: engine, char_count: charCount },
  }
};
""".strip()


VERCEL_DEPLOY_CODE = r"""
// Upload rendered HTML to Vercel and create a preview deployment.
const https  = require('https');
const crypto = require('crypto');
const prev   = $input.first().json;
const html   = prev.rendered_html || '';
const slug   = prev.slug || 'demo';
const token  = $env.VERCEL_TOKEN || '';

if (!token) {
  return [{ json: { ...prev, deploy_ok: false, commit_ok: false,
    preview_url: null, vercel_error: 'VERCEL_TOKEN not set' } }];
}

function httpsReq(opts, bodyBuf) {
  return new Promise((resolve, reject) => {
    const req = https.request(opts, res => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        try { resolve({ status: res.statusCode, data: JSON.parse(raw) }); }
        catch(e) { resolve({ status: res.statusCode, data: { raw: raw.slice(0,400) } }); }
      });
    });
    req.on('error', reject);
    req.setTimeout(30000, () => { req.destroy(); reject(new Error('timeout')); });
    if (bodyBuf) req.write(bodyBuf);
    req.end();
  });
}

// 1. Upload file blob to Vercel blob store
const htmlBuf = Buffer.from(html, 'utf8');
const sha1    = crypto.createHash('sha1').update(htmlBuf).digest('hex');
await httpsReq({
  hostname: 'api.vercel.com', path: '/v2/files', method: 'POST',
  headers: {
    'Authorization': 'Bearer ' + token,
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': htmlBuf.length,
    'x-vercel-digest': sha1,
  }
}, htmlBuf);

// 2. Create preview deployment (framework: null = pure static)
const depName = ('bridge-' + slug).slice(0, 52);
const depBody = Buffer.from(JSON.stringify({
  name: depName,
  files: [{ file: 'index.html', sha: sha1, size: htmlBuf.length }],
  projectSettings: { framework: null },
  target: 'preview',
}));
const depRes = await httpsReq({
  hostname: 'api.vercel.com', path: '/v13/deployments', method: 'POST',
  headers: {
    'Authorization': 'Bearer ' + token,
    'Content-Type': 'application/json',
    'Content-Length': depBody.length,
  }
}, depBody);

const dep       = depRes.data || {};
const vercelUrl = dep.url ? 'https://' + dep.url : null;
const deployOk  = !!vercelUrl;

return [{ json: {
  ...prev,
  preview_url:    vercelUrl,
  vercel_url:     vercelUrl,
  vercel_id:      dep.id || null,
  vercel_status:  dep.readyState || dep.status || 'unknown',
  vercel_error:   deployOk ? null : JSON.stringify(dep).slice(0, 300),
  deploy_ok:      deployOk,
  commit_ok:      deployOk,
  repo_commit_sha: dep.id || null,
} }];
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


def code_deploy_vercel():
    return {
        "parameters": {"jsCode": VERCEL_DEPLOY_CODE, "mode": "runOnceForAllItems"},
        "id": "node_deploy_vercel",
        "name": "Deploy to Vercel",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [1780, 400],
    }


def wait_vercel_ready():
    return {
        "parameters": {"amount": 20, "unit": "seconds"},
        "id": "node_wait",
        "name": "Wait for Vercel (20s)",
        "type": "n8n-nodes-base.wait",
        "typeVersion": 1.1,
        "position": [2000, 400],
        "webhookId": "bridge-vercel-wait",
    }


def pg_insert_project():
    return {
        "parameters": {
            "operation": "executeQuery",
            "query": INSERT_WEBSITE_PROJECT_QUERY,
            "options": {"queryReplacement": "={{ JSON.stringify({lead_id: $json.lead_id, slug: $json.slug, brief_json: $json.brief_json, copy_json: $json.copy_json, build_method: 'v0_vercel', preview_url: $json.preview_url, repo_name: null, repo_commit_sha: $json.vercel_id, deployment_status: $json.commit_ok ? 'live' : 'failed'}) }}"},
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
                    "lead_id: $('Deploy to Vercel').first().json.lead_id, "
                    "commit_ok: $('Deploy to Vercel').first().json.commit_ok"
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
        "={{ $('Deploy to Vercel').first().json.lead_id }},"
        "v0.dev website generation for {{ $('Deploy to Vercel').first().json.slug }},"
        "{{ $('Deploy to Vercel').first().json.llm_cost_usd }},"
        "{{ JSON.stringify($('Deploy to Vercel').first().json.llm_meta) }}"
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
        "lead_id: $('Deploy to Vercel').first().json.lead_id, "
        "event_type: ($('Deploy to Vercel').first().json.commit_ok ? 'demo_built' : 'demo_build_failed'), "
        "old_status: 'In Progress', "
        "new_status: ($('Deploy to Vercel').first().json.commit_ok ? 'Ready for Marketing' : 'Cannot Build Yet'), "
        "slug: $('Deploy to Vercel').first().json.slug, "
        "preview_url: $('Deploy to Vercel').first().json.preview_url, "
        "repo_commit_sha: $('Deploy to Vercel').first().json.repo_commit_sha, "
        "llm_cost_usd: $('Deploy to Vercel').first().json.llm_cost_usd"
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
        "{{ $('Deploy to Vercel').first().json.commit_ok ? '✨  Demo live on Vercel!' : '⚠️  Build failed' }}\n"
        "\n"
        "  Client:    {{ $('Build brief').first().json.brief_json.business_name }}\n"
        "  Status:    {{ $('Deploy to Vercel').first().json.commit_ok ? 'Ready for Marketing' : 'Cannot Build Yet' }}\n"
        "\n"
        "🌐  Vercel preview URL:\n"
        "  {{ $('Deploy to Vercel').first().json.preview_url }}\n"
        "\n"
        "🤖  Engine: {{ $('Deploy to Vercel').first().json.copy_json.engine }} → Vercel\n"
        "\n"
        "  Contact baked in:\n"
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
        "  Engine:    {{ $('Deploy to Vercel').first().json.llm_meta.model }} ({{ $('Deploy to Vercel').first().json.llm_meta.char_count }} chars)\n"
        "  Cost:      ${{ $('Deploy to Vercel').first().json.llm_cost_usd }}\n"
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
        "\"lead_id\": {{ JSON.stringify($('Deploy to Vercel').first().json.lead_id) }}, "
        "\"slug\": {{ JSON.stringify($('Deploy to Vercel').first().json.slug) }}, "
        "\"preview_url\": {{ JSON.stringify($('Deploy to Vercel').first().json.preview_url) }}, "
        "\"llm_cost_usd\": {{ $('Deploy to Vercel').first().json.llm_cost_usd }}, "
        "\"commit_ok\": {{ $('Deploy to Vercel').first().json.commit_ok }} }"
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
        code_deploy_vercel(),
        wait_vercel_ready(),
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
    link("Parse v0 HTML", "Deploy to Vercel")
    link("Deploy to Vercel", "Wait for Vercel (20s)")
    link("Wait for Vercel (20s)", "Insert website_project")
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
