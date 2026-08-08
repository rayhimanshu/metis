"""install-hooks tests.

Overwriting a settings file is a bad trade: whatever else was in there was put
there deliberately.
"""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

from metis.hook_commands import _merge_hooks, cmd_install_hooks
from metis.hooks import SETTINGS_TEMPLATE


@pytest.fixture
def template() -> dict:
    return json.loads(SETTINGS_TEMPLATE.read_text(encoding="utf-8"))


def _commands(settings: dict, event: str) -> list[str]:
    return [h["command"] for block in settings["hooks"][event] for h in block["hooks"]]


def test_merges_into_an_empty_settings_file(template):
    merged, added = _merge_hooks({}, template)
    assert added == 2
    assert _commands(merged, "PreToolUse") == ["metis hook pre"]


def test_existing_unrelated_settings_survive(template):
    existing = {"permissions": {"allow": ["Bash(git status)"]}, "model": "opus"}
    merged, _ = _merge_hooks(existing, template)

    assert merged["permissions"] == existing["permissions"]
    assert merged["model"] == "opus"


def test_existing_hooks_are_kept(template):
    existing = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-hook"}]}
    ]}}
    merged, added = _merge_hooks(existing, template)

    assert "my-own-hook" in _commands(merged, "PreToolUse")
    assert "metis hook pre" in _commands(merged, "PreToolUse")
    assert added == 2


def test_installing_twice_adds_nothing(template):
    once, _ = _merge_hooks({}, template)
    twice, added = _merge_hooks(once, template)

    assert added == 0
    assert _commands(twice, "PreToolUse") == ["metis hook pre"]


def test_refuses_when_metis_is_not_on_path(tmp_path, monkeypatch, capsys):
    """Hooks pointing at a missing binary would silently never fire."""
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: None)

    code = cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=False, force=False))
    assert code == 1
    assert "not on PATH" in capsys.readouterr().err
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_force_overrides_the_path_check(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: None)

    code = cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=False, force=True))
    assert code == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_malformed_settings_are_not_clobbered(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{ not json", encoding="utf-8")

    code = cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=False, force=False))
    assert code == 1
    assert settings.read_text() == "{ not json"
    assert "refusing to overwrite" in capsys.readouterr().err


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")

    code = cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=True, force=False))
    assert code == 0
    assert not (tmp_path / ".claude").exists()
