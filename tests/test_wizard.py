"""Setup wizard tests.

The property that matters most: pressing enter through every question produces a
working configuration, and nothing is demanded that the chosen setup does not
actually need.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metis import wizard
from metis.config import load


def answerer(script: dict[str, str] | None = None, default_all: bool = True):
    """An `ask` that returns scripted answers, falling back to the default."""
    script = script or {}

    def ask(question: str, default: str | None = None) -> str:
        for fragment, answer in script.items():
            if fragment.lower() in question.lower():
                return answer
        return default or ""

    return ask


# ------------------------------------------------------- defaults suffice


def test_pressing_enter_throughout_produces_a_working_config(tmp_path):
    outcome = wizard.run(ask=answerer(), root=tmp_path, interactive=False,
                         store_secrets=False)

    cfg = load(outcome.config_path)
    assert cfg.environment == "dev"
    assert cfg.max_iterations == 4
    assert set(cfg.agents) == {"swe", "devops", "tester"}
    assert cfg.agents["swe"].mode == "attached"
    assert cfg.intake == {}


def test_no_credentials_are_required_without_a_work_source(tmp_path):
    """Someone who only wants `metis discover` must never be asked for a token."""
    outcome = wizard.run(ask=answerer(), root=tmp_path, interactive=False,
                         store_secrets=False)

    assert outcome.values["source"] == "none"
    assert wizard.required_secrets("none") == []
    assert outcome.secrets_stored == []


def test_generated_config_is_loadable_and_commented(tmp_path):
    outcome = wizard.run(ask=answerer(), root=tmp_path, interactive=False,
                         store_secrets=False)
    text = outcome.config_path.read_text()

    assert text.startswith("#"), "generated config should explain itself"
    assert "credentials are never stored here" in text
    load(outcome.config_path)  # must parse


# ------------------------------------------------ capability-driven asks


def test_choosing_jira_makes_its_fields_required():
    assert [k for k, _ in wizard.required_secrets("jira")] == ["jira.api_token"]


def test_choosing_trello_requires_both_halves_of_its_credential():
    assert [k for k, _ in wizard.required_secrets("trello")] == ["trello.key", "trello.token"]


def test_jira_answers_land_in_the_config(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "WORK_SOURCES", {"2": ("jira", "Jira")})
    ask = answerer({
        "choose": "2",
        "jira base url": "https://acme.atlassian.net",
        "account email": "dev@acme.test",
        "jql": "project = ENG AND labels = metis",
        "when picked up": "In Progress",
        "tests pass": "In Review",
        "poll interval": "60",
    })

    outcome = wizard.run(ask=ask, root=tmp_path, interactive=True, store_secrets=False)
    cfg = load(outcome.config_path)

    assert cfg.intake["jira"]["url"] == "https://acme.atlassian.net"
    assert cfg.intake["jira"]["email"] == "dev@acme.test"
    assert cfg.intake["jira"]["jql"] == "project = ENG AND labels = metis"
    assert cfg.intake["jira"]["poll_seconds"] == 60
    assert cfg.intake["jira"]["on_done"] == "In Review"


def test_blank_transitions_are_omitted_rather_than_written_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "WORK_SOURCES", {"2": ("jira", "Jira")})
    ask = answerer({
        "choose": "2",
        "jira base url": "https://acme.atlassian.net",
        "account email": "dev@acme.test",
        "when picked up": "",
        "tests pass": "",
    })

    outcome = wizard.run(ask=ask, root=tmp_path, interactive=True, store_secrets=False)
    assert "on_start" not in outcome.config_path.read_text()


# ------------------------------------------------------------ re-running


def test_existing_answers_come_back_as_defaults(tmp_path):
    first = wizard.run(ask=answerer({"environment name": "staging",
                                     "iteration cap": "6"}),
                       root=tmp_path, interactive=False, store_secrets=False)
    assert load(first.config_path).environment == "staging"

    # Second pass accepts every default, which should be what was chosen before.
    second = wizard.run(ask=answerer(), root=tmp_path, interactive=False,
                        store_secrets=False)
    cfg = load(second.config_path)

    assert cfg.environment == "staging"
    assert cfg.max_iterations == 6


def test_existing_config_is_backed_up_before_being_rewritten(tmp_path):
    (tmp_path / "metis.yaml").write_text("# hand written\nrun:\n  environment: prod\n",
                                         encoding="utf-8")

    outcome = wizard.run(ask=answerer(), root=tmp_path, interactive=False,
                         store_secrets=False)

    assert outcome.backup and outcome.backup.is_file()
    assert "# hand written" in outcome.backup.read_text()


def test_agent_modes_can_be_overridden(tmp_path):
    ask = answerer({"recommended agent modes": "n",
                    "swe mode": "spawned",
                    "devops mode": "attached",
                    "tester mode": "attached"})

    outcome = wizard.run(ask=ask, root=tmp_path, interactive=True, store_secrets=False)
    cfg = load(outcome.config_path)

    assert cfg.agents["swe"].mode == "spawned"
    assert cfg.agents["devops"].mode == "attached"


# ------------------------------------------------------------ git hosting


def test_git_is_reported_as_optional_not_demanded(tmp_path, monkeypatch):
    """Agents can build and test without it; only pushing needs credentials."""
    monkeypatch.setattr(wizard.shutil, "which", lambda _: None)
    monkeypatch.setattr(wizard.secrets, "present", lambda _: False)

    have, note = wizard.git_hosting_note()
    assert not have
    assert "cannot push" in note and "later" in note


def test_existing_gh_auth_means_nothing_to_store(monkeypatch):
    """Asking for a second token to sit in a second place proliferates secrets."""
    import subprocess

    monkeypatch.setattr(wizard.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "Logged in", ""),
    )

    have, note = wizard.git_hosting_note()
    assert have and "no token needed" in note


# ------------------------------------------------------------- summary


def test_summary_points_at_the_next_step(tmp_path):
    outcome = wizard.run(ask=answerer(), root=tmp_path, interactive=False,
                         store_secrets=False)
    text = wizard.summarize(outcome)

    assert "metis discover" in text
    assert "metis install-hooks" in text
    assert "metis intake" not in text  # no work source, so not relevant
