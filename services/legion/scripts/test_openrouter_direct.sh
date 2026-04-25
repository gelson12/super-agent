#!/bin/bash
# Direct curl against OpenRouter to surface the real error body (the agent
# wrapper only logs HTTP 404 — useful for diagnosing model availability,
# auth scope, or referer requirements).
set -e
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY not set}"
MODEL="${1:-meta-llama/llama-3.3-70b-instruct:free}"
curl -sS -X POST https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -H "HTTP-Referer: https://legion-production-36db.up.railway.app" \
  -H "X-Title: Legion Engineer" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":20}"
echo ""
