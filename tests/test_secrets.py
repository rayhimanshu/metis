"""Secrets tests.

These run against the file backend so they never touch a real keychain, and
they never assert on a real credential.
"""

import inspect
import stat

import pytest

from metis import secrets


@pytest.fixture
def file_store(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "FALLBACK_PATH", tmp_path / "credentials")
    monkeypatch.setattr(secrets, "_backend", lambda: "file")
    return tmp_path / "credentials"


def test_roundtrip(file_store, monkeypatch):
    monkeypatch.setattr(secrets, "getpass", lambda prompt: "tok_abcdefghijkl")
    secrets.set_interactive("jira.api_token", "Jira API token")

    assert secrets.get("jira.api_token") == "tok_abcdefghijkl"
    assert secrets.present("jira.api_token")
    assert secrets.delete("jira.api_token")
    assert secrets.get("jira.api_token") is None


def test_fallback_file_is_owner_only(file_store, monkeypatch):
    monkeypatch.setattr(secrets, "getpass", lambda prompt: "tok_abcdefghijkl")
    secrets.set_interactive("jira.api_token", "Jira API token")

    mode = stat.S_IMODE(file_store.stat().st_mode)
    assert mode == 0o600, f"credentials file is {oct(mode)}, must be 0600"


def test_env_var_wins_over_store(file_store, monkeypatch):
    """CI has no keychain, so the environment has to take precedence."""
    monkeypatch.setattr(secrets, "getpass", lambda prompt: "from_file_value")
    secrets.set_interactive("jira.api_token", "Jira API token")
    monkeypatch.setenv("METIS_JIRA_API_TOKEN", "from_env_value")

    assert secrets.get("jira.api_token") == "from_env_value"


def test_empty_input_stores_nothing(file_store, monkeypatch):
    monkeypatch.setattr(secrets, "getpass", lambda prompt: "   ")
    with pytest.raises(secrets.SecretError):
        secrets.set_interactive("jira.api_token", "Jira API token")
    assert not secrets.present("jira.api_token")


def test_require_names_the_fix(file_store):
    with pytest.raises(secrets.SecretError, match="metis setup jira"):
        secrets.require("jira.api_token")


def test_redact_scrubs_known_values(file_store, monkeypatch):
    monkeypatch.setattr(secrets, "getpass", lambda prompt: "supersecrettoken123")
    secrets.set_interactive("jira.api_token", "Jira API token")

    text = "calling jira with supersecrettoken123 in the header"
    assert secrets.redact(text, ["jira.api_token"]) == "calling jira with <redacted> in the header"


def test_redact_ignores_short_values(file_store, monkeypatch):
    """A short value would blank out ordinary text everywhere it appeared."""
    monkeypatch.setattr(secrets, "getpass", lambda prompt: "abc")
    secrets.set_interactive("jira.api_token", "Jira API token")

    text = "abc appears in ordinary words like abcdef"
    assert secrets.redact(text, ["jira.api_token"]) == text


def test_no_api_accepts_a_secret_as_an_argument():
    """A credential passed on a command line lands in shell history and `ps`.

    The only writer is `set_interactive`, which prompts. If someone later adds
    a plain `set(name, value)`, this fails and makes them justify it.
    """
    setters = [
        name for name, obj in vars(secrets).items()
        if inspect.isfunction(obj) and name in ("set", "store", "put", "save")
    ]
    assert not setters, f"secrets module exposes non-interactive setter(s): {setters}"

    params = list(inspect.signature(secrets.set_interactive).parameters)
    assert params == ["name", "prompt"], "set_interactive must not take a value argument"
