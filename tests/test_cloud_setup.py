"""Cloud credentials: checked, never held.

The design claim these tests defend is that Metis stores nothing. A cloud CLI
already resolves credentials through SSO sessions, assumed roles and instance
metadata, which are short-lived and rotate. Taking a static access key so Metis
could keep a second copy would swap that for a long-lived secret and call it
convenience.

What Metis owes you instead is catching the silent case -- DevOps cannot deploy
and nothing says so until a deploy fails halfway through.
"""

from __future__ import annotations

import subprocess

import pytest

from metis import setup as setup_mod
from metis.config import load


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "metis.yaml").write_text("run:\n  environment: dev\n", encoding="utf-8")
    return load(tmp_path / "metis.yaml")


def fake_run(mapping):
    """Stand in for subprocess.run.

    Keyed on a substring of the whole command, not just the binary: a provider
    is often asked two different questions (gcloud for the account, then for the
    project) and keying on the binary alone answers both the same way.
    """
    def run(argv, **kwargs):
        joined = " ".join(argv)
        for pattern, (code, out) in mapping.items():
            if pattern in joined:
                return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
    return run


def installed(monkeypatch, *binaries):
    monkeypatch.setattr(setup_mod.shutil, "which",
                        lambda b: f"/usr/bin/{b}" if b in binaries else None)


# ------------------------------------------------------- nothing is stored


@pytest.mark.parametrize("provider", setup_mod.CLOUD)
def test_no_cloud_provider_asks_for_a_secret(provider):
    """The property the whole approach rests on: there is nothing to paste."""
    integration = setup_mod.INTEGRATIONS[provider]

    assert integration.fields == []
    assert integration.secret_fields == []


@pytest.mark.parametrize("provider", setup_mod.CLOUD)
def test_setup_never_writes_a_cloud_secret(provider, cfg, monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(setup_mod.secrets, "set_interactive",
                        lambda key, prompt: written.append(key))
    installed(monkeypatch)  # nothing installed; the check simply fails

    setup_mod.run_setup(provider, cfg)

    assert written == [], "a cloud credential reached the keychain"


# ------------------------------------------------------------- reporting


def test_aws_reports_the_identity_that_will_deploy(cfg, monkeypatch):
    """The role matters: a failing deploy is usually the wrong one, not none."""
    installed(monkeypatch, "aws")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run({
        "sts get-caller-identity": (0, "276278913322\tarn:aws:sts::276278913322:assumed-role/deployer/x\tAID"),
        "configure get region": (0, "eu-west-1"),
    }))

    ok, detail = setup_mod.verify_aws(cfg)

    assert ok
    assert "276278913322" in detail


def test_a_missing_cli_says_so_rather_than_blaming_your_login(cfg, monkeypatch):
    """Reported once as 'no active account' when gcloud was simply absent.

    That sends someone to run a login command that cannot possibly work.
    """
    installed(monkeypatch)  # gcloud absent

    ok, detail = setup_mod.verify_gcp(cfg)

    assert not ok
    assert "not installed" in detail
    assert "auth login" not in detail


def test_gcp_needs_a_project_not_just_an_account(cfg, monkeypatch):
    """An authenticated account with no project cannot deploy anything."""
    installed(monkeypatch, "gcloud")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run({
        "auth list": (0, "me@example.com"),
        "get-value project": (0, ""),
    }))

    ok, detail = setup_mod.verify_gcp(cfg)

    assert not ok
    assert "no project" in detail


def test_an_unauthenticated_cli_fails_with_its_own_words(cfg, monkeypatch):
    installed(monkeypatch, "az")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run({
        "az account show": (1, "Please run 'az login' to setup account"),
    }))

    ok, detail = setup_mod.verify_azure(cfg)

    assert not ok
    assert "az login" in detail


# ---------------------------------------------------------------- status


def test_cloud_is_never_reported_as_not_configured(cfg, monkeypatch):
    """There is nothing to configure, so that wording would send people hunting."""
    installed(monkeypatch)
    rows = {name: (ok, detail) for name, ok, detail in setup_mod.status(cfg, verify=False)}

    for provider in setup_mod.CLOUD:
        assert "not configured" not in rows[provider][1]


def test_verify_surfaces_a_broken_cloud_login(cfg, monkeypatch):
    """The silent failure worth catching: DevOps cannot deploy, nothing says so."""
    installed(monkeypatch, "aws")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run({
        "sts get-caller-identity": (1, "ExpiredToken: the security token included in the request is expired"),
    }))

    rows = {name: (ok, detail) for name, ok, detail in setup_mod.status(cfg, verify=True)}

    ok, detail = rows["aws"]
    assert not ok
    assert "Expired" in detail


def test_setup_exits_nonzero_when_a_cloud_cannot_deploy(cfg, monkeypatch):
    installed(monkeypatch)

    assert setup_mod.run_setup("aws", cfg) == 1
