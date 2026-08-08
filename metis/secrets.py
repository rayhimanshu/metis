"""Credential storage. The security boundary of the whole system.

One rule drives every decision here:

    The substrate holds credentials. Agents never see raw tokens.

An agent asks Metis to comment on an issue; Metis uses the token. A confused or
compromised agent cannot exfiltrate what it was never handed, and tokens stay out
of session context, out of the ledger, and out of transcripts.

Two consequences that look like inconveniences and are not:

* **Interactive entry only.** Nothing accepts a token as a command-line
  argument, because that writes it to shell history and to any process listing.
* **`redact()` runs before anything is persisted.** Defence in depth: if a token
  ever ends up inside a log line or an event payload, it is scrubbed on the way
  out rather than trusted never to have got there.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
from getpass import getpass
from pathlib import Path

SERVICE = "metis"

# Env override, for CI where no keychain exists. METIS_JIRA_TOKEN etc.
ENV_PREFIX = "METIS_"

# Fallback store, used only when no OS keychain is available.
FALLBACK_PATH = Path.home() / ".metis" / "credentials"

# Anything shorter is not a credential and redacting it would mangle ordinary
# text -- a two-character "token" would blank out half a log line.
MIN_REDACT_LEN = 8


class SecretError(RuntimeError):
    pass


def env_name(name: str) -> str:
    return ENV_PREFIX + name.upper().replace("-", "_").replace(".", "_")


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------


def _backend() -> str:
    if platform.system() == "Darwin" and shutil.which("security"):
        return "keychain"
    if shutil.which("secret-tool"):
        return "secret-tool"
    return "file"


def _keychain_get(name: str) -> str | None:
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
        capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _keychain_set(name: str, value: str) -> None:
    # -U updates in place if the item already exists.
    proc = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SecretError(f"keychain write failed: {proc.stderr.strip()}")


def _keychain_delete(name: str) -> bool:
    proc = subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", name],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0


def _secret_tool_get(name: str) -> str | None:
    proc = subprocess.run(
        ["secret-tool", "lookup", "service", SERVICE, "account", name],
        capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _secret_tool_set(name: str, value: str) -> None:
    proc = subprocess.run(
        ["secret-tool", "store", "--label", f"{SERVICE}:{name}",
         "service", SERVICE, "account", name],
        input=value, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SecretError(f"secret-tool write failed: {proc.stderr.strip()}")


def _secret_tool_delete(name: str) -> bool:
    proc = subprocess.run(
        ["secret-tool", "clear", "service", SERVICE, "account", name],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0


def _file_load() -> dict[str, str]:
    if not FALLBACK_PATH.is_file():
        return {}
    values: dict[str, str] = {}
    for line in FALLBACK_PATH.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _file_write(values: dict[str, str]) -> None:
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the outset. Writing then chmod-ing leaves a window
    # where the file is world-readable.
    fd = os.open(FALLBACK_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Metis credentials. Prefer an OS keychain; this file is a fallback.\n")
        for key, value in sorted(values.items()):
            fh.write(f"{key}={value}\n")


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------


def get(name: str) -> str | None:
    """Resolve a secret. Environment wins, so CI needs no keychain."""
    from_env = os.environ.get(env_name(name))
    if from_env:
        return from_env

    backend = _backend()
    if backend == "keychain":
        return _keychain_get(name)
    if backend == "secret-tool":
        return _secret_tool_get(name)
    return _file_load().get(name)


def require(name: str) -> str:
    value = get(name)
    if not value:
        raise SecretError(
            f"missing credential '{name}'. Run: metis setup {name.split('.')[0]}"
        )
    return value


def set_interactive(name: str, prompt: str) -> None:
    """Prompt a human and store the result.

    There is deliberately no `set(name, value)` taking a plain argument. A
    credential passed on a command line lands in shell history, in `ps` output,
    and in any shell integration that records commands.
    """
    value = getpass(f"{prompt}: ").strip()
    if not value:
        raise SecretError("empty value, nothing stored")

    backend = _backend()
    if backend == "keychain":
        _keychain_set(name, value)
    elif backend == "secret-tool":
        _secret_tool_set(name, value)
    else:
        values = _file_load()
        values[name] = value
        _file_write(values)


def delete(name: str) -> bool:
    backend = _backend()
    if backend == "keychain":
        return _keychain_delete(name)
    if backend == "secret-tool":
        return _secret_tool_delete(name)

    values = _file_load()
    if name in values:
        del values[name]
        _file_write(values)
        return True
    return False


def present(name: str) -> bool:
    """Whether a secret is set. Never reveals the value."""
    return bool(get(name))


def redact(text: str, names: list[str]) -> str:
    """Scrub known secret values out of text before it is persisted.

    Called on every payload heading for the ledger. It is a backstop, not the
    primary defence -- the primary defence is that agents never receive tokens
    at all.
    """
    if not text:
        return text
    for name in names:
        value = get(name)
        if value and len(value) >= MIN_REDACT_LEN:
            text = text.replace(value, "<redacted>")
    return text


def backend_description() -> str:
    return {
        "keychain": "macOS Keychain",
        "secret-tool": "libsecret (secret-tool)",
        "file": f"file {FALLBACK_PATH} (0600) -- no OS keychain found",
    }[_backend()]
