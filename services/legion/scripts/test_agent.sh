#!/bin/bash
# Test a specific Legion agent end-to-end with a HMAC-signed /v1/respond.
# Usage:  /app/scripts/test_agent.sh <agent_id> [query]
#         /app/scripts/test_agent.sh groq "say hello in 5 words"
#
# Forces the hive shortlist to only include the named agent so we know
# the response actually came from it (not a competing winner).

set -e
AGENT="${1:?agent_id required}"
QUERY="${2:-Reply with the single word 'pong'.}"
SECRET="${LEGION_API_SHARED_SECRET:?LEGION_API_SHARED_SECRET not set in env}"

# Build JSON body manually so HMAC matches what FastAPI receives byte-for-byte.
BODY=$(python3 -c "import json,sys; print(json.dumps({'query': sys.argv[1], 'complexity': 3, 'shortlist_override': [sys.argv[2]]}))" "$QUERY" "$AGENT")

TS=$(date +%s)
SIG=$(printf '%s\n%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')

echo "=== request ==="
echo "agent=$AGENT  query=\"${QUERY:0:60}...\""
echo ""
echo "=== response ==="
curl -sS -X POST http://127.0.0.1:8010/v1/respond \
  -H "Content-Type: application/json" \
  -H "X-Legion-Ts: $TS" \
  -H "X-Legion-Sig: $SIG" \
  -d "$BODY"
echo ""
