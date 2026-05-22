"""
_migrate_llm_deepseek.py

Migrates the crypto workflows (btc_main / eth_main) OFF the Claude CLI /
inspiring-cat path and onto DeepSeek (primary) + Groq (free fallback).

The trade decision is made upstream by deterministic code (Decision Gate);
the LLM only writes commentary for the Telegram alert / Obsidian note, so
this swap does not change trading behaviour.

Node names ("Submit to Claude CLI", "Claude AI Primary") are intentionally
kept so downstream refs ($('Claude AI Primary')) need no edits. Only the two
nodes' internals change. "Submit to Claude CLI" becomes a code node.

Run:  python n8n_workflows/_migrate_llm_deepseek.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ── New node code ───────────────────────────────────────────────────────────

SUBMIT_CODE = r"""// Primary LLM analysis - DeepSeek (replaces the old Claude CLI / inspiring-cat path).
// The trade decision is already made upstream (Decision Gate); this only
// produces human-readable commentary, so the provider swap is behaviour-safe.
const pd = $('Prepare Claude Prompt').first().json;

// Honour the upstream skip / cache-hit short-circuit - no LLM call on those cycles.
if (pd.skip === true) {
  return [{ json: { skip: true, cacheHit: !!pd.cacheHit,
    analysis: pd.analysis || pd.skipReason || '' } }];
}

const prompt = (pd.payload && pd.payload.prompt) || '';
const apiKey = $env.DEEPSEEK_API_KEY;
if (!apiKey || apiKey.length < 5 || !prompt) {
  return [{ json: { skip: false, ok: false,
    err: !prompt ? 'no_prompt' : 'no_deepseek_key' } }];
}

const https = require('https');
const body = JSON.stringify({
  model: 'deepseek-chat',
  messages: [{ role: 'user', content: prompt }],
  max_tokens: 600,
  temperature: 0.2,
});
const res = await new Promise((resolve) => {
  const req = https.request({
    hostname: 'api.deepseek.com', path: '/chat/completions', method: 'POST',
    headers: { 'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + apiKey,
      'Content-Length': Buffer.byteLength(body) },
  }, (r) => {
    let d = ''; r.on('data', c => d += c);
    r.on('end', () => {
      try {
        const j = JSON.parse(d);
        const txt = j && j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content;
        resolve(txt ? { ok: true, text: txt } : { ok: false, err: 'empty_response' });
      } catch (e) { resolve({ ok: false, err: 'parse_error' }); }
    });
  });
  req.on('error', (e) => resolve({ ok: false, err: 'net:' + e.message }));
  req.setTimeout(30000, () => { req.destroy(); resolve({ ok: false, err: 'timeout' }); });
  req.write(body); req.end();
});

return [{ json: res.ok
  ? { skip: false, ok: true, analysis: res.text, model_used: 'deepseek-chat', routed_by: 'deepseek-api' }
  : { skip: false, ok: false, err: res.err } }];
"""

PRIMARY_CODE = r"""// Final LLM analysis - uses the DeepSeek result; falls back to Groq (free,
// Llama 3.3 70B) on any failure. Node name kept as "Claude AI Primary" only so
// downstream refs stay intact - it no longer calls Claude / Anthropic.
const pd = $('Prepare Claude Prompt').first().json;

// Skip / cache-hit cycles: pass the upstream analysis straight through.
if (pd.skip === true) {
  return [{ json: {
    analysis: pd.analysis || pd.skipReason || 'DO_NOT_TRADE: high no-trade score - system conserving resources this cycle.',
    model_used: pd.cacheHit ? 'cached' : 'skipped',
    routed_by: pd.cacheHit ? 'cache' : 'no-trade-gate',
  }}];
}

