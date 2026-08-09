"""Integration setup and verification.

Two stores, on purpose:

* **Settings** (Jira URL, account email, JQL) live in `metis.yaml`, hand-edited
  and safe to commit.
* **Secrets** (API tokens) live in the OS keychain, entered interactively.

Setup never rewrites `metis.yaml`. Round-tripping YAML through a parser destroys
comments and reorders keys, and silently rewriting a file a human owns is a poor
trade for saving them one paste.

Every setup ends with **one authenticated read-only call**, because a token that
silently lacks scope is worse than a missing one: it fails later, in the middle
of something, instead of now.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

import requests

from . import secrets
from .config import Config

TIMEOUT = 15


@dataclass
class Field:
    name: str
    prompt: str
    where: str  # "keychain" | "config"
    required: bool = True


@dataclass
class Integration:
    name: str
    label: str
    fields: list[Field]
    verify: Callable[[Config], tuple[bool, str]]
    config_hint: str = ""

    @property
    def secret_fields(self) -> list[Field]:
        return [f for f in self.fields if f.where == "keychain"]

    @property
    def config_fields(self) -> list[Field]:
        return [f for f in self.fields if f.where == "config"]

    def secret_key(self, field_name: str) -> str:
        return f"{self.name}.{field_name}"


# --------------------------------------------------------------------------
# verifiers
# --------------------------------------------------------------------------


def _settings(cfg: Config, name: str) -> dict:
    return (cfg.intake or {}).get(name) or {}


def verify_jira(cfg: Config) -> tuple[bool, str]:
    settings = _settings(cfg, "jira")
    url, email = settings.get("url"), settings.get("email")
    if not url or not email:
        return False, "metis.yaml is missing intake.jira.url and/or intake.jira.email"

    token = secrets.get("jira.api_token")
    if not token:
        return False, "no API token stored (run: metis setup jira)"

    try:
        resp = requests.get(
            f"{url.rstrip('/')}/rest/api/3/myself",
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"could not reach {url}: {e}"

    if resp.status_code == 401:
        return False, "401 unauthorized -- token or email is wrong"
    if resp.status_code == 403:
        return False, "403 forbidden -- token is valid but lacks permission"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    who = resp.json()
    return True, f"authenticated as {who.get('displayName')} <{who.get('emailAddress') or email}>"


def verify_trello(cfg: Config) -> tuple[bool, str]:
    key, token = secrets.get("trello.key"), secrets.get("trello.token")
    if not key or not token:
        return False, "key and/or token not stored (run: metis setup trello)"

    try:
        resp = requests.get(
            "https://api.trello.com/1/members/me",
            params={"key": key, "token": token},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"could not reach Trello: {e}"

    if resp.status_code == 401:
        return False, "401 unauthorized -- key or token is wrong"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    who = resp.json()
    return True, f"authenticated as {who.get('username')} ({who.get('fullName')})"


def verify_git(cfg: Config) -> tuple[bool, str]:
    # Prefer the GitHub CLI's own credentials -- if `gh` is already authenticated
    # there is no reason to ask for a second token and store it twice.
    if shutil.which("gh"):
        proc = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, check=False
        )
        if proc.returncode == 0:
            line = next(
                (l.strip() for l in (proc.stdout + proc.stderr).splitlines() if "account" in l),
                "gh is authenticated",
            )
            return True, f"using GitHub CLI -- {line}"

    token = secrets.get("git.token")
    if not token:
        return False, "gh is not authenticated and no token stored (run: metis setup git)"

    try:
        resp = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"could not reach GitHub: {e}"

    if resp.status_code == 401:
        return False, "401 unauthorized -- token is wrong or expired"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    scopes = resp.headers.get("x-oauth-scopes", "")
    return True, f"authenticated as {resp.json().get('login')}" + (f" (scopes: {scopes})" if scopes else "")


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


# --------------------------------------------------------------- cloud
#
# Every one of these verifies and stores nothing.
#
# Cloud CLIs already resolve credentials through chains Metis has no business
# competing with: SSO sessions, assumed roles, instance metadata, application
# default credentials. Those are usually short-lived and rotate. Asking someone
# to paste a static access key so Metis can keep a second copy would be a
# downgrade in security dressed up as convenience -- and the agent runs `aws`
# or `gcloud` anyway, which picks up the ambient credential regardless of what
# Metis stored.
#
# What matters is catching the silent case: DevOps cannot deploy, and nothing
# says so until a deploy fails halfway through. So these check, and report
# which identity is actually in play.


def _cli(binary: str, argv: list[str], timeout: int = 20) -> tuple[bool, str]:
    if not shutil.which(binary):
        return False, f"{binary} is not installed"
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{binary} failed to run: {e}"

    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        first = out.splitlines()[0][:160] if out else "no output"
        return False, first
    return True, out


def verify_aws(cfg: Config) -> tuple[bool, str]:
    ok, out = _cli("aws", ["aws", "sts", "get-caller-identity", "--output", "text"])
    if not ok:
        return False, f"{out} (try: aws sso login, or aws configure)"

    # account, arn, userid -- tab separated. The ARN says which role is in play,
    # which is the thing worth reporting: a deploy failing on permissions is
    # nearly always the wrong role rather than a missing credential.
    parts = out.split()
    account = parts[0] if parts else "?"
    arn = next((p for p in parts if p.startswith("arn:")), "")
    region = subprocess.run(["aws", "configure", "get", "region"],
                            capture_output=True, text=True, check=False).stdout.strip()
    where = f", region {region}" if region else ", no default region"
    return True, f"account {account}{where}" + (f" as {arn.split('/')[-1]}" if arn else "")


def verify_gcp(cfg: Config) -> tuple[bool, str]:
    ok, out = _cli("gcloud", ["gcloud", "auth", "list",
                              "--filter=status:ACTIVE", "--format=value(account)"])
    if not ok:
        # Keep the real reason. "No active account" when the binary is simply
        # absent sends someone to run a login that cannot work.
        return False, out
    if not out:
        return False, "no active gcloud account (try: gcloud auth login)"

    project = subprocess.run(["gcloud", "config", "get-value", "project"],
                             capture_output=True, text=True, check=False).stdout.strip()
    account = out.splitlines()[0]
    if not project or project == "(unset)":
        return False, f"{account} is active but no project is set (gcloud config set project)"
    return True, f"{account}, project {project}"


def verify_azure(cfg: Config) -> tuple[bool, str]:
    ok, out = _cli("az", ["az", "account", "show", "--output", "tsv",
                          "--query", "[name,id,user.name]"])
    if not ok:
        return False, f"{out} (try: az login)"
    parts = out.split("\t")
    name = parts[0] if parts else "?"
    who = parts[2] if len(parts) > 2 else ""
    return True, f"subscription {name}" + (f" as {who}" if who else "")


def verify_alicloud(cfg: Config) -> tuple[bool, str]:
    ok, out = _cli("aliyun", ["aliyun", "sts", "GetCallerIdentity"])
    if not ok:
        return False, f"{out} (try: aliyun configure)"
    import json as _json

    try:
        body = _json.loads(out)
        return True, f"account {body.get('AccountId', '?')} as {body.get('Arn', '?')}"
    except ValueError:
        return True, "authenticated"


INTEGRATIONS: dict[str, Integration] = {
    "jira": Integration(
        name="jira",
        label="Jira",
        fields=[
            Field("url", "Jira base URL", "config"),
            Field("email", "Atlassian account email", "config"),
            Field("api_token", "Jira API token", "keychain"),
        ],
        verify=verify_jira,
        config_hint="""\
