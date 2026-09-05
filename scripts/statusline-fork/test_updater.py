from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("updater.py")
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("statusline_fork_updater", SCRIPT)
assert SPEC and SPEC.loader
updater = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updater
SPEC.loader.exec_module(updater)


def test_latest_release_tag_ignores_prereleases_and_peeled_refs() -> None:
    output = "\n".join(
        [
            "a refs/tags/rust-v0.144.0",
            "b refs/tags/rust-v0.145.0-alpha.1",
            "c refs/tags/rust-v0.145.0",
            "d refs/tags/rust-v0.145.0^{}",
            "e refs/tags/python-v9.0.0",
        ]
    )

    assert updater.latest_release_tag(output) == "rust-v0.145.0"


def test_release_version_rejects_non_release_tags() -> None:
    with pytest.raises(updater.UpdateError):
        updater.release_version("main")


def test_write_json_atomic_replaces_complete_document(tmp_path: Path) -> None:
    path = tmp_path / "state" / "config.json"
    updater.write_json_atomic(path, {"version": 1})
    updater.write_json_atomic(path, {"version": 2, "ready": True})

    assert json.loads(path.read_text()) == {"ready": True, "version": 2}
    assert not list(path.parent.glob("*.tmp"))


def test_deploy_binary_backs_up_legacy_and_switches_symlink(
    tmp_path: Path,
) -> None:
    layout = updater.Layout(
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
    )
    layout.active_binary.parent.mkdir(parents=True)
    layout.active_binary.write_bytes(b"legacy")
    layout.active_binary.chmod(0o755)
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"new")
    candidate.chmod(0o755)

    manifest = updater.deploy_binary(
        layout,
        candidate,
        tag="rust-v0.145.0",
        build_commit="1234567890abcdef",
    )

    assert layout.active_binary.is_symlink()
    assert layout.active_binary.read_bytes() == b"new"
    assert manifest["version"] == "0.145.0"
    assert manifest["previousTarget"].startswith("releases/legacy-")
    assert (layout.install_root / manifest["previousTarget"]).read_bytes() == b"legacy"


def test_timer_is_persistent_and_does_not_build_on_session_start() -> None:
    timer = updater.update_timer_text()

    assert "OnActiveSec=10m" in timer
    assert "OnUnitActiveSec=6h" in timer
    assert "Persistent=true" in timer
    assert "SessionStart" not in timer


def test_usage_timer_runs_after_user_manager_startup() -> None:
    timer = updater.usage_timer_text()

    assert "OnActiveSec=30s" in timer
    assert "OnUnitActiveSec=2m" in timer
    assert "Unit=codex-statusline-usage.service" in timer


def test_prepare_worktree_resolves_managed_patch_refs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = updater.Layout(tmp_path / "state", tmp_path / "install")
    repo = tmp_path / "repo"
    git_calls: list[tuple[str, ...]] = []

    def fake_git_text(_repo: Path, *args: str, timeout: int = 120) -> str:
        del timeout
        git_calls.append(args)
        if args == ("rev-parse", updater.BASE_REF):
            return "base"
        if args == ("rev-parse", updater.PATCH_REF):
            return "tip"
        if args == ("rev-parse", "HEAD"):
            return "build"
        raise AssertionError(args)

    def fake_command_output(
        args: list[str],
        **_kwargs: object,
    ) -> bytes:
        if "diff" in args:
            return b"patch"
        return b""

    monkeypatch.setattr(updater, "git_text", fake_git_text)
    monkeypatch.setattr(updater, "command_output", fake_command_output)

    _worktree, build_commit = updater.prepare_release_worktree(
        layout,
        repo,
        "rust-v0.145.0",
        "/usr/bin/git",
    )

    assert build_commit == "build"
    assert ("rev-parse", updater.BASE_REF) in git_calls
    assert ("rev-parse", updater.PATCH_REF) in git_calls


