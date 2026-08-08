"""Context tests.

A spawned agent has nothing but this output, so every omission here becomes a
cold agent guessing.
"""

from __future__ import annotations

import pytest

from metis.bus import context as ctx_mod
from metis.bus import events as ev
from metis.bus.store import Store
from metis.config import load

RUN = "testrun"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "swe.md").write_text("# Role: SWE\nYou write code.\n", encoding="utf-8")
    (tmp_path / "metis.yaml").write_text(
        "run:\n  workspace: .\n  environment: dev\n", encoding="utf-8"
    )
    return load(tmp_path / "metis.yaml")


@pytest.fixture
def store(cfg) -> Store:
    s = Store(cfg.bus_path())
    s.initialize()
    s.create_run(RUN, str(cfg.workspace), "dev", "Add an S3 probe", max_iterations=4)
    return s


def _cycle(store, target="api", summary="compile error"):
    """One SWE attempt that fails a build."""
    ready = ev.post(store, RUN, "code_ready", agent="swe", target=target,
                    rationale="tried the obvious fix",
                    payload={"sha": "aaa", "files": ["src/A.java"]})
    failed = ev.post(store, RUN, "build_failed", agent="devops", target=target,
                     caused_by=ready, payload={"summary": summary, "detail": "[ERROR] ..."})
    return ready, failed


# ------------------------------------------------------- prior attempts


def test_prior_attempts_record_their_outcome(store, cfg):
    _cycle(store)
    attempts = ctx_mod.prior_attempts(store, RUN, "api")

    assert len(attempts) == 1
    assert attempts[0].outcome == "build_failed"
    assert attempts[0].rationale == "tried the obvious fix"
    assert attempts[0].files == ["src/A.java"]


def test_every_attempt_is_kept(store, cfg):
    """Three failures must show as three, or the loop repeats the first."""
    _cycle(store, summary="first")
    _cycle(store, summary="second")
    _cycle(store, summary="third")

    attempts = ctx_mod.prior_attempts(store, RUN, "api")
    assert [a.outcome_summary for a in attempts] == ["first", "second", "third"]


def test_attempts_are_scoped_to_their_target(store, cfg):
    _cycle(store, target="api")
    _cycle(store, target="web")

    assert len(ctx_mod.prior_attempts(store, RUN, "api")) == 1


def test_a_successful_attempt_is_marked(store, cfg):
    ready = ev.post(store, RUN, "code_ready", agent="swe", target="api",
                    payload={"sha": "bbb"})
    ev.post(store, RUN, "build_passed", agent="devops", target="api", caused_by=ready,
            payload={"sha": "bbb"})

    assert ctx_mod.prior_attempts(store, RUN, "api")[0].outcome == "build_passed"


# -------------------------------------------------------------- context


def test_context_carries_what_the_agent_needs(store, cfg):
    _, failed = _cycle(store)
    ctx = ctx_mod.build(store, cfg, "swe", event_id=failed, target="api")

    assert ctx["run"]["requirement"] == "Add an S3 probe"
    assert ctx["trigger"]["type"] == "build_failed"
    assert ctx["prior_attempts"], "a cold agent must see what was already tried"
    assert ctx["latest_fault"]["summary"] == "compile error"
    assert ctx["phase"] == "SWE"


def test_role_text_is_included(store, cfg):
    ctx = ctx_mod.build(store, cfg, "swe")
    assert "You write code." in ctx["role"]


def test_remaining_iterations_are_reported(store, cfg):
    ctx = ctx_mod.build(store, cfg, "swe")
    assert ctx["run"]["remaining"] == 3


def test_unknown_event_is_an_error(store, cfg):
    with pytest.raises(ValueError, match="no such event"):
        ctx_mod.build(store, cfg, "swe", event_id=9999)


# --------------------------------------------------------------- render


def test_rendered_output_states_what_was_tried(store, cfg):
    _, failed = _cycle(store)
    text = ctx_mod.render(ctx_mod.build(store, cfg, "swe", event_id=failed, target="api"))

    assert "Prior attempts" in text
    assert "tried the obvious fix" in text
    assert "will fail again" in text  # the instruction, not just the data


def test_first_change_says_so_rather_than_showing_nothing(store, cfg):
    """An empty section reads as missing data; a sentence reads as a fact."""
    text = ctx_mod.render(ctx_mod.build(store, cfg, "swe", target="api"))
    assert "This is the first change for this target." in text


def test_last_iteration_is_called_out(store, cfg):
    for _ in range(3):
        _cycle(store)
    with store.write() as conn:
        conn.execute("UPDATE events SET iteration = 4 WHERE run_id = ?", (RUN,))

    text = ctx_mod.render(ctx_mod.build(store, cfg, "swe", target="api"))
    assert "Last iteration" in text


def test_protected_test_file_is_named(store, cfg):
    """The agent should learn the rule from context, not only from a refusal."""
    ready = ev.post(store, RUN, "code_ready", agent="swe", target="api", payload={"sha": "a"})
    failed = ev.post(store, RUN, "test_failed", agent="tester", target="api", caused_by=ready,
                     payload={"summary": "assert failed", "test_file": "tests/test_api.py",
                              "owning_target": "api"})

    text = ctx_mod.render(ctx_mod.build(store, cfg, "swe", event_id=failed, target="api"))
    assert "tests/test_api.py" in text
    assert "may not edit this file" in text