intake:
  jira:
    url: https://example.atlassian.net
    email: you@example.com
    jql: 'project = ENG AND status = "Ready for Dev" AND labels = metis'
    poll_seconds: 120
    on_start: In Progress
    on_done: In Review""",
    ),
    "trello": Integration(
        name="trello",
        label="Trello",
        fields=[
            Field("key", "Trello API key", "keychain"),
            Field("token", "Trello API token", "keychain"),
        ],
        verify=verify_trello,
        config_hint="""\
intake:
  trello:
    board_id: <board id>
    list_name: Ready for Dev
    poll_seconds: 120
    on_start: In Progress
    on_done: In Review""",
    ),
    "git": Integration(
        name="git",
        label="Git hosting",
        fields=[Field("token", "GitHub personal access token", "keychain", required=False)],
        verify=verify_git,
    ),
    # No fields at all: these are checked, never stored. See the note above.
    "aws": Integration(name="aws", label="AWS", fields=[], verify=verify_aws),
    "gcp": Integration(name="gcp", label="Google Cloud", fields=[], verify=verify_gcp),
    "azure": Integration(name="azure", label="Azure", fields=[], verify=verify_azure),
    "alicloud": Integration(name="alicloud", label="Alibaba Cloud",
                            fields=[], verify=verify_alicloud),
}

# Providers Metis checks but never holds credentials for.
CLOUD = ("aws", "gcp", "azure", "alicloud")


def run_setup(name: str, cfg: Config) -> int:
    integration = INTEGRATIONS.get(name)
    if not integration:
        print(f"unknown integration '{name}'. Known: {', '.join(sorted(INTEGRATIONS))}")
        return 2

    if name in CLOUD:
        return _check_cloud(integration, cfg)

    print(f"\nSetting up {integration.label}")
    print(f"Secrets are stored in: {secrets.backend_description()}")
    print("Values are never echoed, never logged, and never given to agents.\n")

    missing_config = [
        f for f in integration.config_fields
        if not _settings(cfg, integration.name).get(f.name)
    ]
    if missing_config:
        names = ", ".join(f"intake.{integration.name}.{f.name}" for f in missing_config)
        print(f"metis.yaml is missing: {names}")
        if integration.config_hint:
            print("\nAdd this to metis.yaml (edit the values):\n")
            print(integration.config_hint)
        print("\nThen re-run this command.\n")
        return 1

    for field_spec in integration.secret_fields:
        key = integration.secret_key(field_spec.name)
        if secrets.present(key):
            answer = input(f"{field_spec.prompt} is already stored. Replace it? [y/N] ").strip().lower()
            if answer != "y":
                continue
        try:
            secrets.set_interactive(key, field_spec.prompt)
        except secrets.SecretError as e:
            if field_spec.required:
                print(f"  {e}")
                return 1
            print(f"  skipped: {e}")

    print("\nVerifying...")
    ok, detail = integration.verify(cfg)
    print(f"  {'OK  ' if ok else 'FAIL'} {detail}")
    return 0 if ok else 1


def _check_cloud(integration: Integration, cfg: Config) -> int:
    """Report a cloud identity. Nothing is asked for and nothing is stored.

    Deliberately not a setup step. Pasting a static access key here so Metis
    could keep a second copy would replace a short-lived SSO session with a
    long-lived secret -- worse security, sold as convenience.
    """
    print(f"\nChecking {integration.label}")
    print("Metis stores no cloud credentials. Your CLI already resolves them")
    print("through SSO, assumed roles, or instance metadata, and DevOps runs")
    print("that CLI directly.\n")

    ok, detail = integration.verify(cfg)
    print(f"  {'ok ' if ok else '-- '} {detail}")

    if not ok:
        print("\nDevOps will be able to build and test, but not deploy.")
        return 1
    print("\nDevOps will deploy as this identity. Check it is the one you meant.")
    return 0


# Three states, not two. "Unknown" is a real answer here and collapsing it into
# either of the others is a lie: saying ok claims a working integration nobody
# checked, and saying failed sends someone hunting for a problem that may not
# exist. Only --verify can turn an unknown into one of the other two.
UNKNOWN = None


def status(cfg: Config, verify: bool = False) -> list[tuple[str, bool | None, str]]:
    """Per-integration state. Reports whether a secret is set, never its value.

    The middle value is True (working), False (broken), or None (not checked).
    """
    rows: list[tuple[str, bool, str]] = []

    for name, integration in sorted(INTEGRATIONS.items()):
        stored = [f for f in integration.secret_fields if secrets.present(integration.secret_key(f.name))]
        required = [f for f in integration.secret_fields if f.required]

        if name in CLOUD:
            # Never "not configured": there is nothing here to configure.
            if verify:
                rows.append((name, *integration.verify(cfg)))
            else:
                rows.append((name, UNKNOWN,
                             "nothing to store -- not checked (add --verify)"))
            continue

        if not stored and required:
            rows.append((name, False, "not configured"))
            continue

        if verify:
            ok, detail = integration.verify(cfg)
            rows.append((name, ok, detail))
        elif stored:
            rows.append((name, True, "secrets stored: " + ", ".join(f.name for f in stored)))
        else:
            # Every field optional and nothing stored. Saying "ok" would claim a
            # working integration on the strength of having asked for nothing.
            rows.append((name, UNKNOWN,
                         "no stored secret -- will try existing tooling (add --verify)"))

    return rows
