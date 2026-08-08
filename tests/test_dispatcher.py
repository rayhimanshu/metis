"""Dispatcher tests.

The behaviours here only surface under load, which is exactly when you cannot
afford to debug them: duplicate spawns, self-triggering loops, and leases left
behind by a process that died.
"""

from __future__ import annotations

import time

import pytest

from metis.bus import events as ev
from metis.bus import leases
from metis.bus.store import Store
from metis.config import load
from metis.dispatcher import Dispatcher

RUN = "testrun"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "roles").mkdir()
    for name in ("swe", "devops"):
        (tmp_path / "roles" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    (tmp_path / "metis.yaml").write_text(
        "run:\n  workspace: .\n"
        "agents:\n"
        "  swe:\n    mode: attached\n    wake_on: [build_failed]\n"
        "  devops:\n    mode: spawned\n    wake_on: [code_ready]\n",
        encoding="utf-8",
    )
    return load(tmp_path / "metis.yaml")


@pytest.fixture
def store(cfg) -> Store:
    s = Store(cfg.bus_path())
    s.initialize()
    s.create_run(RUN, str(cfg.workspace), "dev", "do the thing", max_iterations=4)
    return s


def dispatcher(store, cfg, **kwargs) -> Dispatcher:
    return Dispatcher(store=store, cfg=cfg, run_id=RUN, debounce=0.0, **kwargs)


def test_only_spawned_agents_are_dispatched(store, cfg):
    """Attached agents pull; dispatching them too would double-wake them."""
    assert list(dispatcher(store, cfg).agents()) == ["devops"]


def test_a_matching_event_becomes_pending(store, cfg):
    ev.post(store, RUN, "code_ready", agent="swe", target="api", payload={"sha": "a"})

    d = dispatcher(store, cfg)
    d.collect()
    assert "devops" in d.pending


def test_unrelated_events_are_ignored(store, cfg):
    ev.post(store, RUN, "test_failed", agent="tester", target="api")

    d = dispatcher(store, cfg)
    d.collect()
    assert d.pending == {}


def test_an_agent_is_never_woken_by_its_own_event(store, cfg):
    """Otherwise an agent that posts and wakes on the same type spins forever."""
    ev.post(store, RUN, "code_ready", agent="devops", target="api")

    d = dispatcher(store, cfg)
    d.collect()
    assert d.pending == {}


def test_a_burst_coalesces_into_one_wake(store, cfg):
    """Cold agents are expensive, and the last event supersedes the earlier ones."""
    ids = [ev.post(store, RUN, "code_ready", agent="swe", target="api",
                   payload={"sha": str(i)}) for i in range(5)]

    d = dispatcher(store, cfg)
    d.collect()

    assert len(d.pending) == 1
    assert d.pending["devops"].event_id == ids[-1]
    assert d.pending["devops"].count == 5


def test_debounce_holds_a_wake_briefly(store, cfg):
    ev.post(store, RUN, "code_ready", agent="swe", target="api")

    d = dispatcher(store, cfg)
    d.debounce = 5.0
    d.collect()
    assert d.ready() == []

    d.pending["devops"].first_seen -= 10
    assert [name for name, _ in d.ready()] == ["devops"]


def test_no_second_spawn_while_one_is_running(store, cfg):
    """Two cold agents on one target would fight over the same worktree lease."""
    ev.post(store, RUN, "code_ready", agent="swe", target="api")

    d = dispatcher(store, cfg)
    d.collect()
    d.running["devops"] = object()  # stand in for an in-flight process
    assert d.ready() == []


def test_dry_run_spawns_nothing(store, cfg, capsys):
    trigger = ev.post(store, RUN, "code_ready", agent="swe", target="api",
                      payload={"sha": "a"})

    d = dispatcher(store, cfg, dry_run=True)
    d.tick()

    assert d.running == {}
    assert f"would spawn devops for event #{trigger}" in capsys.readouterr().out


def test_cursor_advances_so_an_event_wakes_once(store, cfg):
    ev.post(store, RUN, "code_ready", agent="swe", target="api")

    d = dispatcher(store, cfg, dry_run=True)
    d.tick()
    assert d.pending == {}

    d.tick()
    assert d.pending == {}, "the same event must not wake the agent twice"


def test_leases_are_released_when_a_spawn_exits(store, cfg):
    """A TTL would eventually free it; waiting out a TTL wedges the resource."""
    import subprocess

    leases.claim(store, RUN, "worktree:api@main", "devops", 3600)
    assert leases.held_by(store, RUN, "devops")

    d = dispatcher(store, cfg)
    finished = subprocess.Popen(["true"])
    finished.wait()

    from metis.dispatcher import Spawn

    d.running["devops"] = Spawn(
        agent="devops", event_id=1, process=finished,
        started_at=time.monotonic(), log_path=cfg.root / "x.log",
    )
    d.reap()

    assert leases.held_by(store, RUN, "devops") == []
    assert d.running == {}


def test_exit_is_recorded_as_ground_truth(store, cfg):
    import subprocess

    from metis.dispatcher import Spawn

    d = dispatcher(store, cfg)
    finished = subprocess.Popen(["true"])
    finished.wait()
    d.running["devops"] = Spawn(agent="devops", event_id=1, process=finished,
                                started_at=time.monotonic(), log_path=cfg.root / "x.log")
    d.reap()

    rows = ev.read_since(store, RUN, 0, types=["agent_exited"])
    assert len(rows) == 1
    assert rows[0]["tier"] == "ground_truth"
