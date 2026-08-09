"""Picking work up, and knowing what is already in flight.

Looking must never commit anything: a person should be able to check the board
as often as they like without a card moving or an event landing.
"""

from __future__ import annotations

import argparse

import pytest

from metis import work, work_commands
from metis.bus import events as ev
from metis.bus.store import Store
from metis.intake.base import Issue

RUN = "testrun"


class FakeSource:
    """A tracker that records what was asked of it."""

    def __init__(self, issues, on_start="In Progress"):
        self._issues = issues
        self.on_start = on_start
        self.transitions: list[tuple[str, str]] = []
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        return list(self._issues)

    def transition(self, key, to):
        self.transitions.append((key, to))

    def comment(self, key, body):
        pass


def issue(key, title="Fix the api health endpoint"):
    return Issue(source="trello", key=key, title=title, body="body", url=f"u/{key}")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "bus.db")
    s.initialize()
    s.create_run(RUN, str(tmp_path), "dev", "placeholder", max_iterations=4)
    return s


@pytest.fixture
def cfg(tmp_path):
    from metis.config import load

    (tmp_path / "metis.yaml").write_text(
        "intake:\n  trello:\n    board: abc123\n", encoding="utf-8"
    )
    return load(tmp_path / "metis.yaml")


@pytest.fixture
def source(monkeypatch):
    fake = FakeSource([issue("LG-1"), issue("LG-2", "Speed up the api cache")])
    monkeypatch.setattr(work.sync, "sources", lambda cfg: {"trello": fake})
    return fake


# ------------------------------------------------------------------ looking


def test_available_lists_what_has_not_been_taken(store, cfg, source):
    found = work.available(cfg, store, RUN)
    assert [i.key for i in found] == ["LG-1", "LG-2"]


def test_looking_commits_nothing(store, cfg, source):
    """No event posted, no card moved -- checking the board is free."""
    before = len(ev.read_since(store, RUN, 0, limit=1000))

    work.available(cfg, store, RUN)
    work.available(cfg, store, RUN)

    assert len(ev.read_since(store, RUN, 0, limit=1000)) == before
    assert source.transitions == []


def test_taken_work_stops_being_offered(store, cfg, source):
    work.take(store, RUN, cfg, [issue("LG-1")], ["api"])

    assert [i.key for i in work.available(cfg, store, RUN)] == ["LG-2"]


# ------------------------------------------------------------------- taking


def test_taking_posts_a_requirement_and_moves_the_card(store, cfg, source):
    taken = work.take(store, RUN, cfg, [issue("LG-1")], ["api"])

    assert len(taken) == 1
    rows = ev.read_since(store, RUN, 0, types=["requirement"], limit=10)
    assert len(rows) == 1
    assert source.transitions == [("LG-1", "In Progress")]


def test_taking_only_takes_what_was_chosen(store, cfg, source):
    """The whole point of the picker -- intake used to take everything."""
    work.take(store, RUN, cfg, [issue("LG-2")], ["api"])

    rows = ev.read_since(store, RUN, 0, types=["requirement"], limit=10)
    assert len(rows) == 1
    assert "LG-2" in rows[0]["rationale"]


def test_a_tracker_that_will_not_move_a_card_does_not_stop_the_work(
    store, cfg, source, capsys
):
    def refuse(key, to):
        raise RuntimeError("board is read-only")

    source.transition = refuse
    taken = work.take(store, RUN, cfg, [issue("LG-1")], ["api"])

    assert len(taken) == 1, "the requirement should still be posted"
    assert "could not move" in capsys.readouterr().out


# ----------------------------------------------------------------- in flight


def test_in_flight_reads_state_from_the_ledger(store, cfg, source):
    work.take(store, RUN, cfg, [issue("LG-1")], ["api"])
    ev.post(store, RUN, "code_ready", agent="swe", target="api")

    task = work.in_flight(store, RUN)[0]
    assert task.state == work.WAITING
    assert "build" in task.detail


def test_a_passing_test_finishes_a_task(store, cfg, source):
    work.take(store, RUN, cfg, [issue("LG-1")], ["api"])
    ev.post(store, RUN, "test_passed", agent="tester", target="api")

    task = work.in_flight(store, RUN)[0]
    assert task.state == work.DONE
    assert not task.is_open


