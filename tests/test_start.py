"""Opening a run in one window.

These never launch a real agent. The thing worth testing is what gets handed to
tmux, and a test that spawns three Claude sessions to find out costs real money
every time the suite runs.
"""

from __future__ import annotations

import argparse
import subprocess

import pytest

from metis import start_commands as start
from metis.bus.store import Store
from metis.config import load


@pytest.fixture
def project(tmp_path):
    (tmp_path / "metis.yaml").write_text(
        "run:\n  workspace: .\n  environment: dev\n"
        "agents:\n"
        "  swe:\n    mode: attached\n    role: roles/swe.md\n    wake_on: [requirement]\n"
        "  devops:\n    mode: attached\n    role: roles/devops.md\n    wake_on: [code_ready]\n"
        "  tester:\n    mode: attached\n    role: roles/tester.md\n    wake_on: [deployed]\n",
        encoding="utf-8",
    )
    hooks = tmp_path / ".claude"
    hooks.mkdir()
    (hooks / "settings.json").write_text('{"hooks": {"PreToolUse": "metis hook pre"}}',
                                         encoding="utf-8")

    cfg = load(tmp_path / "metis.yaml")
    store = Store(cfg.bus_path())
    store.initialize()
    store.create_run("r1", str(tmp_path), "dev", "demo", max_iterations=4)
    return cfg, store


def everything_installed(monkeypatch):
    monkeypatch.setattr(start.shutil, "which", lambda b: f"/usr/bin/{b}")


# ------------------------------------------------------------- briefings


@pytest.mark.parametrize("role", ["swe", "devops", "tester"])
def test_each_agent_is_told_which_one_it_is(role):
    text = start.briefing(role)

    assert role.upper() in text
    assert f"metis context --agent {role}" in text
    assert f"metis tail --agent {role}" in text


def test_the_briefing_survives_being_a_shell_argument():
    """It contains backticks. Unquoted, the shell would execute them."""
    import shlex

    quoted = shlex.quote(start.briefing("swe"))
    result = subprocess.run(["bash", "-c", f"printf '%s' {quoted}"],
                            capture_output=True, text=True, check=True)

    assert result.stdout == start.briefing("swe")
    assert "`" in result.stdout, "backticks should reach the agent, not the shell"


# ------------------------------------------------------------- preflight


def test_a_ready_project_has_no_complaints(project, monkeypatch):
    cfg, store = project
    everything_installed(monkeypatch)

    assert start._preflight(cfg, store) == []


def test_missing_metis_on_path_is_called_out(project, monkeypatch):
    """The silent one: hooks shell out to `metis hook pre`.

    Without it they never fire, and a run enforcing nothing looks exactly like
    one that does.
    """
    cfg, store = project
    monkeypatch.setattr(start.shutil, "which",
                        lambda b: None if b == "metis" else f"/usr/bin/{b}")

    problems = start._preflight(cfg, store)

    assert any("hooks would never fire" in p for p in problems)


def test_uninstalled_hooks_are_caught(project, monkeypatch, tmp_path):
    cfg, store = project
    everything_installed(monkeypatch)
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

    assert any("hooks are not installed" in p for p in start._preflight(cfg, store))


def test_a_spawned_agent_is_refused(tmp_path, monkeypatch):
    """A spawned agent writes to a log file, so its pane would sit empty."""
    (tmp_path / "metis.yaml").write_text(
        "agents:\n"
        "  swe:\n    mode: attached\n    role: r.md\n    wake_on: [requirement]\n"
        "  devops:\n    mode: spawned\n    role: r.md\n    wake_on: [code_ready]\n",
        encoding="utf-8",
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"h": "metis hook pre"}',
                                                        encoding="utf-8")
    cfg = load(tmp_path / "metis.yaml")
    store = Store(cfg.bus_path())
    store.initialize()
    store.create_run("r", str(tmp_path), "dev", "x", max_iterations=4)
    everything_installed(monkeypatch)

    problems = start._preflight(cfg, store)

    assert any("devops" in p and "attached" in p for p in problems)


def test_no_run_is_caught(tmp_path, monkeypatch):
    (tmp_path / "metis.yaml").write_text("run:\n  workspace: .\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"h": "metis hook pre"}',
                                                        encoding="utf-8")
    cfg = load(tmp_path / "metis.yaml")
    everything_installed(monkeypatch)

    assert any("no run yet" in p for p in start._preflight(cfg, Store(cfg.bus_path())))


def test_preflight_failure_stops_before_anything_launches(project, monkeypatch, capsys):
    cfg, store = project
    monkeypatch.setattr(start.shutil, "which", lambda b: None)

    launched: list = []
    monkeypatch.setattr(start.subprocess, "run",
                        lambda *a, **k: launched.append(a) or subprocess.CompletedProcess([], 0))

    code = start.cmd_start(argparse.Namespace(
        config=str(cfg.path), session="x", replace=False, no_attach=True, force=False))

    assert code == 1
    assert launched == [], "nothing should be started when the run cannot work"
    assert "Not ready to start" in capsys.readouterr().err


# --------------------------------------------------- what tmux is handed


def test_every_agent_gets_a_pane_with_its_role(project, monkeypatch):
    cfg, store = project
    everything_installed(monkeypatch)

    calls: list[list[str]] = []

    def fake(argv, **kwargs):
        calls.append(argv)
        # has-session must fail, so start does not think one already exists.
        code = 1 if "has-session" in argv else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="")

    monkeypatch.setattr(start.subprocess, "run", fake)

    start.cmd_start(argparse.Namespace(
        config=str(cfg.path), session="t", replace=False, no_attach=True, force=False))

    build = next(c for c in calls if "new-session" in c)
    joined = " ".join(build)

    assert "metis watch" in joined
    for role in ("swe", "devops", "tester"):
        assert f"METIS_ROLE={role} claude" in joined
    assert "main-vertical" in joined, "without the layout the panes land oddly"


def test_an_existing_session_is_not_clobbered(project, monkeypatch, capsys):
    """Someone's running agents should not be killed by a stray command."""
    cfg, store = project
    everything_installed(monkeypatch)

    killed: list[str] = []

    def fake(argv, **kwargs):
        if "kill-session" in argv:
            killed.append(" ".join(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(start.subprocess, "run", fake)

    code = start.cmd_start(argparse.Namespace(
        config=str(cfg.path), session="t", replace=False, no_attach=True, force=False))

    assert code == 1
    assert killed == []
    assert "already exists" in capsys.readouterr().out
