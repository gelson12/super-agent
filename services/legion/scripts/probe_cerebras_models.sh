#!/bin/bash
# List models the configured CEREBRAS_API_KEY can actually call.
# Usage (inside Legion container): /app/scripts/probe_cerebras_models.sh
set -e
: "${CEREBRAS_API_KEY:?CEREBRAS_API_KEY not set in env}"
curl -sS -H "Authorization: Bearer $CEREBRAS_API_KEY" \
  https://api.cerebras.ai/v1/models | python3 -m json.tool