def test_halted_means_a_human_is_needed(store, cfg, source):
    work.take(store, RUN, cfg, [issue("LG-1")], ["api"])
    ev.post(store, RUN, "halted", agent="swe", target="api")

    assert work.in_flight(store, RUN)[0].state == work.NEEDS_HUMAN


def test_a_terminal_event_wins_over_later_chatter(store, cfg, source):
    """Noise after a pass must not reopen a finished task."""
    work.take(store, RUN, cfg, [issue("LG-1")], ["api"])
    ev.post(store, RUN, "test_passed", agent="tester", target="api")
    ev.post(store, RUN, "lease_released", agent="tester", target="api")

    assert work.in_flight(store, RUN)[0].state == work.DONE


def test_tasks_on_different_targets_do_not_bleed(store, cfg, source):
    work.take(store, RUN, cfg, [issue("LG-1")], ["api"])
    work.take(store, RUN, cfg, [Issue(source="trello", key="LG-2",
                                      title="Something vague", body="")], ["api"])

    ev.post(store, RUN, "test_failed", agent="tester", target="api")

    states = {t.issue_key: t.state for t in work.in_flight(store, RUN)}
    assert states["LG-1"] == work.WAITING, "the targeted task tracks the failure"
    assert states["LG-2"] == work.UNTRACKED, "an untargeted task is not swept up by it"


# -------------------------------------------------------------------- picker


def test_the_picker_accepts_a_list_of_numbers(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "1,3")

    issues = [issue("A"), issue("B"), issue("C")]
    assert [i.key for i in work_commands._choose(issues, False)] == ["A", "C"]


def test_the_picker_ignores_nonsense_rather_than_crashing(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "1,99,banana")

    issues = [issue("A"), issue("B")]
    chosen = work_commands._choose(issues, False)

    assert [i.key for i in chosen] == ["A"]
    assert "ignoring" in capsys.readouterr().out


def test_enter_takes_nothing(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    assert work_commands._choose([issue("A")], False) == []


def test_all_needs_no_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert len(work_commands._choose([issue("A"), issue("B")], True)) == 2


def test_a_non_interactive_shell_refuses_rather_than_guessing(monkeypatch, capsys):
    """Taking work because nobody could be asked is the wrong default."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert work_commands._choose([issue("A")], False) == []
    assert "--all" in capsys.readouterr().err


# ----------------------------------------------------------- the old papercut


def test_work_starts_a_run_when_there_is_none(tmp_path, cfg, source):
    """intake used to fail here, demanding a requirement before fetching one."""
    store = Store(tmp_path / "fresh.db")
    args = argparse.Namespace(run=None)

    run = work_commands._run_or_start(store, cfg, args)

    assert run["id"]
    assert store.resolve_run(None)["id"] == run["id"]


def test_a_task_with_no_target_says_so_rather_than_lying(store, cfg, source):
    """Events link to a requirement by target, so no target means no tracking.

    Reporting "picked up" forever would let someone believe the task is being
    watched when nothing can ever update it.
    """
    work.take(store, RUN, cfg,
              [Issue(source="trello", key="LG-9", title="Vague request", body="")],
              ["api"])

    task = work.in_flight(store, RUN)[0]
    assert task.target is None
    assert task.state == work.UNTRACKED
    assert "cannot be followed" in task.detail


def test_a_single_target_workspace_attributes_the_seed_requirement(store, cfg, source):
    """`init-run` on a one-target repo must not produce an untrackable task.

    The demo hit this: its own requirement rendered as "untracked -- progress
    cannot be followed", which is a poor first thing for a new user to read and
    is avoidable, since with one target there is nothing to guess between.
    """
    ev.post(store, RUN, "requirement", agent="human", target="calc-demo",
            payload="Add divide()", rationale="run started")
    ev.post(store, RUN, "code_ready", agent="swe", target="calc-demo")

    task = work.in_flight(store, RUN)[0]
    assert task.state == work.WAITING
    assert task.source == "local"
    assert task.title == "Add divide()"