const primary = $('Submit to Claude CLI').first().json;
if (primary && primary.ok && primary.analysis) {
  if (pd.cacheKey) {
    const sd = $getWorkflowStaticData('global');
    sd[pd.cacheKey] = { analysis: primary.analysis, ts: Date.now() };
  }
  return [{ json: { analysis: primary.analysis,
    model_used: primary.model_used || 'deepseek-chat', routed_by: 'deepseek-api' } }];
}

// -- Fallback: Groq (free, Llama 3.3 70B) -----------------------------------
const prompt = (pd.payload && pd.payload.prompt) || '';
const gk = $env.GROQ_API_KEY;
if (!gk || gk.length < 5 || !prompt) {
  return [{ json: {
    analysis: 'LLM analysis unavailable (deepseek: ' + ((primary && primary.err) || 'n/a') + '; no groq fallback).',
    model_used: 'error', routed_by: 'none' } }];
}

const https = require('https');
const body = JSON.stringify({
  model: 'llama-3.3-70b-versatile',
  messages: [{ role: 'user', content: prompt }],
  max_tokens: 600,
  temperature: 0.2,
});
const res = await new Promise((resolve) => {
  const req = https.request({
    hostname: 'api.groq.com', path: '/openai/v1/chat/completions', method: 'POST',
    headers: { 'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + gk,
      'Content-Length': Buffer.byteLength(body) },
  }, (r) => {
    let d = ''; r.on('data', c => d += c);
    r.on('end', () => {
      try {
        const j = JSON.parse(d);
        const txt = j && j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content;
        resolve(txt ? { ok: true, text: txt } : { ok: false, err: 'empty_response' });
      } catch (e) { resolve({ ok: false, err: 'parse_error' }); }
    });
  });
  req.on('error', (e) => resolve({ ok: false, err: 'net:' + e.message }));
  req.setTimeout(30000, () => { req.destroy(); resolve({ ok: false, err: 'timeout' }); });
  req.write(body); req.end();
});

if (res.ok) {
  if (pd.cacheKey) {
    const sd = $getWorkflowStaticData('global');
    sd[pd.cacheKey] = { analysis: res.text, ts: Date.now() };
  }
  return [{ json: { analysis: res.text,
    model_used: 'llama-3.3-70b-versatile', routed_by: 'groq-fallback' } }];
}
return [{ json: {
  analysis: 'LLM analysis unavailable - deepseek and groq fallback both failed.',
  model_used: 'error', routed_by: 'none' } }];
"""


def migrate(path):
    with open(path, encoding="utf-8") as f:
        wf = json.load(f)
    nodes = {n["name"]: n for n in wf["nodes"]}
    before = len(wf["nodes"])

    submit = nodes["Submit to Claude CLI"]
    submit["type"] = "n8n-nodes-base.code"
    submit["typeVersion"] = 2
    submit["parameters"] = {"jsCode": SUBMIT_CODE}
    for k in ("credentials", "webhookId"):
        submit.pop(k, None)

    primary = nodes["Claude AI Primary"]
    primary["type"] = "n8n-nodes-base.code"
    primary["typeVersion"] = 2
    primary["parameters"] = {"jsCode": PRIMARY_CODE}

    # ── Validation ───────────────────────────────────────────────────────────
    assert len(wf["nodes"]) == before, "node count changed"
    names = {n["name"] for n in wf["nodes"]}
    for src, outs in wf.get("connections", {}).items():
        assert src in names, f"dangling connection source: {src}"
        for grp in outs.get("main", []):
            for c in grp:
                assert c["node"] in names, f"dangling connection target: {c['node']}"
    blob = json.dumps(wf)
    assert "inspiring-cat" not in blob or "memory/export" in blob, "inspiring-cat still referenced for tasks"
    assert "/tasks" not in json.dumps(submit), "submit node still posts to /tasks"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)
    print(f"OK  {os.path.basename(path)}: {before} nodes, Claude CLI path -> DeepSeek+Groq")


if __name__ == "__main__":
    for fn in ("btc_main.json", "eth_main.json"):
        migrate(os.path.join(HERE, fn))
    print("Migration complete.")
