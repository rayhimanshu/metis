"""What gets written back on the ticket.

The value of this comment is that it is a *record* rather than a claim: file
changes and commands come from hooks that fired as the tool ran, not from an
agent's recollection afterwards. The tests below mostly guard that distinction.
"""

from __future__ import annotations

import pytest

from metis import summary
from metis.bus import events as ev
from metis.bus.store import Store

RUN = "testrun"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "bus.db")
    s.initialize()
    s.create_run(RUN, str(tmp_path), "dev", "r", max_iterations=4)
    return s


@pytest.fixture
def requirement(store):
    return ev.post(store, RUN, "requirement", agent="intake", target="api",
                   payload={"issue_key": "LG-42",
                            "title": "Add a health-check endpoint",
                            "body": "Users need a probe.\n\n"
                                    "Acceptance criteria:\n"
                                    "- returns 200 when the cache is warm\n"
                                    "- returns 503 when it is not\n"},
                   rationale="trello LG-42")


def _did_work(store):
    ev.post(store, RUN, "file_changed", agent="swe", target="api",
            payload={"path": "src/health.py", "tool": "Write",
                     "insertions": 38, "deletions": 0})
    ev.post(store, RUN, "file_changed", agent="swe", target="api",
            payload={"path": "tests/test_health.py", "tool": "Write",
                     "insertions": 12, "deletions": 2})
    ev.post(store, RUN, "command_run", agent="devops", target="api",
            payload={"argv": "pytest -q", "exit": 0})


# ------------------------------------------------------------- collecting


def test_the_record_comes_from_ground_truth(store, requirement):
    _did_work(store)

    work = summary.collect(store, RUN, requirement)

    assert work.files == {"src/health.py": (38, 0), "tests/test_health.py": (12, 2)}
    assert work.insertions == 50 and work.deletions == 2
    assert work.commands == [("pytest -q", 0)]


def test_work_before_the_requirement_is_not_claimed(store, requirement):
    """A summary must not take credit for changes that predate the ticket."""
    early = ev.post(store, RUN, "requirement", agent="intake", target="api",
                    payload={"issue_key": "LG-1", "title": "Earlier"})
    ev.post(store, RUN, "file_changed", agent="swe", target="api",
            payload={"path": "old.py", "insertions": 9, "deletions": 0})

    later = ev.post(store, RUN, "requirement", agent="intake", target="api",
                    payload={"issue_key": "LG-2", "title": "Later"})
    ev.post(store, RUN, "file_changed", agent="swe", target="api",
            payload={"path": "new.py", "insertions": 3, "deletions": 0})

    assert "old.py" in summary.collect(store, RUN, early).files
    assert list(summary.collect(store, RUN, later).files) == ["new.py"]
    assert later > early


def test_another_target_is_not_swept_in(store, requirement):
    ev.post(store, RUN, "file_changed", agent="swe", target="billing",
            payload={"path": "billing/x.py", "insertions": 5, "deletions": 0})

    assert summary.collect(store, RUN, requirement).files == {}


def test_repeated_edits_to_one_file_accumulate(store, requirement):
    for _ in range(3):
        ev.post(store, RUN, "file_changed", agent="swe", target="api",
                payload={"path": "src/health.py", "insertions": 4, "deletions": 1})

    work = summary.collect(store, RUN, requirement)
    assert work.files == {"src/health.py": (12, 3)}


# ------------------------------------------------ acceptance criteria


def test_criteria_are_lifted_verbatim(store, requirement):
    """The author's words. Rewording them changes what was agreed."""
    work = summary.collect(store, RUN, requirement)
    criteria = summary.acceptance_criteria(work.body)

    assert criteria == ["returns 200 when the cache is warm",
                        "returns 503 when it is not"]


def test_no_criteria_are_invented(store):
    """Metis cannot know what done means, so it must not say."""
    assert summary.acceptance_criteria("Just fix the bug please") == []
    assert summary.acceptance_criteria("") == []


@pytest.mark.parametrize("heading", [
    "Acceptance Criteria:", "## Done when", "DEFINITION OF DONE",
])
def test_common_headings_are_recognised(heading):
    body = f"context\n\n{heading}\n- one thing\n"
    assert summary.acceptance_criteria(body) == ["one thing"]


# -------------------------------------------------------------- rendering


def test_the_comment_reports_what_happened(store, requirement):
    _did_work(store)
    ev.post(store, RUN, "test_passed", agent="tester", target="api",
            rationale="24 checks")

    text = summary.for_requirement(store, RUN, requirement)

    assert "LG-42 complete" in text
    assert "`src/health.py` (+38 −0)" in text
    assert "+50 −2" in text
    assert "returns 200 when the cache is warm" in text
    assert "test passed" in text


def test_the_comment_says_where_it_came_from(store, requirement):
    """A reader months later needs to know if this is a record or a claim."""
    _did_work(store)
    text = summary.for_requirement(store, RUN, requirement)

    assert "recorded by hooks" in text
    assert "not reported by the agent" in text


def test_failed_commands_are_shown_not_buried(store, requirement):
    ev.post(store, RUN, "command_run", agent="devops", target="api",
            payload={"argv": "pytest -q", "exit": 1})
    ev.post(store, RUN, "command_run", agent="devops", target="api",
            payload={"argv": "ruff check", "exit": 0})

    text = summary.for_requirement(store, RUN, requirement, done=False)

    assert "1 failed" in text
    assert "`pytest -q` exited 1" in text
    assert "needs attention" in text


def test_an_approved_approach_is_recorded(store, requirement):
    ev.post(store, RUN, "design_proposed", agent="swe", target="api",
            payload={"design": "Add /health separate from the LB path."})
    ev.post(store, RUN, "approved", agent="human", target="api",
            allow_human_only=True, rationale="looks right")

    text = summary.for_requirement(store, RUN, requirement)

    assert "Approach (approved by human)" in text
    assert "separate from the LB path" in text


def test_a_quiet_task_still_produces_a_comment(store, requirement):
    """Nothing recorded is itself worth saying, rather than saying nothing."""
    text = summary.for_requirement(store, RUN, requirement)

    assert "LG-42" in text
    assert "Changed" not in text
