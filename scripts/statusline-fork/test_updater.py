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

    assert "OnUnitActiveSec=6h" in timer
    assert "Persistent=true" in timer
    assert "SessionStart" not in timer


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
