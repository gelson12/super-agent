#!/bin/bash
# Capture helper for CLI-subscription credentials. Run inside a live Legion
# container via `railway ssh --service Legion` AFTER a successful interactive
# login of the relevant CLI.
#
# Usage (from your laptop, not inside this script):
#   railway ssh --service Legion
#   # then inside the container:
#   kimi login                                   # browser/device-code flow
#   /app/scripts/capture_cli_creds.sh kimi       # prints base64 blob
#
# Copy the base64 output, paste into Railway → Legion → Variables:
#   KIMI_SESSION_TOKEN     (for kimi)
#   GEMINI_B_SESSION_TOKEN (for gemini_b)
#
# On next container restart, app.healing.cli_creds.restore_all() will decode
# and extract so the CLI is pre-authenticated without another login.

set -euo pipefail

TARGET="${1:-}"
case "$TARGET" in
  kimi)
    PATHS="/root/.local/share/kimi-cli /root/.config/kimi"
    ;;
  gemini_b|gemini)
    PATHS="/root/.gemini /root/.config/gemini"
    ;;
  claude_b|claude)
    PATHS="/root/.claude"
    ;;
  *)
    echo "Usage: $0 {kimi|gemini_b|claude_b}" >&2
    exit 1
    ;;
esac

EXISTING=""
for p in $PATHS; do
  if [ -e "$p" ]; then
    EXISTING="$EXISTING $p"
  fi
done
if [ -z "$EXISTING" ]; then
  echo "No credentials directory exists for $TARGET. Run the CLI's login first:" >&2
  case "$TARGET" in
    kimi)     echo "  kimi login" >&2 ;;
    gemini_b) echo "  gemini auth login" >&2 ;;
    claude_b) echo "  claude login" >&2 ;;
  esac
  exit 2
fi

echo "# Capturing: $EXISTING" >&2
echo "# Copy the single line below and paste into Railway env var." >&2
echo "# ---" >&2
tar czf - $EXISTING 2>/dev/null | base64 -w 0
echo ""
