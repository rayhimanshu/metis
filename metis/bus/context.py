"""Everything an agent needs to act, assembled from the ledger.

This is the command the system lives or dies on, because a spawned agent has
nothing else -- no memory of iteration 1, no idea what has already been tried.

The acceptance test is deliberately human: **a person handed only this output
could do the work correctly.** If they could not, a cold agent cannot either.

The item that is easy to omit and expensive to miss is `prior attempts`. Without
it a fresh agent will confidently re-apply a fix that already failed twice, and
the loop burns its whole budget rediscovering the same dead end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import Config
from . import events as ev
from .store import Store

# Event -> the phase a target is in once it lands.
PHASE_BY_EVENT = {
    "requirement": "SWE",
    "review_findings": "SWE",
    "build_failed": "SWE",
    "test_failed": "SWE",
    "deploy_failed": "SWE",
    "code_ready": "DEVOPS",
    "build_passed": "DEVOPS",
    "deploy_requested": "DEVOPS",
    "approved": "DEVOPS",
    "deployed": "TEST",
    "test_passed": "DONE",
    "halted": "HALTED",
}

FAULT_TYPES = ("build_failed", "test_failed", "deploy_failed")

OUTCOME_TYPES = ("build_passed", "build_failed", "test_passed", "test_failed",
                 "deployed", "deploy_failed")


@dataclass
class Attempt:
    iteration: int
    event_id: int
    agent: str | None
    rationale: str | None
    files: list[str] = field(default_factory=list)
    sha: str | None = None
    outcome: str = "unknown"
    outcome_summary: str | None = None


def _payload(row) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {"text": value}


def prior_attempts(store: Store, run_id: str, target: str | None) -> list[Attempt]:
    """Every change already tried for this target, and how it turned out.

    Consult this before proposing a fix. An idea already in here that failed
    will fail again.
    """
    rows = ev.read_since(store, run_id, 0, limit=10_000)
    attempts: list[Attempt] = []
    open_attempt: Attempt | None = None

    for row in rows:
        if target and row["target"] and row["target"] != target:
            continue

        if row["type"] == "code_ready":
            payload = _payload(row)
            open_attempt = Attempt(
                iteration=int(row["iteration"]), event_id=int(row["id"]),
                agent=row["agent"], rationale=row["rationale"],
                files=[f for f in payload.get("files", []) if isinstance(f, str)],
                sha=payload.get("sha"),
            )
            attempts.append(open_attempt)
        elif row["type"] in OUTCOME_TYPES and open_attempt is not None:
            payload = _payload(row)
            open_attempt.outcome = row["type"]
            open_attempt.outcome_summary = payload.get("summary")
            if row["type"] in ("test_passed", "deployed"):
                open_attempt = None

    return attempts


def latest_fault(store: Store, run_id: str, target: str | None) -> dict[str, Any] | None:
    rows = ev.read_since(store, run_id, 0, types=list(FAULT_TYPES), limit=10_000)
    for row in reversed(rows):
        if target and row["target"] and row["target"] != target:
            continue
        payload = _payload(row)
        return {
            "event_id": int(row["id"]),
            "type": row["type"],
            "target": row["target"],
            "summary": payload.get("summary"),
            "detail": payload.get("detail"),
            "test_file": payload.get("test_file"),
            "owning_target": payload.get("owning_target"),
        }
    return None


def phase_for(store: Store, run_id: str, target: str | None) -> str:
    rows = ev.read_since(store, run_id, 0, limit=10_000)
    phase = "SWE"
    for row in rows:
        if target and row["target"] and row["target"] != target:
            continue
        phase = PHASE_BY_EVENT.get(row["type"], phase)
    return phase


def _discovery_for(cfg: Config, target: str | None) -> dict[str, Any] | None:
    """Read the cached discovery report rather than re-scanning per call."""
    latest = cfg.root / ".metis" / "discovery" / "latest" / "discovered.yaml"
    if not latest.is_file():
        return None
    try:
        report = yaml.safe_load(latest.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None
    if not target:
        return report
    return next((t for t in report.get("targets", []) if t.get("name") == target), None)


def build(
    store: Store, cfg: Config, agent: str, event_id: int | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    run = store.resolve_run(None)
    run_id = run["id"]

    trigger = ev.get(store, event_id) if event_id else None
    if trigger is None and event_id:
        raise ValueError(f"no such event: {event_id}")

    target = target or (trigger["target"] if trigger else None)
    iteration = ev.current_iteration(store, run_id)

    role_path = cfg.role_path(agent) if agent in cfg.agents else None
    role_text = role_path.read_text(encoding="utf-8") if role_path else None

    discovered = _discovery_for(cfg, target)
    lock_keys = (discovered or {}).get("lock_keys") if isinstance(discovered, dict) else None
    commands = (discovered or {}).get("commands") if isinstance(discovered, dict) else None

    return {
        "run": {
            "id": run_id,
            "status": run["status"],
            "requirement": run["requirement"],
            "environment": run["environment"],
            "iteration": iteration,
            "max_iterations": run["max_iterations"],
            "remaining": max(0, run["max_iterations"] - iteration),
        },
        "agent": agent,
        "role": role_text,
        "role_path": str(role_path) if role_path else None,
        "target": target,
        "phase": phase_for(store, run_id, target),
        "trigger": dict(trigger) if trigger else None,
        "prior_attempts": [a.__dict__ for a in prior_attempts(store, run_id, target)],
        "latest_fault": latest_fault(store, run_id, target),
        "lock_keys": lock_keys,
        "commands": commands,
        "inbox": [dict(m) for m in ev.inbox(store, run_id, agent, mark_read=False)],
    }


def render(ctx: dict[str, Any], include_role: bool = True) -> str:
    """Format for a prompt. Ordered so the most decision-changing part is first."""
    run = ctx["run"]
    out: list[str] = []

    out.append(f"# Metis context — {ctx['agent']}")
    out.append("")
    out.append(f"run {run['id']} · {run['status']} · environment {run['environment']}")
    out.append(f"iteration {run['iteration']} of {run['max_iterations']} "
               f"({run['remaining']} remaining)")
    if run["remaining"] <= 1:
        out.append("**Last iteration. Prefer the safe minimal fix over the elegant one.**")
    out.append(f"target: {ctx['target'] or '(none)'} · phase: {ctx['phase']}")
    out.append("")
    out.append(f"## Requirement\n\n{run['requirement']}")

    trigger = ctx["trigger"]
    if trigger:
        out.append(f"\n## What woke you\n")
        out.append(f"event #{trigger['id']} `{trigger['type']}` "
                   f"from {trigger['agent'] or 'unknown'} at {trigger['ts']}")
        if trigger.get("rationale"):
            out.append(f"\nstated reason: {trigger['rationale']}")
        if trigger.get("payload"):
            out.append(f"\n```json\n{trigger['payload']}\n```")

    attempts = ctx["prior_attempts"]
    out.append("\n## Prior attempts")
    if not attempts:
        out.append("\nNone. This is the first change for this target.")
    else:
        out.append("\nAn idea already here that failed will fail again. Do something else.\n")
        for a in attempts:
            files = ", ".join(a["files"][:6]) or "(files not recorded)"
            out.append(f"- iteration {a['iteration']} (#{a['event_id']}, {a['agent']}) "
                       f"→ **{a['outcome']}**")
            out.append(f"  - reason: {a['rationale'] or '(none given)'}")
            out.append(f"  - files: {files}")
            if a["outcome_summary"]:
                out.append(f"  - outcome: {a['outcome_summary']}")

    fault = ctx["latest_fault"]
    if fault:
        out.append(f"\n## Latest fault (event #{fault['event_id']}, {fault['type']})")
        if fault.get("owning_target"):
            out.append(f"\nowning target: **{fault['owning_target']}**")
        if fault.get("test_file"):
            out.append(f"\ntest that reported it: `{fault['test_file']}` — "
                       "you may not edit this file while repairing the failure it found")
        if fault.get("summary"):
            out.append(f"\n{fault['summary']}")
        if fault.get("detail"):
            out.append(f"\n```\n{fault['detail']}\n```")

    if ctx["lock_keys"]:
        out.append("\n## Lock keys")
        out.append("")
        for action, keys in ctx["lock_keys"].items():
            if keys:
                out.append(f"- `{action}`: {' '.join(keys)}")
        out.append("\nClaim before acting; release before handing off.")

    if ctx["commands"]:
        out.append("\n## Derived commands\n")
        for name, command in ctx["commands"].items():
            out.append(f"- `{name}`: `{command}`")

    if ctx["inbox"]:
        out.append("\n## Messages\n")
        for message in ctx["inbox"]:
            out.append(f"- from {message['from_agent']}: {message['subject']}")
            if message.get("body"):
                out.append(f"  {message['body']}")

    if include_role and ctx["role"]:
        out.append(f"\n---\n\n{ctx['role']}")
    elif include_role:
        out.append(f"\n(no role file at {ctx['role_path']})")

    return "\n".join(out)
