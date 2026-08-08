"""Change set tests.

One feature spanning several repositories is one change, and has to behave like
one: changed in the same context, built together, pushed together or not at all.
The failure worth machinery is partial application -- repo A lands, repo B
fails, and production holds two services that disagree about a contract with
nothing recording that they were meant to go together.
"""

from __future__ import annotations

import pytest

from metis.bus import changesets as cs
from metis.bus import events as ev
from metis.bus.store import BusError, Store

RUN = "testrun"


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "bus.db")
    s.initialize()
    s.create_run(RUN, str(tmp_path), "dev", "rename the user contract", max_iterations=4)
    return s


@pytest.fixture
def changeset(store):
    return cs.create(store, RUN, ["service-b", "service-a"], "swe",
                     reason="rename the user id field on both sides")


def _built(store, target, change_set):
    ready = ev.post(store, RUN, "code_ready", agent="swe", target=target,
                    change_set=change_set, payload={"sha": "a"})
    ev.post(store, RUN, "build_passed", agent="devops", target=target,
            caused_by=ready, change_set=change_set, payload={"sha": "a"})


# ------------------------------------------------------------- creation


def test_targets_are_sorted_so_the_set_is_order_independent(changeset):
    assert changeset.targets == ["service-a", "service-b"]


def test_a_single_target_is_not_a_change_set(store):
    """One repository needs no coordination; a set would be ceremony."""
    with pytest.raises(BusError, match="at least two targets"):
        cs.create(store, RUN, ["service-a"], "swe")


def test_the_trailer_is_what_links_the_commits(changeset):
    """Nothing else survives to connect N commits in N repositories."""
    assert changeset.trailer == f"Metis-Change-Id: {changeset.id}"


# -------------------------------------------------------------- progress


def test_progress_starts_pending_for_every_target(store, changeset):
    assert cs.progress(store, changeset.id) == {
        "service-a": cs.PENDING, "service-b": cs.PENDING
    }


def test_progress_follows_the_ledger(store, changeset):
    _built(store, "service-a", changeset.id)

    state = cs.progress(store, changeset.id)
    assert state["service-a"] == cs.PASSED
    assert state["service-b"] == cs.PENDING


def test_events_outside_the_set_do_not_count(store, changeset):
    """An unrelated build of the same repo must not mark a set target ready."""
    ready = ev.post(store, RUN, "code_ready", agent="swe", target="service-a",
                    payload={"sha": "unrelated"})
    ev.post(store, RUN, "build_passed", agent="devops", target="service-a",
            caused_by=ready, payload={"sha": "unrelated"})

    assert cs.progress(store, changeset.id)["service-a"] == cs.PENDING


# ------------------------------------------------------------ the gate


def test_a_repo_outside_any_set_may_push(store):
    allowed, _ = cs.may_push(store, RUN, "unrelated-service")
    assert allowed


def test_pushing_half_a_change_is_refused(store, changeset):
    """The failure this whole feature exists to prevent."""
    _built(store, "service-a", changeset.id)

    allowed, reason = cs.may_push(store, RUN, "service-a")
    assert not allowed
    assert "service-b" in reason
    assert "half-applied" in reason


def test_the_set_may_push_once_everything_has_built(store, changeset):
    _built(store, "service-a", changeset.id)
    _built(store, "service-b", changeset.id)

    for target in ("service-a", "service-b"):
        allowed, _ = cs.may_push(store, RUN, target)
        assert allowed, f"{target} should be clear once the whole set has built"


def test_a_failed_sibling_keeps_the_set_blocked(store, changeset):
    _built(store, "service-a", changeset.id)
    ready = ev.post(store, RUN, "code_ready", agent="swe", target="service-b",
                    change_set=changeset.id, payload={"sha": "b"})
    ev.post(store, RUN, "build_failed", agent="devops", target="service-b",
            caused_by=ready, change_set=changeset.id, payload={"summary": "compile error"})

    allowed, reason = cs.may_push(store, RUN, "service-a")
    assert not allowed and "service-b" in reason


def test_closing_the_set_releases_the_gate(store, changeset):
    _built(store, "service-a", changeset.id)
    cs.set_status(store, changeset.id, cs.ABANDONED)

    allowed, _ = cs.may_push(store, RUN, "service-a")
    assert allowed, "an abandoned set should not block forever"


# -------------------------------------------------------- hook wiring


def test_the_hook_blocks_a_premature_push(store, changeset, tmp_path):
    """End to end: the rule reaches the tool call, not just the library."""
    from metis.enforcement import check_changeset_push

    (tmp_path / "service-a").mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path / "service-a", check=True)
    _built(store, "service-a", changeset.id)

    allowed, reason = check_changeset_push(
        store, RUN, "git push origin main", str(tmp_path / "service-a")
    )
    assert not allowed and "service-b" in reason


def test_the_hook_ignores_commands_that_are_not_pushes(store, changeset, tmp_path):
    from metis.enforcement import check_changeset_push

    allowed, _ = check_changeset_push(store, RUN, "git status", str(tmp_path))
    assert allowed


# ------------------------------------------------------------ rollback


def test_rollback_plan_covers_every_repository(store, changeset):
    """Undoing 'the change' must not mean hunting for its pieces."""
    plan = "\n".join(cs.rollback_plan(store, changeset.id))

    assert "service-a" in plan and "service-b" in plan
    assert plan.count("git -C") == 2
    assert "reset --hard" in plan


# ---------------------------------------------------------- migration


def test_an_existing_bus_gains_the_column(tmp_path):
    """CREATE TABLE IF NOT EXISTS will not alter a table that already exists."""
    path = tmp_path / "bus.db"
    store = Store(path)
    store.initialize()

    with store.write() as conn:
        conn.execute("ALTER TABLE events DROP COLUMN change_set")
    with store.read() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    assert "change_set" not in columns

    Store(path).initialize()
    with store.read() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    assert "change_set" in columns


def test_post_exposes_change_set_on_the_cli():
    """A parameter that exists in the library but not the CLI is unusable.

    Unit tests call ev.post(change_set=...) directly and never notice.
    """
    from metis.cli import build_parser

    args = build_parser().parse_args(
        ["post", "--type", "code_ready", "--target", "a", "--change-set", "run/cs-1"]
    )
    assert args.change_set == "run/cs-1"
