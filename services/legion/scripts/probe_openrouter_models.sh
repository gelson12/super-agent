#!/bin/bash
# List free OpenRouter models the configured key can call.
# Usage (inside Legion container): /app/scripts/probe_openrouter_models.sh
set -e
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY not set in env}"
curl -sS -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  https://openrouter.ai/api/v1/models \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
free=[m for m in d['data'] if str(m.get('id','')).endswith(':free')]
free.sort(key=lambda m: m['id'])
for m in free:
    print(m['id'])
print(f'--- {len(free)} free models total ---')
"
