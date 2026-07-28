"""Shared primitives for the command-backed Codex fork updater."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


UPSTREAM_URL = "https://github.com/openai/codex"
BASE_REF = "refs/statusline-fork/base"
PATCH_REF = "refs/statusline-fork/patch-tip"
RELEASE_TAG_RE = re.compile(r"^rust-v(\d+)\.(\d+)\.(\d+)$")


class UpdateError(RuntimeError):
    """An expected updater failure that must leave the active binary untouched."""


@dataclass(frozen=True)
class Layout:
    state_root: Path
    install_root: Path

    @property
    def config_path(self) -> Path:
        return self.state_root / "config.json"

    @property
    def source_repo(self) -> Path:
        return self.state_root / "source"

    @property
    def lock_path(self) -> Path:
        return self.state_root / "update.lock"

    @property
    def build_root(self) -> Path:
        return self.state_root / "builds"

    @property
    def cargo_target(self) -> Path:
        return self.state_root / "target"

    @property
    def manifest_path(self) -> Path:
        return self.install_root / "manifest.json"

    @property
    def updater_dir(self) -> Path:
        return self.install_root / "updater"

    @property
    def updater_path(self) -> Path:
        return self.updater_dir / "updater.py"

    @property
    def updater_lib_path(self) -> Path:
        return self.updater_dir / "updater_lib.py"

    @property
    def active_binary(self) -> Path:
        return self.install_root / "bin" / "codex"


def default_layout() -> Layout:
    home = Path.home()
    state_root = Path(
        os.environ.get(
            "CODEX_STATUSLINE_STATE_DIR",
            home / ".local" / "share" / "codex-statusline",
        )
    ).expanduser()
    install_root = Path(
        os.environ.get(
            "CODEX_STATUSLINE_INSTALL_DIR",
            home / ".local" / "lib" / "codex-statusline",
        )
    ).expanduser()
    return Layout(state_root.resolve(), install_root.resolve())


def release_version(tag: str) -> tuple[int, int, int]:
    match = RELEASE_TAG_RE.fullmatch(tag)
    if not match:
        raise UpdateError(f"unsupported Codex release tag: {tag}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def latest_release_tag(ls_remote_output: str) -> str:
    tags: set[str] = set()
    for line in ls_remote_output.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1].endswith("^{}"):
            continue
        tag = fields[1].removeprefix("refs/tags/")
        if RELEASE_TAG_RE.fullmatch(tag):
            tags.add(tag)
    if not tags:
        raise UpdateError("upstream did not return a stable rust-vX.Y.Z tag")
    return max(tags, key=release_version)


def command_output(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    timeout: int = 120,
) -> bytes:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"command failed to start: {args[0]}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise UpdateError(
            f"command exited {result.returncode}: {' '.join(args)}"
            + (f"\n{stderr}" if stderr else "")
        )
    return result.stdout


def run_logged(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> None:
    print(f"+ {' '.join(args)}", flush=True)
    try:
        result = subprocess.run(args, cwd=cwd, env=env, check=False)
    except OSError as exc:
        raise UpdateError(f"command failed to start: {args[0]}: {exc}") from exc
    if result.returncode != 0:
        raise UpdateError(f"command exited {result.returncode}: {' '.join(args)}")


def git_output(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    timeout: int = 120,
) -> bytes:
    return command_output(
        ["git", "-C", str(repo), *args],
        input_bytes=input_bytes,
        timeout=timeout,
    )


def git_text(repo: Path, *args: str, timeout: int = 120) -> str:
    return git_output(repo, *args, timeout=timeout).decode("utf-8").strip()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UpdateError(f"{path} must contain a JSON object")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def install_file_atomic(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    shutil.copy2(source, temporary)
    temporary.chmod(mode)
    os.replace(temporary, destination)


def discover_tools() -> dict[str, str]:
    tools: dict[str, str] = {}
    for name in ("cargo", "git", "just"):
        path = shutil.which(name)
        if not path:
            raise UpdateError(f"required command is not installed: {name}")
        # Preserve command shims such as ~/.cargo/bin/cargo -> rustup. Executing
        # the resolved target changes argv[0] and bypasses rustup's proxy mode.
        tools[name] = str(Path(path).absolute())
    return tools


def ensure_clean_source(source_repo: Path) -> None:
    if not (source_repo / ".git").exists():
        raise UpdateError(f"not a git worktree: {source_repo}")
    dirty = git_text(source_repo, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise UpdateError(
            "commit tracked status-line changes before installing the updater:\n"
            + dirty
        )


def clone_patch_source(source_repo: Path, managed_repo: Path, branch: str) -> None:
    if managed_repo.exists():
        if not (managed_repo / ".git").exists():
            raise UpdateError(f"managed source is not a git repository: {managed_repo}")
        command_output(
            [
                "git",
                "-C",
                str(managed_repo),
                "fetch",
                "--force",
                "--no-tags",
                str(source_repo),
                f"refs/heads/{branch}:refs/statusline-fork/install-source",
            ],
            timeout=600,
        )
    else:
        managed_repo.parent.mkdir(parents=True, exist_ok=True)
        command_output(
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--branch",
                branch,
                "--single-branch",
                str(source_repo),
                str(managed_repo),
            ],
            timeout=600,
        )
        git_output(managed_repo, "remote", "remove", "origin")
        git_output(managed_repo, "remote", "add", "upstream", UPSTREAM_URL)


def unit_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
