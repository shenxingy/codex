#!/usr/bin/env python3
"""Safely keep the command-backed Codex status-line fork on current releases."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from updater_lib import BASE_REF
from updater_lib import PATCH_REF
from updater_lib import UPSTREAM_URL
from updater_lib import Layout
from updater_lib import UpdateError
from updater_lib import clone_patch_source
from updater_lib import command_output
from updater_lib import default_layout
from updater_lib import discover_tools
from updater_lib import ensure_clean_source
from updater_lib import git_output
from updater_lib import git_text
from updater_lib import install_file_atomic
from updater_lib import latest_release_tag
from updater_lib import read_json
from updater_lib import release_version
from updater_lib import run_logged
from updater_lib import unit_quote
from updater_lib import write_json_atomic


UPDATE_SERVICE = "codex-statusline-update.service"
UPDATE_TIMER = "codex-statusline-update.timer"
USAGE_SERVICE = "codex-statusline-usage.service"
USAGE_TIMER = "codex-statusline-usage.timer"


def update_service_text(updater_path: Path, environment_path: str) -> str:
    return f"""[Unit]
Description=Update the command-backed Codex status-line fork
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PATH={unit_quote(environment_path)}
ExecStart=/usr/bin/python3 {unit_quote(str(updater_path))} update
Nice=10
IOSchedulingClass=idle
"""


def update_timer_text() -> str:
    return """[Unit]
Description=Check for a new Codex release every six hours

[Timer]
OnStartupSec=10m
OnUnitActiveSec=6h
RandomizedDelaySec=15m
Persistent=true
Unit=codex-statusline-update.service

[Install]
WantedBy=timers.target
"""


def usage_service_text(refresh_path: Path) -> str:
    return f"""[Unit]
Description=Refresh cached Codex usage for the custom status line
After=network-online.target
Wants=network-online.target
ConditionFileIsExecutable={refresh_path}

[Service]
Type=oneshot
ExecStart={unit_quote(str(refresh_path))}
"""


def usage_timer_text() -> str:
    return """[Unit]
Description=Refresh custom Codex usage every two minutes

[Timer]
OnStartupSec=30s
OnUnitActiveSec=2m
Persistent=true
Unit=codex-statusline-usage.service

