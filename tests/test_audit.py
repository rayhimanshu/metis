"""Audit tests.

Two things carry the weight: causality has to be reconstructable, and silence
has to be diagnosable. The second is the common failure -- nothing is happening
and it is not obvious why.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from metis.bus import audit
from metis.bus import events as ev
from metis.bus import leases
from metis.bus.store import Store
from metis.config import load

RUN = "testrun"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "metis.yaml").write_text(
        "run:\n  workspace: .\n"
        "agents:\n"
        "  swe:\n    mode: attached\n    wake_on: [requirement, build_failed]\n"
        "  devops:\n    mode: spawned\n    wake_on: [code_ready]\n",
        encoding="utf-8",
    )
    return load(tmp_path / "metis.yaml")


@pytest.fixture
def store(cfg) -> Store:
    s = Store(cfg.bus_path())
    s.initialize()
    s.create_run(RUN, str(cfg.workspace), "dev", "Add a probe", max_iterations=4)
    return s


def _chain(store):
    a = ev.post(store, RUN, "requirement", agent="human", payload="do the thing")
    b = ev.post(store, RUN, "code_ready", agent="swe", target="api", caused_by=a,
                payload={"sha": "aaa"})
    c = ev.post(store, RUN, "build_failed", agent="devops", target="api", caused_by=b,
                payload={"summary": "compile error"})
    return a, b, c


def _check(checks, name):
    return next(c for c in checks if c.name == name)


# ------------------------------------------------------------ causality


def test_why_walks_backwards_to_the_origin(store):
    a, b, c = _chain(store)
    assert [int(r["id"]) for r in audit.why(store, c)] == [a, b, c]


def test_trace_walks_forwards_with_depth(store):
    a, b, c = _chain(store)
    nodes = audit.trace(store, RUN, a)
    assert [(d, int(r["id"])) for d, r in nodes] == [(0, a), (1, b), (2, c)]


def test_trace_branches(store):
    a = ev.post(store, RUN, "requirement", agent="human")
    ev.post(store, RUN, "code_ready", agent="swe", caused_by=a)
    ev.post(store, RUN, "review_findings", agent="swe", caused_by=a)

    assert len(audit.trace(store, RUN, a)) == 3


def test_why_on_an_unknown_event_is_empty(store):
    assert audit.why(store, 9999) == []


# -------------------------------------------------------------- replay


def test_replay_reconstructs_an_earlier_moment(store):
    """Only possible because events are append-only."""
    _, b, c = _chain(store)

    # By event id -- exact, because several events land inside one millisecond.
    state = audit.replay(store, RUN, before_event=b)
    assert state["events"] == 2
    assert state["phases"]["api"] == "DEVOPS"

    later = audit.replay(store, RUN, before_event=c)
    assert later["events"] == 3
    assert later["phases"]["api"] == "SWE"  # the failure sent it back


def test_replay_by_timestamp(store):
    _chain(store)
    state = audit.replay(store, RUN, at=_dt.datetime.now(_dt.UTC).isoformat())
    assert state["events"] == 3


def test_replay_needs_a_cutoff(store):
    with pytest.raises(ValueError, match="either"):
        audit.replay(store, RUN)


# -------------------------------------------------------------- doctor


def test_orphan_events_are_detected(store, cfg):
    """A typo in wake_on makes a system that looks healthy and does nothing."""
    _chain(store)
    ev.post(store, RUN, "deployed", agent="devops", target="api")

    check = _check(audit.run_checks(store, cfg, RUN), "orphan events")
    assert not check.ok
    assert "deployed" in check.detail


def test_no_orphans_when_everything_is_listened_for(store, cfg):
    _chain(store)
    assert _check(audit.run_checks(store, cfg, RUN), "orphan events").ok


def test_cursor_lag_is_detected(store, cfg):
    """An agent whose tail died stops listening, silently."""
    _chain(store)
    check = _check(audit.run_checks(store, cfg, RUN), "cursor lag")
    assert not check.ok and "swe" in check.detail


def test_a_current_agent_is_not_flagged(store, cfg):
    _chain(store)
    for row in ev.read_since(store, RUN, 0):
        ev.set_cursor(store, RUN, "swe", int(row["id"]))
        ev.set_cursor(store, RUN, "devops", int(row["id"]))

    assert _check(audit.run_checks(store, cfg, RUN), "cursor lag").ok


def test_stale_lease_is_detected(store, cfg):
    leases.claim(store, RUN, "env:dev", "tester", 3600)
    with store.write() as conn:
        conn.execute("UPDATE claims SET acquired_at = ?",
                     ("2020-01-01T00:00:00+00:00",))

    check = _check(audit.run_checks(store, cfg, RUN), "stale leases")
    assert not check.ok and "env:dev" in check.detail


def test_ground_truth_events_are_not_flagged_as_missing_causality(store, cfg):
    """Requiring caused_by on hook-written rows would flag every lease.

    A check that fires constantly is a check people learn to ignore.
    """
    _chain(store)
    ev.post(store, RUN, "lease_acquired", agent="swe", payload={"key": "env:dev"})
    ev.post(store, RUN, "command_run", agent="devops", payload={"argv": "mvn verify"})

    assert _check(audit.run_checks(store, cfg, RUN), "causality").ok


def test_missing_causality_on_agent_events_is_flagged(store, cfg):
    ev.post(store, RUN, "requirement", agent="human")
    ev.post(store, RUN, "code_ready", agent="swe")  # no caused_by

    check = _check(audit.run_checks(store, cfg, RUN), "causality")
    assert not check.ok


def test_budget_check_reflects_a_halted_run(store, cfg):
    _chain(store)
    store.set_run_status(RUN, "HALTED")
    assert not _check(audit.run_checks(store, cfg, RUN), "budget").ok


# -------------------------------------------------------------- report


def test_report_separates_ground_truth_from_testimony(store, cfg):
    _chain(store)
    ev.post(store, RUN, "file_changed", agent="swe",
            payload={"path": "src/A.java", "insertions": 3, "deletions": 1})
    ev.post(store, RUN, "command_run", agent="devops",
            payload={"argv": "./mvnw -B verify", "exit": 1})

    text = audit.report(store, cfg, RUN)
    assert "ground-truth events: 2" in text
    assert "not the agents' own account" in text
    assert "src/A.java" in text
    assert "./mvnw -B verify" in text


def test_report_lists_attempts_per_target(store, cfg):
    _chain(store)
    text = audit.report(store, cfg, RUN)
    assert "### api" in text
    assert "attempts: 1" in text
