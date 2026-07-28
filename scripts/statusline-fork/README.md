# Codex status-line fork updater

This updater keeps the locally built, command-backed status-line fork on the
latest stable `rust-vX.Y.Z` Codex release. It is intentionally independent of
Codex session startup: a user-level systemd timer checks every six hours, and a
session always starts with the last successfully installed binary.

## Safety model

For each new stable release the updater:

1. fetches the release tag;
2. reapplies the tracked `codex-rs` patch in a temporary worktree;
3. runs the focused status-line tests;
4. builds Codex and verifies that `codex --version` exactly matches the tag;
5. copies the binary into a versioned release directory; and
6. atomically switches the active symlink.

Any fetch, patch, test, build, or version-validation failure leaves the active
binary and patch baseline unchanged. The first managed deployment also keeps a
copy of the legacy binary.

## Install

Run this from a clean, committed patch branch:

```sh
python3 scripts/statusline-fork/updater.py install \
  --source-repo "$PWD" \
  --branch wt/automate-codex-fork-updates
```

The installer creates:

- managed source and build state under
  `~/.local/share/codex-statusline/`;
- versioned binaries and the updater under
  `~/.local/lib/codex-statusline/`;
- `codex-statusline-update.timer`, which checks every six hours; and
- `codex-statusline-usage.timer`, which refreshes the footer usage
  cache every two minutes when `refresh-usage` is installed.

## Operate

```sh
# Check installed and upstream versions
~/.local/lib/codex-statusline/updater/updater.py status --online

# Run an update immediately
~/.local/lib/codex-statusline/updater/updater.py update

# Inspect timers and updater logs
systemctl --user list-timers 'codex-statusline-*'
journalctl --user -u codex-statusline-update.service
```

The active and previous binary targets are recorded in
`~/.local/lib/codex-statusline/manifest.json`.
