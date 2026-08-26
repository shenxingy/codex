# feat(tui): command-backed `custom` status-line item

Implements the command-backed status line requested in #17827 — the extension
point that lets tools render their own segment (à la Claude Code's
`statusLine.command` and the `ccstatusline` ecosystem), rather than picking from
the fixed built-in field set.

## What it does

Additive to the existing `[tui].status_line` field picker:

```toml
[tui]
status_line = ["custom", "git-branch", "weekly-limit"]
status_line_command = ["my-statusline"]
```

- A new `custom` status-line item renders the output of `status_line_command`.
- Codex runs the command on a 5s timer, writes a small JSON session snapshot to
  its **stdin**, and renders the **first non-empty line of stdout** wherever
  `custom` appears in `status_line`.
- Current stdin payload: `{"cwd": "...", "git_branch": "...", "model": "..."}`
  (easy to extend with more fields).
- ANSI SGR styling from the command is preserved span-by-span, so an existing
  Claude Code formatter renders with the same colors in the Codex footer.
- The run is bounded by a **1.5s timeout with kill-on-drop** and **fails open**:
  on timeout, non-zero exit, or spawn error the previous value is kept and the
  TUI is never blocked.
- Refresh is driven by the existing frame scheduler, mirroring the async
  `workspace-headline` status item — no new runtime.

## Why

`[tui].status_line` is a fixed enum of built-in ids, so segments like a
themed rate-limit pace indicator, jj/VCS state, or a spend HUD can't be
rendered in the footer today (see the use cases collected in #17827). This is
the same capability PR #10170 first explored; the status surface has since been
refactored into `tui/src/bottom_pane/`, so this is a fresh implementation on the
current architecture.

## Design notes

- `status_line_command: Option<Vec<String>>` on `[tui]` (argv, not a shell
  string) → resolved onto `Config.tui_status_line_command`.
- `AppEvent::RefreshStatusLineCommand { request_id, command, stdin_json }`
  captures argv + payload at request time so the spawned process needs no
  chat-widget state; the async runner replies with
  `AppEvent::StatusLineCommandUpdated { request_id, result }`.
- Request-id correlation + `pending`/`last_requested_at` guards mirror the
  workspace-headline item exactly.

## Testing

- Unit tests for the command runner: first-line extraction, stdin passthrough,
  empty output → `None`, non-zero exit / empty argv → `Err`, and timeout.
- `config_schema_matches_fixture` regenerated; all 66 `status_line` tui tests
  pass.
- Manually verified in the TUI: the `custom` item renders the command's stdout
  and the JSON snapshot arrives on the command's stdin.

## Possible follow-ups

- Configurable refresh interval / timeout.
- Richer stdin snapshot (rate limits, context %, approval mode, …).

Happy to iterate on scope, payload shape, or config naming.
