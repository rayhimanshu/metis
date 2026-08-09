"""Guided first-run setup.

The organising principle is that **almost nothing is unconditionally required**.
`metis discover` works with no configuration at all, so a wizard that demands a
Jira token before it will do anything is hostile to the person who just wants to
scan a repository.

Instead every question is *capability-driven*: choose a work source and its
fields become required; choose none and you are never asked. Three rules follow:

* **Defaults everywhere.** Pressing enter through the whole wizard produces a
  working configuration.
* **Secrets are prompted, never passed as flags**, and are verified against the
  live service immediately -- a token that silently lacks scope is worse than a
  missing one, because it fails later, in the middle of something.
* **Re-runnable.** It reads what is already there, offers it back as the
  default, and backs up the file before rewriting.

Questions are data and the asking is injected, so the whole flow is testable
without a terminal.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import secrets
from .config import ConfigError, CONFIG_NAMES, Config, find_config, load

Asker = Callable[[str, str | None], str]

WORK_SOURCES = {
    "1": ("none", "Nothing yet -- I'll create runs by hand"),
    "2": ("jira", "Jira"),
    "3": ("trello", "Trello"),
}

DEFAULT_JQL = 'assignee = currentUser() AND status = "Ready for Dev"'


@dataclass
class Outcome:
    config_path: Path
    values: dict[str, Any] = field(default_factory=dict)
    secrets_stored: list[str] = field(default_factory=list)
    verifications: list[tuple[str, bool, str]] = field(default_factory=list)
    backup: Path | None = None
    notes: list[str] = field(default_factory=list)


def _prompt(question: str, default: str | None = None) -> str:
    import sys

    suffix = f" [{default}]" if default else ""
    sys.stdout.flush()
    answer = input(f"{question}{suffix}: ").strip()
    return answer or (default or "")


def _yes(ask: Asker, question: str, default: bool = True) -> bool:
    answer = ask(f"{question} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y")


# --------------------------------------------------------------- rendering


def render_config(values: dict[str, Any]) -> str:
    """Build metis.yaml from a template rather than round-tripping YAML.

    A parser cannot preserve comments, and the comments are most of what makes
    this file readable to whoever inherits it.
    """
    lines = [
        "# Metis configuration. Safe to commit -- credentials are never stored here.",
        "# Tokens live in your OS keychain; re-run `metis setup` to change them.",
        "",
        "run:",
        f"  workspace: {values['workspace']}",
        f"  environment: {values['environment']}",
        f"  max_iterations: {values['max_iterations']}",
        "",
        "agents:",
    ]

    wake_on = {
        "swe": "[requirement, build_failed, test_failed, review_findings]",
        "devops": "[code_ready, deploy_requested, approved]",
        "tester": "[deployed]",
    }
    for name in ("swe", "devops", "tester"):
        lines += [
            f"  {name}:",
            f"    mode: {values['modes'][name]}          # attached | spawned",
            f"    role: roles/{name}.md",
            f"    wake_on: {wake_on[name]}",
        ]

    source = values.get("source", "none")
    if source == "none":
        lines += [
            "",
            "# No work source configured. Start runs by hand:",
            "#   metis init-run --requirement \"...\"",
            "# Or re-run `metis setup` to connect Jira or Trello.",
        ]
    elif source == "jira":
        lines += [
            "",
            "intake:",
            "  jira:",
            f"    url: {values['jira_url']}",
            f"    email: {values['jira_email']}",
            f"    jql: '{values['jira_jql']}'",
            f"    poll_seconds: {values['poll_seconds']}",
        ]
        if values.get("jira_on_start"):
            lines.append(f"    on_start: {values['jira_on_start']}")
        if values.get("jira_on_done"):
            lines.append(f"    on_done: {values['jira_on_done']}")
    elif source == "trello":
        lines += [
            "",
            "intake:",
            "  trello:",
            f"    board_id: {values['trello_board']}",
            f"    list_name: {values['trello_list']}",
            f"    poll_seconds: {values['poll_seconds']}",
        ]
        # Trello has no workflow states, so a "transition" is a card moving to
        # another list. Both lists must already exist on the board.
        if values.get("trello_on_start"):
            lines.append(f"    on_start: {values['trello_on_start']}")
        if values.get("trello_on_done"):
            lines.append(f"    on_done: {values['trello_on_done']}")

    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ flow


def collect(ask: Asker, existing: Config | None, interactive: bool = True,
            workspace: str | None = None) -> dict[str, Any]:
    """Gather answers. Every question carries a default that works."""
    values: dict[str, Any] = {}

    # An explicit --workspace wins over a remembered one: someone who names a
    # directory on the command line has just told you which they mean.
    values["workspace"] = workspace or ask(
        "Workspace -- the directory holding the repos agents will work on",
        str(existing.workspace) if existing else ".",
    )
    values["environment"] = ask(
        "Environment name (used as a lock key, so staging and prod stay separate)",
        existing.environment if existing else "dev",
    )
    values["max_iterations"] = ask(
        "Iteration cap before a run halts and asks for a human",
        str(existing.max_iterations) if existing else "4",
    )

    default_modes = {"swe": "attached", "devops": "spawned", "tester": "spawned"}
    if existing:
        default_modes = {n: a.mode for n, a in existing.agents.items()} or default_modes

    if interactive and not _yes(
        ask, f"Use recommended agent modes ({', '.join(f'{k}={v}' for k, v in default_modes.items())})"
    ):
        modes = {}
        for name in ("swe", "devops", "tester"):
            modes[name] = ask(f"  {name} mode (attached | spawned)", default_modes[name])
        values["modes"] = modes
    else:
        values["modes"] = default_modes

    # Work source. Everything below here is required only because of what was
    # chosen here -- decline, and nothing further is asked.
    current = next(iter(existing.intake), "none") if existing and existing.intake else "none"
    if interactive:
        print("\nWhere does work come from?")
        for key, (_, label) in WORK_SOURCES.items():
            print(f"  {key}) {label}")
        default_key = next((k for k, (n, _) in WORK_SOURCES.items() if n == current), "1")
        choice = ask("Choose", default_key)
        source = WORK_SOURCES.get(choice, ("none", ""))[0]
    else:
        source = current
    values["source"] = source

    settings = (existing.intake.get(source) or {}) if existing else {}

    if source == "jira":
        values["jira_url"] = ask("  Jira base URL", settings.get("url"))
        values["jira_email"] = ask("  Atlassian account email", settings.get("email"))
        values["jira_jql"] = ask("  JQL selecting work to pick up",
                                 settings.get("jql") or DEFAULT_JQL)
        values["jira_on_start"] = ask("  Move issues to this status when picked up (blank to skip)",
                                      settings.get("on_start") or "In Progress")
        values["jira_on_done"] = ask("  ...and this one when tests pass (blank to skip)",
                                     settings.get("on_done") or "In Review")
        values["poll_seconds"] = ask("  Poll interval, seconds",
                                     str(settings.get("poll_seconds", 120)))
    elif source == "trello":
        values["trello_board"] = ask("  Trello board id", settings.get("board_id"))
        values["trello_list"] = ask("  List to pull cards from",
                                    settings.get("list_name") or "Ready for Dev")
        values["trello_on_start"] = ask("  Move cards to this list when picked up (blank to skip)",
                                        settings.get("on_start") or "In Progress")
        values["trello_on_done"] = ask("  ...and this one when tests pass (blank to skip)",
                                       settings.get("on_done") or "Done")
        values["poll_seconds"] = ask("  Poll interval, seconds",
                                     str(settings.get("poll_seconds", 120)))
    else:
        values["poll_seconds"] = "120"

    return values


def required_secrets(source: str) -> list[tuple[str, str]]:
    """(key, prompt) pairs the chosen source needs. Nothing else is asked for."""
    if source == "jira":
        return [("jira.api_token", "  Jira API token")]
    if source == "trello":
        return [("trello.key", "  Trello API key"), ("trello.token", "  Trello API token")]
    return []


def git_hosting_note() -> tuple[bool, str]:
    """Whether pushing is already possible without storing anything.

    Most people already have `gh` authenticated, and asking for a second token
    to sit in a second place is how credentials proliferate.
    """
    if shutil.which("gh"):
        import subprocess

        proc = subprocess.run(["gh", "auth", "status"], capture_output=True,
                              text=True, check=False)
        if proc.returncode == 0:
            return True, "GitHub CLI is already authenticated -- no token needed"
    if secrets.present("git.token"):
        return True, "a git token is already stored"
    return False, ("no git credentials found. Agents can still build and test; "
                   "they just cannot push. Add one later with `metis setup git`.")


def write_config(values: dict[str, Any], root: Path) -> tuple[Path, Path | None]:
    path = root / CONFIG_NAMES[0]
    backup = None

    if path.is_file():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    path.write_text(render_config(values), encoding="utf-8")
    return path, backup


def run(ask: Asker | None = None, root: Path | None = None,
        interactive: bool = True, store_secrets: bool = True,
        workspace: str | None = None) -> Outcome:
    ask = ask or _prompt
    root = root or Path.cwd()

    if workspace:
        resolved = Path(workspace).expanduser()
        if not resolved.is_dir():
            raise ConfigError(f"no such directory: {resolved}")
        # The config belongs beside the code it describes, so naming a
        # workspace also decides where metis.yaml lands.
        workspace = str(resolved)
        root = resolved

    found = find_config(root)
    existing = load(found) if found else None

    if interactive:
        print("Metis setup\n")
        print("Press enter to accept a default. Nothing here is required except a")
        print("workspace and an environment name, and both already have one.\n")
        if existing:
            print(f"Reading existing configuration from {found}\n")
        print(end="", flush=True)

    values = collect(ask, existing, interactive=interactive, workspace=workspace)
    config_path, backup = write_config(values, root)
    outcome = Outcome(config_path=config_path, values=values, backup=backup)

    needed = required_secrets(values["source"])
    if needed and interactive and store_secrets:
        # flush matters here: getpass writes its prompt straight to the
        # terminal, while stdout is block-buffered whenever output is piped or
        # captured. Without this the explanation of what is about to be asked
        # for appears *after* the prompt asking for it.
        print(f"\nCredentials for {values['source']} "
              f"(stored in {secrets.backend_description()}; never echoed, never "
              "given to agents):", flush=True)
        for key, prompt in needed:
            if secrets.present(key) and not _yes(ask, f"  {key} is already stored. Replace it",
                                                 default=False):
                continue
            try:
                secrets.set_interactive(key, prompt)
                outcome.secrets_stored.append(key)
            except secrets.SecretError as e:
                outcome.notes.append(f"{key}: {e}")

    # Verify against the live service. A credential that only looks right is the
    # expensive kind of wrong.
    if values["source"] != "none" and store_secrets:
        from .setup import INTEGRATIONS

        integration = INTEGRATIONS.get(values["source"])
        if integration:
            cfg = load(config_path)
            ok, detail = integration.verify(cfg)
            outcome.verifications.append((values["source"], ok, detail))

    have_git, note = git_hosting_note()
    outcome.notes.append(note)
    if not have_git:
        outcome.notes.append("")

    return outcome


def summarize(outcome: Outcome) -> str:
    lines = [f"\nWrote {outcome.config_path}"]
    if outcome.backup:
        lines.append(f"Previous version saved as {outcome.backup.name}")

    if outcome.secrets_stored:
        lines.append(f"Stored: {', '.join(outcome.secrets_stored)}")

    for name, ok, detail in outcome.verifications:
        lines.append(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")

    for note in outcome.notes:
        if note:
            lines.append(f"note: {note}")

    lines += [
        "",
        "Next:",
        "  metis discover          # see what Metis makes of your repos",
        "  metis install-hooks     # wire the safety hooks into this project",
        "  metis doctor            # check everything",
    ]
    if outcome.values.get("source") != "none":
        lines.insert(-1, "  metis intake --dry-run  # see what work it would pick up")
    return "\n".join(lines)
