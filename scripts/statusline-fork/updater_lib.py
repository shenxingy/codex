"""Shared primitives for the command-backed Codex fork updater."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


UPSTREAM_URL = "https://github.com/openai/codex"
BASE_REF = "refs/statusline-fork/base"
PATCH_REF = "refs/statusline-fork/patch-tip"
RELEASE_TAG_RE = re.compile(r"^rust-v(\d+)\.(\d+)\.(\d+)$")

#: Since 0.153 Codex spawns this sibling executable for Code Mode and fails
#: closed when it is missing ("Code Mode is unavailable because failed to spawn
#: code-mode host ...: host executable was not found"). It ships next to
#: `codex` in every upstream release bundle, so it ships next to ours.
CODE_MODE_HOST_BINARY = "codex-code-mode-host"

#: The host embeds V8. rusty_v8's build script downloads a prebuilt archive
#: named after its enabled features, and the `ptrcomp_sandbox` variant Codex
#: enables is not published by denoland/rusty_v8 -- Codex builds and publishes
#: it under its own `rusty-v8-v<version>` release, which is what upstream CI
#: points the build at (`.github/actions/setup-rusty-v8`). Same recipe here.
RUSTY_V8_RELEASE_BASE = "https://github.com/openai/codex/releases/download"
RUSTY_V8_PROFILE = "ptrcomp_sandbox_release"
RUSTY_V8_VERSION_SCRIPT = ".github/scripts/rusty_v8_bazel.py"


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

    @property
    def rusty_v8_cache(self) -> Path:
        return self.state_root / "rusty_v8"

    def active_code_mode_host(self) -> Path:
        """Where Codex will look for the host: beside the resolved binary."""
        return self.active_binary.resolve(strict=False).parent / CODE_MODE_HOST_BINARY


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


# ─── rusty_v8 prebuilt artifacts (needed to build codex-code-mode-host) ─────
@dataclass(frozen=True)
class RustyV8Artifacts:
    archive: Path
    binding: Path

    def environment(self) -> dict[str, str]:
        return {
            "RUSTY_V8_ARCHIVE": str(self.archive),
            "RUSTY_V8_SRC_BINDING_PATH": str(self.binding),
        }


def rusty_v8_artifact_names(target: str) -> tuple[str, str, str]:
    """(archive, binding, checksums) file names, exactly as upstream publishes."""
    profile = RUSTY_V8_PROFILE
    if target.endswith("-pc-windows-msvc"):
        archive = f"rusty_v8_{profile}_{target}.lib.gz"
    else:
        archive = f"librusty_v8_{profile}_{target}.a.gz"
    return archive, f"src_binding_{profile}_{target}.rs", f"rusty_v8_{profile}_{target}.sha256"


def resolved_v8_crate_version(worktree: Path) -> str:
    """The v8 crate version the checkout pins, via upstream's own resolver."""
    script = worktree / RUSTY_V8_VERSION_SCRIPT
    if not script.is_file():
        raise UpdateError(
            f"cannot resolve the rusty_v8 version: {RUSTY_V8_VERSION_SCRIPT} is "
            "missing from this release; the updater needs adapting"
        )
    output = command_output(["python3", str(script), "resolved-v8-crate-version"], cwd=worktree)
    version = output.decode("utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise UpdateError(f"unexpected rusty_v8 version from upstream resolver: {version!r}")
    return version


def host_target(cargo_path: str) -> str:
    """The toolchain's host triple, from the rustc beside cargo."""
    rustc = Path(cargo_path).with_name("rustc")
    output = command_output([str(rustc) if rustc.is_file() else "rustc", "-vV"]).decode("utf-8")
    for line in output.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ").strip()
    raise UpdateError("rustc -vV did not report a host target")


def download_file(url: str, destination: Path, *, timeout: int = 600) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, temporary.open("wb") as out:
            shutil.copyfileobj(response, out)
        os.replace(temporary, destination)
    except (OSError, ValueError) as exc:
        raise UpdateError(f"download failed: {url}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def verify_sha256_manifest(directory: Path, checksums: Path, expected_names: set[str]) -> None:
    """Check every listed file; the manifest must cover exactly the expected names.

    Upstream's manifests are two `<sha256>  <name>` lines (CRLF from the Windows
    builders), and a manifest that names fewer files than we rely on is treated
    as a failure rather than a pass -- a partial check is not a check.
    """
    listed: dict[str, str] = {}
    for raw in checksums.read_text(encoding="utf-8").replace("\r", "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64:
            raise UpdateError(f"malformed checksum line in {checksums.name}: {line!r}")
        listed[parts[1].lstrip("*")] = parts[0].lower()
    if set(listed) != expected_names:
        raise UpdateError(
            f"{checksums.name} lists {sorted(listed)}, expected {sorted(expected_names)}"
        )
    for name, digest in listed.items():
        path = directory / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise UpdateError(f"checksum mismatch for {name}: expected {digest}, got {actual}")


def fetch_rusty_v8_artifacts(
    cache_dir: Path,
    version: str,
    target: str,
    *,
    downloader=download_file,
) -> RustyV8Artifacts:
    """Fetch (once) and verify the Codex-built V8 archive + binding for `target`.

    Files land under `<cache_dir>/<version>/`. Anything that fails verification
    is deleted so the next run re-downloads instead of trusting a bad cache.
    """
    archive_name, binding_name, checksums_name = rusty_v8_artifact_names(target)
    release_dir = cache_dir / version
    base_url = f"{RUSTY_V8_RELEASE_BASE}/rusty-v8-v{version}"
    for name in (archive_name, binding_name, checksums_name):
        path = release_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            downloader(f"{base_url}/{name}", path)
    try:
        verify_sha256_manifest(
            release_dir, release_dir / checksums_name, {archive_name, binding_name}
        )
    except UpdateError:
        for name in (archive_name, binding_name, checksums_name):
            (release_dir / name).unlink(missing_ok=True)
        raise
    return RustyV8Artifacts(
        archive=release_dir / archive_name, binding=release_dir / binding_name
    )