[Install]
WantedBy=timers.target
"""


def install_systemd_units(layout: Layout, environment_path: str) -> None:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    units = {
        UPDATE_SERVICE: update_service_text(layout.updater_path, environment_path),
        UPDATE_TIMER: update_timer_text(),
    }
    refresh_path = layout.install_root / "refresh-usage"
    if refresh_path.is_file() and os.access(refresh_path, os.X_OK):
        units[USAGE_SERVICE] = usage_service_text(refresh_path)
        units[USAGE_TIMER] = usage_timer_text()
    for name, contents in units.items():
        path = unit_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(contents, encoding="utf-8")
        os.replace(temporary, path)
    command_output(["systemctl", "--user", "daemon-reload"])
    command_output(["systemctl", "--user", "enable", UPDATE_TIMER])
    command_output(["systemctl", "--user", "restart", UPDATE_TIMER])
    if USAGE_TIMER in units:
        command_output(["systemctl", "--user", "enable", USAGE_TIMER])
        command_output(["systemctl", "--user", "restart", USAGE_TIMER])


def install(args: argparse.Namespace, layout: Layout) -> None:
    source_repo = Path(args.source_repo).expanduser().resolve()
    ensure_clean_source(source_repo)
    branch = args.branch or git_text(source_repo, "branch", "--show-current")
    if not branch:
        raise UpdateError("the patch source must be on a named branch")
    base_tag = args.base_tag or git_text(
        source_repo, "describe", "--tags", "--abbrev=0", "HEAD"
    )
    release_version(base_tag)
    patch_tip = git_text(source_repo, "rev-parse", "HEAD")
    base_commit = git_text(source_repo, "rev-parse", f"{base_tag}^{{commit}}")
    if patch_tip == base_commit:
        raise UpdateError("patch branch has no commit beyond its release base")

    clone_patch_source(source_repo, layout.source_repo, branch)
    git_output(layout.source_repo, "update-ref", BASE_REF, base_commit)
    git_output(layout.source_repo, "update-ref", PATCH_REF, patch_tip)

    tools = discover_tools()
    config = {
        "schemaVersion": 1,
        "upstreamUrl": UPSTREAM_URL,
        "baseTag": base_tag,
        "environmentPath": os.environ.get("PATH", ""),
        "tools": tools,
    }
    write_json_atomic(layout.config_path, config)
    source_dir = Path(__file__).resolve().parent
    install_file_atomic(source_dir / "updater.py", layout.updater_path, 0o755)
    install_file_atomic(
        source_dir / "updater_lib.py",
        layout.updater_lib_path,
        0o644,
    )

    if not args.no_systemd:
        install_systemd_units(layout, config["environmentPath"])
    print(f"Installed updater: {layout.updater_path}")
    print(f"Managed source:    {layout.source_repo}")
    print(f"Patch base:        {base_tag}")


def remote_latest_tag(repo: Path, git_path: str) -> str:
    output = command_output(
        [git_path, "-C", str(repo), "ls-remote", "--tags", "upstream"],
        timeout=180,
    ).decode("utf-8")
    return latest_release_tag(output)


def build_environment(layout: Layout, config: dict[str, Any]) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PATH"] = str(
        config.get("environmentPath") or environment.get("PATH", "")
    )
    environment["CARGO_TARGET_DIR"] = str(layout.cargo_target)
    return environment


def validate_worktree_path(layout: Layout, worktree: Path) -> None:
    try:
        worktree.resolve().relative_to(layout.build_root.resolve())
    except ValueError as exc:
        raise UpdateError(
            f"refusing to clean worktree outside build root: {worktree}"
        ) from exc


def prepare_release_worktree(
    layout: Layout,
    repo: Path,
    tag: str,
    git_path: str,
) -> tuple[Path, str]:
    base_commit = git_text(repo, "rev-parse", BASE_REF)
    patch_tip = git_text(repo, "rev-parse", PATCH_REF)
    patch = command_output(
        [
            git_path,
            "-C",
            str(repo),
            "diff",
            "--binary",
            base_commit,
            patch_tip,
            "--",
            "codex-rs",
        ]
    )
    if not patch:
        raise UpdateError("managed status-line patch is empty")

    layout.build_root.mkdir(parents=True, exist_ok=True)
    worktree = layout.build_root / f"{tag}-{uuid.uuid4().hex}"
    command_output(
        [git_path, "-C", str(repo), "worktree", "add", "--detach", str(worktree), tag],
        timeout=300,
    )
    try:
        command_output(
            [git_path, "-C", str(worktree), "apply", "--index", "--3way", "-"],
            input_bytes=patch,
            timeout=300,
        )
        command_output(
            [
                git_path,
                "-C",
                str(worktree),
                "-c",
                "user.name=Codex Statusline Updater",
                "-c",
                "user.email=codex-statusline@localhost",
                "commit",
                "-m",
                f"feat(tui): apply command-backed status line to {tag}",
            ]
        )
        build_commit = git_text(worktree, "rev-parse", "HEAD")
    except Exception:
        cleanup_release_worktree(layout, repo, worktree, git_path)
        raise
    return worktree, build_commit


def cleanup_release_worktree(
    layout: Layout,
    repo: Path,
    worktree: Path,
    git_path: str,
) -> None:
    validate_worktree_path(layout, worktree)
    subprocess.run(
        [git_path, "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def build_and_test(
    layout: Layout,
    worktree: Path,
    config: dict[str, Any],
) -> Path:
    tools = config.get("tools")
    if not isinstance(tools, dict):
        raise UpdateError("updater config is missing tool paths")
    just_path = str(tools.get("just") or "")
    cargo_path = str(tools.get("cargo") or "")
    if not just_path or not cargo_path:
        raise UpdateError("updater config is missing cargo or just")
    codex_rs = worktree / "codex-rs"
    environment = build_environment(layout, config)
    run_logged(
        [
            just_path,
            "test",
            "-p",
            "codex-tui",
            "-E",
            "test(status_line_command) | test(custom_status_line)",
        ],
        cwd=codex_rs,
        env=environment,
    )
    run_logged(
        [
            cargo_path,
            "build",
            "-p",
            "codex-cli",
            "--bin",
            "codex",
            "--profile",
            "dev-small",
        ],
        cwd=codex_rs,
        env=environment,
    )
    binary = layout.cargo_target / "dev-small" / "codex"
    if not binary.is_file():
        raise UpdateError(f"build succeeded without producing {binary}")
    return binary


def backup_legacy_binary(layout: Layout) -> str | None:
    active = layout.active_binary
    if not active.exists() or active.is_symlink():
        return None
    digest = hashlib.sha256(active.read_bytes()).hexdigest()[:12]
    backup_name = f"legacy-{int(time.time())}-{digest}"
    backup_dir = layout.install_root / "releases" / backup_name
    backup_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(active, backup_dir / "codex")
    return str((backup_dir / "codex").relative_to(layout.install_root))


def deploy_binary(
    layout: Layout,
    binary: Path,
    *,
    tag: str,
    build_commit: str,
) -> dict[str, Any]:
    version = ".".join(str(part) for part in release_version(tag))
    release_name = f"{version}-{build_commit[:12]}"
    releases_dir = layout.install_root / "releases"
    release_dir = releases_dir / release_name
    releases_dir.mkdir(parents=True, exist_ok=True)
    legacy_backup = backup_legacy_binary(layout)

    if not release_dir.exists():
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{release_name}.", dir=str(releases_dir))
        )
        try:
            shutil.copy2(binary, temporary_dir / "codex")
            (temporary_dir / "codex").chmod(0o755)
            os.replace(temporary_dir, release_dir)
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir)

    active = layout.active_binary
    active.parent.mkdir(parents=True, exist_ok=True)
    previous_target: str | None = None
    if active.is_symlink():
        previous_target = os.readlink(active)
    elif legacy_backup:
        previous_target = legacy_backup
    temporary_link = active.with_name(f".codex.{uuid.uuid4().hex}.tmp")
    relative_target = os.path.relpath(release_dir / "codex", active.parent)
    os.symlink(relative_target, temporary_link)
    os.replace(temporary_link, active)

    manifest = {
        "schemaVersion": 1,
        "version": version,
        "tag": tag,
        "buildCommit": build_commit,
        "activeTarget": relative_target,
        "previousTarget": previous_target,
        "deployedAt": int(time.time()),
    }
    write_json_atomic(layout.manifest_path, manifest)
    return manifest


def validate_built_version(binary: Path, tag: str) -> None:
    expected = ".".join(str(part) for part in release_version(tag))
    output = (
        command_output([str(binary), "--version"], timeout=30).decode("utf-8").strip()
    )
    if output != f"codex-cli {expected}":
        raise UpdateError(
            f"built binary version mismatch: expected codex-cli {expected}, got {output!r}"
        )


def update(layout: Layout) -> None:
    config = read_json(layout.config_path)
    git_path = str((config.get("tools") or {}).get("git") or "git")
    layout.state_root.mkdir(parents=True, exist_ok=True)
    with layout.lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Codex status-line update is already running.")
            return

        tag = remote_latest_tag(layout.source_repo, git_path)
        manifest = (
            read_json(layout.manifest_path) if layout.manifest_path.is_file() else {}
        )
        active_is_valid = layout.active_binary.is_file()
        if manifest.get("tag") == tag and active_is_valid:
            print(f"Codex status-line fork is current: {tag}")
            return

        print(f"Updating Codex status-line fork to {tag}", flush=True)
        command_output(
            [
                git_path,
                "-C",
                str(layout.source_repo),
                "fetch",
                "--force",
                "upstream",
                f"refs/tags/{tag}:refs/tags/{tag}",
            ],
            timeout=600,
        )
        worktree, build_commit = prepare_release_worktree(
            layout, layout.source_repo, tag, git_path
        )
        try:
            binary = build_and_test(layout, worktree, config)
            validate_built_version(binary, tag)
            deployed = deploy_binary(
                layout,
                binary,
                tag=tag,
                build_commit=build_commit,
            )
            tag_commit = git_text(layout.source_repo, "rev-parse", f"{tag}^{{commit}}")
            git_output(layout.source_repo, "update-ref", BASE_REF, tag_commit)
            git_output(layout.source_repo, "update-ref", PATCH_REF, build_commit)
            config["baseTag"] = tag
            write_json_atomic(layout.config_path, config)
            print(
                f"Activated Codex {deployed['version']} at "
                f"{layout.active_binary.resolve()}"
            )
        finally:
            cleanup_release_worktree(layout, layout.source_repo, worktree, git_path)


def status(layout: Layout, online: bool) -> None:
    config = read_json(layout.config_path)
    manifest = read_json(layout.manifest_path) if layout.manifest_path.is_file() else {}
    print(f"Active binary: {layout.active_binary}")
    print(f"Resolved to:   {layout.active_binary.resolve(strict=False)}")
    print(f"Installed:     {manifest.get('tag', 'not managed yet')}")
    print(f"Patch base:    {config.get('baseTag', 'unknown')}")
    if online:
        git_path = str((config.get("tools") or {}).get("git") or "git")
        print(f"Latest:        {remote_latest_tag(layout.source_repo, git_path)}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Update the command-backed Codex status-line fork safely."
    )
    subparsers = result.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--source-repo", required=True)
    install_parser.add_argument("--branch")
    install_parser.add_argument("--base-tag")
    install_parser.add_argument("--no-systemd", action="store_true")
    subparsers.add_parser("update")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--online", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    layout = default_layout()
    try:
        if args.command == "install":
            install(args, layout)
        elif args.command == "update":
            update(layout)
        elif args.command == "status":
            status(layout, args.online)
        else:
            raise UpdateError(f"unsupported command: {args.command}")
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
