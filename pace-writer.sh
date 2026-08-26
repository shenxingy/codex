#!/usr/bin/env bash
# Demo scaffold for openai/codex#17827 — keep a fast local cache of Codex
# rate-limit data for the patched `custom` status-line item.
#
# The formatter reads $CODEX_HOME/statusline-usage.json. This loop refreshes
# that file out of band, decoupling the app-server round-trip from TUI renders.
#
# Usage:  ./pace-writer.sh [interval_seconds]
set -euo pipefail

INTERVAL="${1:-20}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
OUT="$CODEX_HOME/statusline-usage.json"
USAGE="$HOME/projects/Clade/configs/skills/codex-usage/scripts/codex_usage.py"

mkdir -p "$CODEX_HOME"
echo "pace-writer: $OUT every ${INTERVAL}s (Ctrl-C to stop)"
while true; do
  if python3 "$USAGE" --json > "$OUT.tmp" 2>/dev/null; then
    mv "$OUT.tmp" "$OUT"
  fi
  sleep "$INTERVAL"
done
