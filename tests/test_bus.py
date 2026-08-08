"""Bus tests.

The one that matters most is `test_concurrent_claim_grants_exactly_one`. Lease
arbitration is the mechanism that stops two agents deploying the same service,
so it has to be proved against real parallel processes rather than a sequential
approximation of them.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from metis.bus import events as ev
from metis.bus import leases
from metis.bus.store import BusError, Store

RUN = "testrun"


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "bus.db")
    s.initialize()
    s.create_run(RUN, str(tmp_path), "dev", "test requirement", max_iterations=4)
    return s


# ---------------------------------------------------------------- events


def test_post_and_read(store):
    first = ev.post(store, RUN, "requirement", agent="human", payload="build a thing")
    second = ev.post(store, RUN, "code_ready", agent="swe", target="api",
                     payload={"sha": "abc"}, caused_by=first)

    rows = ev.read_since(store, RUN, 0)
    assert [r["id"] for r in rows] == [first, second]
    assert rows[1]["caused_by"] == first
    assert rows[1]["target"] == "api"


def test_caused_by_must_reference_a_real_event(store):
    """Causality is recorded, never inferred -- so a dangling link is a bug."""
    with pytest.raises(BusError, match="non-existent event"):
        ev.post(store, RUN, "code_ready", caused_by=999)


def test_ground_truth_tier_is_automatic(store):
    agent_posted = ev.post(store, RUN, "code_ready", agent="swe")
    hook_written = ev.post(store, RUN, "command_run", agent="devops",
                           payload={"argv": ["mvn", "verify"], "exit": 0})

    assert ev.get(store, agent_posted)["tier"] == "testimony"
    assert ev.get(store, hook_written)["tier"] == "ground_truth"


def test_agents_cannot_approve_themselves(store):
    """Approval an agent can grant itself is not approval.

    This is also what stops issue text from manufacturing one.
    """
    with pytest.raises(BusError, match="only be posted by a human"):
        ev.post(store, RUN, "approved", agent="devops")

    assert ev.post(store, RUN, "approved", agent="human", allow_human_only=True)


def test_payload_secrets_are_redacted(store, monkeypatch):
    from metis import secrets

    monkeypatch.setattr(secrets, "get", lambda name: "supersecrettoken" if name == "jira.api_token" else None)

    event_id = ev.post(store, RUN, "build_failed",
                       payload={"detail": "auth header supersecrettoken failed"},
                       secret_names=["jira.api_token"])

    assert "supersecrettoken" not in ev.get(store, event_id)["payload"]
    assert "<redacted>" in ev.get(store, event_id)["payload"]


def test_filter_by_type(store):
    ev.post(store, RUN, "code_ready", agent="swe")
    wanted = ev.post(store, RUN, "build_failed", agent="devops")
    ev.post(store, RUN, "test_passed", agent="tester")

    rows = ev.read_since(store, RUN, 0, types=["build_failed"])
    assert [r["id"] for r in rows] == [wanted]


# --------------------------------------------------------------- cursors


def test_cursor_lets_a_late_agent_catch_up(store):
    """An agent that was down must not lose what happened while it was away."""
    ev.post(store, RUN, "code_ready", agent="swe")
    ev.post(store, RUN, "code_ready", agent="swe")

    seen = list(ev.tail(store, RUN, "devops", once=True))
    assert len(seen) == 2

    ev.post(store, RUN, "code_ready", agent="swe")
    assert len(list(ev.tail(store, RUN, "devops", once=True))) == 1


def test_cursor_never_moves_backwards(store):
    first = ev.post(store, RUN, "code_ready")
    second = ev.post(store, RUN, "code_ready")

    ev.set_cursor(store, RUN, "devops", second)
    ev.set_cursor(store, RUN, "devops", first)
    assert ev.get_cursor(store, RUN, "devops") == second


def test_await_returns_a_match(store):
    ev.post(store, RUN, "code_ready", agent="swe")
    row = ev.await_event(store, RUN, ["code_ready"], agent="devops", since_id=0, timeout=2)
    assert row and row["type"] == "code_ready"


def test_await_times_out_without_consuming(store):
    ev.post(store, RUN, "code_ready", agent="swe")
    assert ev.await_event(store, RUN, ["test_failed"], agent="devops",
                          timeout=0.5, poll=0.05) is None
    # The cursor must not have advanced past the unmatched event.
    assert ev.get_cursor(store, RUN, "devops") == 0


# ---------------------------------------------------------------- leases


def test_capacity_one_by_default(store):
    granted, _ = leases.claim(store, RUN, "worktree:api@main", "swe", 60)
    assert granted.granted and granted.slot == 0

    refused, _ = leases.claim(store, RUN, "worktree:api@main", "devops", 60)
    assert not refused.granted
    assert refused.holders[0].owner == "swe"


def test_capacity_n_allows_parallel_holders(store):
    """Adding capacity is how the system scales, not adding agents."""
    for i in range(3):
        result, _ = leases.claim(store, RUN, "cluster:prod", f"devops{i}", 60)
        assert result.granted, f"slot {i} should have been free"

    fourth, _ = leases.claim(store, RUN, "cluster:prod", "devops3", 60)
    assert not fourth.granted


def test_release_frees_the_slot(store):
    leases.claim(store, RUN, "env:dev", "tester", 60)
    assert leases.release(store, "env:dev", "tester")

    result, _ = leases.claim(store, RUN, "env:dev", "swe", 60)
    assert result.granted


def test_release_requires_ownership(store):
    leases.claim(store, RUN, "env:dev", "tester", 60)
    assert not leases.release(store, "env:dev", "devops")


def test_expired_lease_is_reclaimable(store):
    """TTL is the crash backstop: a killed worker must not wedge a resource."""
    leases.claim(store, RUN, "env:dev", "tester", ttl_seconds=-1)

    result, expired = leases.claim(store, RUN, "env:dev", "swe", 60)
    assert result.granted
    assert [h.owner for h in expired] == ["tester"]


def test_claim_all_is_atomic(store):
    """Holding half a set while waiting for the rest is how agents deadlock."""
    leases.claim(store, RUN, "cluster:prod", "other", 60)
    for i in range(2):
        leases.claim(store, RUN, "cluster:prod", f"filler{i}", 60)

    granted, results, _ = leases.claim_all(
        store, RUN, ["schema:db.public", "cluster:prod"], "devops", 60
    )
    assert not granted
    # The one that did succeed must have been handed back.
    assert leases.held_by(store, RUN, "devops") == []


def test_claim_all_uses_sorted_order(store):
    _, results, _ = leases.claim_all(
        store, RUN, ["worktree:z@main", "branch:a@main"], "devops", 60
    )
    assert [r.key for r in results] == ["branch:a@main", "worktree:z@main"]


def test_claim_refused_once_the_run_is_over(store):
    """Nobody in a peer network owns termination, so the substrate does."""
    store.set_run_status(RUN, "HALTED")
    result, _ = leases.claim(store, RUN, "env:dev", "tester", 60)
    assert not result.granted
    assert "HALTED" in result.reason


def test_invalid_key_is_rejected(store):
    with pytest.raises(BusError, match="invalid lock key"):
        leases.claim(store, RUN, "no-type-prefix", "swe", 60)


# ------------------------------------------------------------ concurrency


def _race(db_path: str, owner: str, barrier, results) -> None:
    """Claim the same key from a separate process, all starting together."""
    store = Store(Path(db_path))
    barrier.wait()
    result, _ = leases.claim(store, RUN, "worktree:contested@main", owner, 60)
    results.append((owner, result.granted))


def test_concurrent_claim_grants_exactly_one(store):
    """The acceptance test for the whole lease design.

    Eight processes hit one capacity-1 key simultaneously. `BEGIN IMMEDIATE`
    serialises them, so exactly one sees a free slot. Without it, several would
    read "no holders" before any of them wrote, and all would proceed -- which
    in production is two deploys racing on one service.
    """
    ctx = mp.get_context("spawn")
    workers = 8
    barrier = ctx.Barrier(workers)
    results = ctx.Manager().list()

    procs = [
        ctx.Process(target=_race, args=(str(store.path), f"agent{i}", barrier, results))
        for i in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    granted = [owner for owner, ok in results if ok]
    assert len(results) == workers, "some worker never reported"
    assert len(granted) == 1, f"expected exactly one winner, got {granted}"

    holders = leases.held_by(store, RUN)
    assert len(holders) == 1 and holders[0].owner == granted[0]


# --------------------------------------------------------------- messages


def test_messages_are_read_once(store):
    ev.send(store, RUN, "devops", "swe", "build broke", "see event #42")

    assert len(ev.inbox(store, RUN, "swe")) == 1
    assert ev.inbox(store, RUN, "swe") == []
    assert len(ev.inbox(store, RUN, "swe", unread_only=False)) == 1
