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