def test_update_service_preserves_installer_path() -> None:
    text = updater.update_service_text(Path("/opt/codex updater/updater.py"), "/bin")

    assert 'ExecStart=/usr/bin/python3 "/opt/codex updater/updater.py" update' in text
    assert "Nice=10" in text


def test_usage_service_preserves_installer_path() -> None:
    text = updater.usage_service_text(Path("/opt/refresh usage"), "/custom/bin")

    assert 'Environment=PATH="/custom/bin"' in text
    assert 'ExecStart="/opt/refresh usage"' in text


def test_default_layout_respects_test_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CODEX_STATUSLINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_STATUSLINE_INSTALL_DIR", str(tmp_path / "install"))

    layout = updater.default_layout()

    assert layout.state_root == (tmp_path / "state").resolve()
    assert layout.install_root == (tmp_path / "install").resolve()


def test_discover_tools_preserves_command_shims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rustup = tmp_path / "rustup"
    rustup.touch()
    cargo = tmp_path / "cargo"
    cargo.symlink_to(rustup)
    paths = {
        "cargo": cargo,
        "git": tmp_path / "git",
        "just": tmp_path / "just",
    }
    paths["git"].touch()
    paths["just"].touch()
    monkeypatch.setattr(
        updater.shutil,
        "which",
        lambda name: str(paths[name]),
    )

    tools = updater.discover_tools()

    assert tools["cargo"] == str(cargo.absolute())
    assert tools["cargo"] != str(rustup.resolve())


# ── the code-mode host ships beside codex ────────────────────────────────────
import hashlib  # noqa: E402

import updater_lib  # noqa: E402


def _layout(tmp_path: Path) -> "updater.Layout":
    return updater.Layout(state_root=tmp_path / "state", install_root=tmp_path / "install")


def _executable(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def test_deploy_binary_places_the_code_mode_host_beside_codex(tmp_path: Path) -> None:
    """0.153.4 deployed on 2026-09-04 had only `codex`; every session warned
    'Code Mode is unavailable ... host executable was not found'."""
    layout = _layout(tmp_path)
    codex = _executable(tmp_path / "build" / "codex", b"codex")
    host = _executable(tmp_path / "build" / "codex-code-mode-host", b"host")

    manifest = updater.deploy_binary(
        layout, codex, tag="rust-v0.153.4", build_commit="26e3786e44690f30",
        companions={updater_lib.CODE_MODE_HOST_BINARY: host},
    )

    release_dir = layout.install_root / "releases" / "0.153.4-26e3786e4469"
    assert (release_dir / "codex-code-mode-host").read_bytes() == b"host"
    assert os.access(release_dir / "codex-code-mode-host", os.X_OK)
    assert layout.active_code_mode_host() == release_dir / "codex-code-mode-host"
    assert updater.release_is_complete(layout)
    assert manifest["companions"] == ["codex-code-mode-host"]


def test_deploy_binary_repairs_a_release_that_lacks_the_host(tmp_path: Path) -> None:
    """An existing release directory keeps its name and its `codex`; only the
    missing companion is added. This is the path that heals today's install."""
    layout = _layout(tmp_path)
    codex = _executable(tmp_path / "build" / "codex", b"codex")
    updater.deploy_binary(layout, codex, tag="rust-v0.153.4", build_commit="26e3786e44690f30")
    assert not updater.release_is_complete(layout)

    host = _executable(tmp_path / "build" / "codex-code-mode-host", b"host")
    rebuilt_codex = _executable(tmp_path / "build2" / "codex", b"codex-rebuilt")
    updater.deploy_binary(
        layout, rebuilt_codex, tag="rust-v0.153.4", build_commit="26e3786e44690f30",
        companions={updater_lib.CODE_MODE_HOST_BINARY: host},
    )
    release_dir = layout.install_root / "releases" / "0.153.4-26e3786e4469"
    assert (release_dir / "codex").read_bytes() == b"codex", "existing release is kept"
    assert (release_dir / "codex-code-mode-host").read_bytes() == b"host"
    assert updater.release_is_complete(layout)


def _fake_release(files: dict[str, bytes]):
    """A downloader serving `files` by basename, recording what was asked."""
    asked: list[str] = []

    def download(url: str, destination: Path) -> None:
        asked.append(url)
        name = url.rsplit("/", 1)[1]
        if name not in files:
            raise updater_lib.UpdateError(f"download failed: {url}: 404")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(files[name])

    return download, asked


def _manifest(files: dict[str, bytes]) -> bytes:
    return "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\r\n" for name, data in files.items()
    ).encode()


