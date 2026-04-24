"""One-off: ping the Bridge Finance bot using Railway's actual env vars.

Builds a temp n8n workflow that reads $env.BRIDGE_FINANCE_BOT_TOKEN and
$env.BRIDGE_ADMIN_TELEGRAM_CHAT_ID from the Railway N8N service and calls
Telegram sendMessage. This bypasses any locally-cached token mismatch and
proves the production wiring works end-to-end.
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_phase0_apply_schema import load_env, run_workflow  # type: ignore


def build_workflow() -> dict:
    webhook_path = f"bridge-finance-bot-test-{int(time.time())}"
    return {
        "name": "_BRIDGE_FINANCE_BOT_TEST_TEMP",
        "nodes": [
            {
                "parameters": {
                    "httpMethod": "GET",
                    "path": webhook_path,
                    "responseMode": "lastNode",
                    "options": {},
                },
                "id": "wh",
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [240, 300],
                "webhookId": webhook_path,
            },
            {
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
                        '"text": "Bridge Finance bot — production wiring test via n8n env vars. '
                        'Token + chat ID resolved from Railway. If this lands, Phase 5 alerts will reach you here." }'
                    ),
                    "options": {"timeout": 20000, "response": {"response": {"fullResponse": True, "neverError": True}}},
                },
                "id": "tg",
                "name": "Telegram sendMessage",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [460, 300],
            },
        ],
        "connections": {
            "Webhook": {"main": [[{"node": "Telegram sendMessage", "type": "main", "index": 0}]]}
        },
        "settings": {"executionOrder": "v1"},
    }


def main() -> int:
    env = load_env()
    status, payload = run_workflow(env, build_workflow(), "finance-bot-test")
    print("\n=== Telegram response ===")
    print(json.dumps(payload, indent=2, default=str)[:1500])
    return status


if __name__ == "__main__":
    sys.exit(main())
