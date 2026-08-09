"""Configuration: `metis.yaml` plus the run's identity.

The split from `secrets.py` is deliberate and load-bearing. **Non-secret values
live here, in a file you can read, diff, and commit. Secrets never do.** A Jira
URL and account email belong in version control; the API token does not.

Keeping them in one place is how tokens end up in a repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAMES = ("metis.yaml", "metis.yml", ".metis.yaml")

DEFAULTS: dict[str, Any] = {
    "run": {
        "workspace": ".",
        "environment": "dev",
        "max_iterations": 4,
    },
    "agents": {
        "swe": {"mode": "attached", "role": "roles/swe.md",
                "wake_on": ["requirement", "build_failed", "test_failed", "review_findings"]},
        "devops": {"mode": "spawned", "role": "roles/devops.md",
                   "wake_on": ["code_ready", "deploy_requested", "approved"]},
        # No tester by default.
        #
        # Most work does not need a third party to notice a failure: a build
        # fails, DevOps has the log, SWE fixes it. That loop is two agents and
        # a ledger, and adding a third to watch it is ceremony.
        #
        # A tester earns its place when verification is genuinely separate work
        # -- a suite someone must run against a deployed environment, or an
        # independent judgement about whether a fix actually holds. Configure
        # it explicitly and it joins:
        #
        #     agents:
        #       tester:
        #         mode: attached
        #         role: roles/tester.md
        #         wake_on: [deployed]
        #
        # Without one, DevOps owns verification and posts `test_passed` itself.
        # That event name is about what happened, not who did it, and keeping
        # it means the dashboard, the tracker transition and the completion
        # summary all still work.
    },
    "intake": {},
}

MODES = ("attached", "spawned")


class ConfigError(RuntimeError):
    pass


@dataclass
class AgentConfig:
    name: str
    mode: str
    role: str
    wake_on: list[str] = field(default_factory=list)


@dataclass
class Config:
    path: Path | None
    root: Path
    workspace: Path
    environment: str
    max_iterations: int
    agents: dict[str, AgentConfig]
    intake: dict[str, dict[str, Any]]

    def agent(self, name: str) -> AgentConfig:
        if name not in self.agents:
            known = ", ".join(sorted(self.agents))
            raise ConfigError(f"unknown agent '{name}'. Configured: {known}")
        return self.agents[name]

    def role_path(self, name: str) -> Path | None:
        """Where an agent's prompt lives: the project's copy, else the packaged one.

        Shipping defaults inside the package means a fresh install works with no
        files to create. A project that wants to change a role drops its own
        `roles/<name>.md` in and that wins -- so customising never means editing
        something inside site-packages.
        """
        project = self.root / self.agent(name).role
        if project.is_file():
            return project

        packaged = Path(__file__).parent / "roles" / f"{name}.md"
        return packaged if packaged.is_file() else None

    def bus_path(self) -> Path:
        return self.root / ".metis" / "bus.db"

    def secret_names(self) -> list[str]:
        """Every secret this config could reference, for redaction."""
        names: list[str] = []
        for source, settings in self.intake.items():
            names += [f"{source}.{f}" for f in (settings.get("secret_fields") or [])]
        return names


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def find_config(start: Path | None = None) -> Path | None:
    """Nearest config walking up from `start`, like git does with .git."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        for name in CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load(path: Path | None = None) -> Config:
    path = path or find_config()
    raw: dict[str, Any] = {}

    if path and path.is_file():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path}: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be a mapping")

    merged = _deep_merge(DEFAULTS, raw)
    root = path.parent if path else Path.cwd()

    # `agents` replaces rather than merges. Merging would make a config that
    # names two agents silently run three, and there would be no way to remove
    # a default -- surprising in exactly the situation where you are trying to
    # narrow what runs.
    declared_agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else None

    agents: dict[str, AgentConfig] = {}
    for name, settings in (declared_agents or merged.get("agents") or {}).items():
        mode = settings.get("mode", "attached")
        if mode not in MODES:
            raise ConfigError(
                f"agent '{name}': mode must be one of {', '.join(MODES)}, got '{mode}'"
            )
        wake_on = settings.get("wake_on") or []
        if not wake_on:
            # An agent with no triggers is invisible: every process healthy,
            # nothing ever happening. Catch it at load rather than at 3am.
            raise ConfigError(f"agent '{name}': wake_on is empty, so it can never be woken")
        agents[name] = AgentConfig(
            name=name, mode=mode, role=settings.get("role", f"roles/{name}.md"),
            wake_on=list(wake_on),
        )

    run = merged.get("run") or {}
    workspace = Path(os.path.expanduser(str(run.get("workspace", "."))))
    if not workspace.is_absolute():
        workspace = (root / workspace).resolve()

    return Config(
        path=path,
        root=root,
        workspace=workspace,
        environment=str(run.get("environment", "dev")),
        max_iterations=int(run.get("max_iterations", 4)),
        agents=agents,
        intake=merged.get("intake") or {},
    )


def sample() -> str:
    """A commented starter config. Deliberately contains no secrets."""
    return """\
# Metis configuration. Safe to commit -- credentials are never stored here.
# Run `metis setup jira` (and friends) to store tokens in your OS keychain.

run:
  workspace: .
  environment: dev
  max_iterations: 4

agents:
  swe:
    mode: attached          # attached | spawned
    role: roles/swe.md
    wake_on: [requirement, build_failed, test_failed, review_findings]
  devops:
    mode: spawned
    role: roles/devops.md
    wake_on: [code_ready, deploy_requested, approved]
  tester:
    mode: spawned
    role: roles/tester.md
    wake_on: [deployed]

# intake:
#   jira:
#     url: https://example.atlassian.net
#     email: you@example.com
#     jql: 'project = ENG AND status = "Ready for Dev" AND labels = metis'
#     poll_seconds: 120
#     on_start: In Progress
#     on_done: In Review
"""