def test_rusty_v8_artifacts_come_from_the_codex_release_and_are_verified(tmp_path: Path) -> None:
    target = "x86_64-unknown-linux-gnu"
    archive, binding, checksums = updater_lib.rusty_v8_artifact_names(target)
    payload = {archive: b"v8-archive", binding: b"// binding"}
    download, asked = _fake_release({**payload, checksums: _manifest(payload)})

    artifacts = updater_lib.fetch_rusty_v8_artifacts(
        tmp_path / "rusty_v8", "150.4.0", target, downloader=download
    )

    assert asked[0].startswith("https://github.com/openai/codex/releases/download/rusty-v8-v150.4.0/")
    assert archive == "librusty_v8_ptrcomp_sandbox_release_x86_64-unknown-linux-gnu.a.gz"
    assert artifacts.archive.read_bytes() == b"v8-archive"
    assert artifacts.environment() == {
        "RUSTY_V8_ARCHIVE": str(artifacts.archive),
        "RUSTY_V8_SRC_BINDING_PATH": str(artifacts.binding),
    }
    # A second call is served from the cache.
    updater_lib.fetch_rusty_v8_artifacts(tmp_path / "rusty_v8", "150.4.0", target, downloader=download)
    assert len(asked) == 3


def test_tampered_rusty_v8_archive_is_rejected_and_not_cached(tmp_path: Path) -> None:
    target = "x86_64-unknown-linux-gnu"
    archive, binding, checksums = updater_lib.rusty_v8_artifact_names(target)
    good = {archive: b"v8-archive", binding: b"// binding"}
    download, _ = _fake_release({archive: b"TAMPERED", binding: b"// binding", checksums: _manifest(good)})

    with pytest.raises(updater_lib.UpdateError, match="checksum mismatch"):
        updater_lib.fetch_rusty_v8_artifacts(tmp_path / "rusty_v8", "150.4.0", target, downloader=download)
    assert not (tmp_path / "rusty_v8" / "150.4.0" / archive).exists(), "bad bytes must not be reused"


def test_a_checksum_manifest_that_covers_fewer_files_is_not_a_pass(tmp_path: Path) -> None:
    target = "x86_64-unknown-linux-gnu"
    archive, binding, checksums = updater_lib.rusty_v8_artifact_names(target)
    download, _ = _fake_release({archive: b"a", binding: b"b", checksums: _manifest({archive: b"a"})})
    with pytest.raises(updater_lib.UpdateError, match="expected"):
        updater_lib.fetch_rusty_v8_artifacts(tmp_path / "rusty_v8", "150.4.0", target, downloader=download)


def test_windows_archive_name_uses_the_lib_suffix() -> None:
    archive, _binding, _checksums = updater_lib.rusty_v8_artifact_names("x86_64-pc-windows-msvc")
    assert archive == "rusty_v8_ptrcomp_sandbox_release_x86_64-pc-windows-msvc.lib.gz"


def test_missing_upstream_version_resolver_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(updater_lib.UpdateError, match="rusty_v8_bazel.py"):
        updater_lib.resolved_v8_crate_version(tmp_path)


def test_build_environment_carries_the_rusty_v8_overrides(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    env = updater.build_environment(
        layout, {"environmentPath": "/bin"}, {"RUSTY_V8_ARCHIVE": "/x.a.gz"}
    )
    assert env["RUSTY_V8_ARCHIVE"] == "/x.a.gz"
    assert env["CARGO_TARGET_DIR"] == str(layout.cargo_target)
