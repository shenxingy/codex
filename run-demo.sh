#!/usr/bin/env bash
# One-command demo of the patched Codex command-backed status line (#17827).
# Uses Claude Code's status-line grammar with Codex quota data: directory,
# robbyrussell git segment, and quota pace styling.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HERE/codex-rs/target/debug/codex"     # the PATCHED binary (not `cx`)
FORMATTER="$HERE/codex-statusline-command.py"
WRITER="$HERE/pace-writer.sh"

"$WRITER" 20 >/dev/null 2>&1 &
trap 'kill $! 2>/dev/null || true' EXIT

# Keep `custom` as the only item: adding a native Codex field would introduce a
# separator and make the row differ from Claude Code's status line.
"$BIN" --dangerously-bypass-hook-trust \
  -c 'tui.status_line=["custom"]' \
  -c "tui.status_line_command=[\"python3\",\"$FORMATTER\"]"
