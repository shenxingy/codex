#!/usr/bin/env python3
"""Render cached Codex usage with Claude Code's status-line grammar."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


RESET = "\033[0m"
PACE_HONEY = "\033[38;2;212;180;119m"  # #D4B477
PACE_PEACH = "\033[38;2;210;160;135m"  # #D2A087
OVERPACE_PROJECTED = 125.0
THEMES = {
    "circles": ("○", "◑", "●", "◉"),
    "plain": ("--", "-", "+", "++"),
    "bird": ("🥚", "🐣", "🐥", "🦢"),
    "moon": ("🌑", "🌙", "🌛", "🌝"),
    "weather": ("🌩️", "🌧️", "🌤️", "🌈"),
    "mood": ("🫠", "😐", "😊", "🤩"),
    "coffee": ("😴", "☕", "💪", "⚡"),
    "rocket": ("🌍", "🚀", "🛸", "⭐"),
    "ocean": ("🫧", "🐠", "🐬", "🐋"),
    "dragon": ("🥚", "🦎", "🐉", "👑"),
}


def color(projected: float, delta: float) -> str:
    """Match Clade's warm pace palette and severe-overpacing threshold."""
    if delta >= 5 and projected > OVERPACE_PROJECTED:
        return PACE_PEACH
    return PACE_HONEY


def symbol(delta: float, theme: str) -> str:
    levels = THEMES.get(theme, THEMES["circles"])
    if delta < -15:
        return levels[0]
    if delta < -5:
        return levels[1]
    if delta < 5:
        return levels[2]
    return levels[3]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def git_branch(cwd: str) -> str:
    if not cwd:
        return ""
    for args in (
        ("symbolic-ref", "--short", "HEAD"),
        ("rev-parse", "--short", "HEAD"),
    ):
        try:
            result = subprocess.run(
                ["git", "-C", cwd, "-c", "gc.auto=0", *args],
                capture_output=True,
                check=False,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}

    cwd = str(payload.get("cwd") or payload.get("workspace", {}).get("current_dir") or "")
    directory = Path(cwd).name if cwd else ""
    branch = str(payload.get("git_branch") or "") or git_branch(cwd)

    output = f"\033[36m{directory}{RESET}"
    if branch:
        output += f" \033[1;34mgit:({RESET}\033[31m{branch}\033[1;34m){RESET}"

    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    try:
        rows = json.loads(read_text(codex_home / "statusline-usage.json"))
    except json.JSONDecodeError:
        rows = []
    primary = next(
        (row for row in rows if isinstance(row, dict) and row.get("limit_id") == "codex"),
        None,
    )
    if primary:
        delta = float(primary.get("pace_delta") or 0.0)
        display_delta = round(delta)
        sign = "+" if display_delta >= 0 else ""
        theme = read_text(codex_home / ".clade-usage-theme") or "circles"
        projected = float(primary.get("projected_percent") or 0.0)
        resets_in = str(primary.get("resets_in") or "?")
        output += (
            f" {symbol(delta, theme)} {color(projected, delta)}{sign}{display_delta}%"
            f"{RESET} ({resets_in})"
        )

    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
