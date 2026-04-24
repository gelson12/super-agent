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
    PATHS="/root/.kimi /root/.local/share/kimi-cli /root/.config/kimi"
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

# Railway env vars cap at 32KB. CLI dirs often include caches/logs/MCP
# registrations we don't need. We tar only credential-shaped files
# (*.json, *.yaml, *.yml, *.toml) while excluding obvious cache dirs.
FILTER_ARGS=( -type f \( -name "*.json" -o -name "*.yaml" -o -name "*.yml" -o -name "*.toml" \) )
EXCLUDE_DIRS=( -not -path "*/cache/*" -not -path "*/logs/*" -not -path "*/tmp/*" -not -path "*/mcp_servers/*" -not -path "*/extensions/*" )

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

echo "# Capturing config files only from: $EXISTING" >&2
# Collect matching files via find, tar from the list (null-delimited for safety)
FILE_LIST=$(mktemp)
for p in $EXISTING; do
  find "$p" "${FILTER_ARGS[@]}" "${EXCLUDE_DIRS[@]}" -print0 2>/dev/null >> "$FILE_LIST.0" || true
done
if [ ! -s "$FILE_LIST.0" ]; then
  echo "# No matching credential files found under $EXISTING" >&2
  echo "# Falling back to full-dir capture (may exceed Railway 32KB limit)" >&2
  tar czf /tmp/_creds.tar.gz $EXISTING 2>/dev/null
else
  tar czf /tmp/_creds.tar.gz --null -T "$FILE_LIST.0" 2>/dev/null
fi
rm -f "$FILE_LIST" "$FILE_LIST.0"

SIZE=$(stat -c%s /tmp/_creds.tar.gz 2>/dev/null || echo 0)
B64_SIZE=$(( SIZE * 4 / 3 ))
echo "# tarball size: $SIZE bytes; base64 approx: $B64_SIZE bytes" >&2
if [ "$B64_SIZE" -gt 32000 ]; then
  echo "# WARNING: base64 output will likely exceed Railway's 32KB var limit." >&2
  echo "# Contents captured (review + trim if needed):" >&2
  tar tzf /tmp/_creds.tar.gz >&2
fi
echo "# Copy the single line below and paste into Railway env var." >&2
echo "# ---" >&2
base64 -w 0 /tmp/_creds.tar.gz
echo ""
rm -f /tmp/_creds.tar.gz
