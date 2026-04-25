#!/bin/bash
# Smoke-test a list of free OpenRouter models with a tiny prompt.
# Reports OK / 429 / 4xx / other so we can pick a default that's actually
# serving traffic right now (free tiers rotate hot).
set -e
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY not set}"
MODELS=(
  "google/gemma-3-4b-it:free"
  "google/gemma-3-12b-it:free"
  "google/gemma-3-27b-it:free"
  "openai/gpt-oss-20b:free"
  "openai/gpt-oss-120b:free"
  "qwen/qwen3-coder:free"
  "nvidia/nemotron-nano-9b-v2:free"
  "nousresearch/hermes-3-llama-3.1-405b:free"
  "meta-llama/llama-3.2-3b-instruct:free"
  "meta-llama/llama-3.3-70b-instruct:free"
  "z-ai/glm-4.5-air:free"
  "minimax/minimax-m2.5:free"
)
for M in "${MODELS[@]}"; do
  body=$(curl -sS -m 15 -X POST https://openrouter.ai/api/v1/chat/completions \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" \
    -H "Content-Type: application/json" \
    -H "HTTP-Referer: https://legion-production-36db.up.railway.app" \
    -H "X-Title: Legion Engineer" \
    -d "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}" 2>&1)
  if echo "$body" | grep -q '"choices"'; then
    echo "OK   $M"
  elif echo "$body" | grep -q '"code":429'; then
    echo "429  $M  (rate-limited)"
  else
    snippet=$(echo "$body" | head -c 120 | tr -d '\n')
    echo "ERR  $M  $snippet"
  fi
done
