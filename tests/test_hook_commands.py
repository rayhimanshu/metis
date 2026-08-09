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


# ----------------------------------------------------------------- removal


def _own_hook(settings_path):
    """A hook the project owner added, beside Metis's."""
    data = json.loads(settings_path.read_text())
    data["hooks"]["PreToolUse"].append({
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "my-own-linter --check"}],
    })
    settings_path.write_text(json.dumps(data, indent=2))


def test_remove_takes_out_metis_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=False))

    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=True))

    text = (tmp_path / ".claude" / "settings.json").read_text()
    assert "metis hook" not in text


def test_removal_leaves_hooks_you_added_yourself(tmp_path, monkeypatch):
    """A settings file is the project's, not ours.

    Uninstalling one tool has no business deleting another's configuration.
    """
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=False))
    settings = tmp_path / ".claude" / "settings.json"
    _own_hook(settings)

    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=True))

    text = settings.read_text()
    assert "my-own-linter" in text
    assert "metis hook" not in text


def test_an_emptied_event_is_dropped_not_left_behind(tmp_path, monkeypatch):
    """A matcher with no hooks is debris, not a rule that matches nothing."""
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=False))

    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=True))

    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "hooks" not in data, "nothing was left, so nothing should remain"


def test_removing_twice_is_harmless(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=False))

    for _ in range(2):
        code = cmd_install_hooks(Namespace(
            project=str(tmp_path), dry_run=False, force=True, remove=True))
        assert code == 0

    assert "nothing of Metis's" in capsys.readouterr().out


def test_remove_with_no_settings_says_so(tmp_path):
    code = cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=True))

    assert code == 0


def test_remove_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=False, force=True, remove=False))
    settings = tmp_path / ".claude" / "settings.json"
    before = settings.read_text()

    cmd_install_hooks(Namespace(
        project=str(tmp_path), dry_run=True, force=True, remove=True))

    assert settings.read_text() == before


# ------------------------------------------------- the protocol is allowed


def test_metis_commands_are_pre_approved(tmp_path, monkeypatch):
    """Stopping to ask permission for `metis post` is asking permission to speak."""
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")

    cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=False,
                                force=True, remove=False))

    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "Bash(metis *)" in data["permissions"]["allow"]


def test_permissions_you_already_had_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(./mvnw *)"]}}), encoding="utf-8")

    cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=False,
                                force=True, remove=False))

    allow = json.loads((tmp_path / ".claude" / "settings.json").read_text())["permissions"]["allow"]
    assert "Bash(./mvnw *)" in allow
    assert "Bash(metis *)" in allow


def test_removal_takes_the_permission_back_out(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(./mvnw *)"]}}), encoding="utf-8")

    cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=False,
                                force=True, remove=False))
    cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=False,
                                force=True, remove=True))

    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["permissions"]["allow"] == ["Bash(./mvnw *)"]


def test_installing_twice_adds_one_permission_not_two(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")

    for _ in range(2):
        cmd_install_hooks(Namespace(project=str(tmp_path), dry_run=False,
                                    force=True, remove=False))

    allow = json.loads((tmp_path / ".claude" / "settings.json").read_text())["permissions"]["allow"]
    assert allow.count("Bash(metis *)") == 1

# ---------------------------------------------------- where hooks land


def _project(tmp_path, workspace_name="my-app"):
    workspace = tmp_path / workspace_name
    workspace.mkdir()
    (tmp_path / "metis.yaml").write_text(
        f"run:\n  workspace: {workspace}\n", encoding="utf-8")
    return workspace


def test_hooks_follow_the_configured_workspace(tmp_path, monkeypatch):
    """Not the current directory.

    Claude Code reads .claude/settings.json from wherever the session started,
    and agents start in the workspace -- so hooks installed anywhere else are
    simply never read.
    """
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    workspace = _project(tmp_path)

    cmd_install_hooks(Namespace(project=None, dry_run=False, force=True,
                                remove=False, config=str(tmp_path / "metis.yaml")))

    assert (workspace / ".claude" / "settings.json").is_file()
    assert not (tmp_path / ".claude").exists(), "not where the config file sits"


def test_an_explicit_path_still_wins(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    cmd_install_hooks(Namespace(project=str(elsewhere), dry_run=False, force=True,
                                remove=False, config=str(tmp_path / "metis.yaml")))

    assert (elsewhere / ".claude" / "settings.json").is_file()


def test_removal_follows_the_workspace_too(tmp_path, monkeypatch):
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    workspace = _project(tmp_path)
    config = str(tmp_path / "metis.yaml")

    cmd_install_hooks(Namespace(project=None, dry_run=False, force=True,
                                remove=False, config=config))
    cmd_install_hooks(Namespace(project=None, dry_run=False, force=True,
                                remove=True, config=config))

    assert "metis hook" not in (workspace / ".claude" / "settings.json").read_text()


def test_installing_twice_does_not_read_like_a_failure(tmp_path, monkeypatch, capsys):
    """'0 hook(s) added' after asking to install looks like nothing worked."""
    monkeypatch.setattr("metis.hook_commands.shutil.which", lambda _: "/usr/bin/metis")
    _project(tmp_path)
    config = str(tmp_path / "metis.yaml")

    for _ in range(2):
        cmd_install_hooks(Namespace(project=None, dry_run=False, force=True,
                                    remove=False, config=config))

    out = capsys.readouterr().out
    assert "already set up" in out
    assert "0 hook(s)" not in out
