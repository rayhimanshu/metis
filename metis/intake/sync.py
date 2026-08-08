"""Bidirectional sync between a tracker and the bus.

Agents never call a tracker directly. They post events; this module decides what
is worth mirroring back. Two reasons:

* **Credentials stay out of agent reach.** An agent that cannot reach Jira
  cannot leak a Jira token.
* **A ticket does not get spammed.** One component decides what a human wants to
  read, rather than three agents each deciding for themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..bus import events as ev
from ..bus.store import Store
from ..config import Config
from .base import Issue, to_requirement_payload

ADAPTERS: dict[str, Callable[[dict], Any]] = {}


def _load_adapters() -> None:
    if ADAPTERS:
        return
    from . import jira, trello

    ADAPTERS["jira"] = jira.build
    ADAPTERS["trello"] = trello.build


# Events worth putting in front of a human, and how they read on a ticket.
# Deliberately short: a comment per build attempt makes a ticket unreadable and
# trains people to ignore it.
MIRRORED: dict[str, Callable[[dict], str]] = {
    "deployed": lambda p: f"Deployed to {p.get('environment', 'an environment')}.",
    "deploy_failed": lambda p: f"Deploy failed: {p.get('summary', 'see run log')}",
    "test_passed": lambda p: f"Tests passed ({p.get('count', '?')} checks).",
    "test_failed": lambda p: f"Tests failed: {p.get('summary', 'see run log')}",
    "halted": lambda p: f"Run halted: {p.get('reason', 'iteration cap reached')}. Needs a human.",
}


@dataclass
class PullResult:
    fetched: int = 0
    posted: list[str] = None
    skipped: list[str] = None
    warnings: list[tuple[str, list[str]]] = None

    def __post_init__(self):
        self.posted = self.posted or []
        self.skipped = self.skipped or []
        self.warnings = self.warnings or []


def sources(cfg: Config) -> dict[str, Any]:
    _load_adapters()
    built: dict[str, Any] = {}
    for name, settings in (cfg.intake or {}).items():
        if name not in ADAPTERS:
            raise ValueError(f"unknown intake source '{name}' (known: {', '.join(ADAPTERS)})")
        built[name] = ADAPTERS[name](settings)
    return built


def already_ingested(store: Store, run_id: str) -> set[str]:
    """Issue keys already turned into requirements.

    Polling is at-least-once, so the ledger -- not the tracker -- is what
    decides whether work has been taken up.
    """
    seen: set[str] = set()
    for row in ev.read_since(store, run_id, 0, types=["requirement"], limit=10_000):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        key = payload.get("issue_key") if isinstance(payload, dict) else None
        if key:
            seen.add(f"{payload.get('source')}:{key}")
    return seen


def pull(
    store: Store, run_id: str, cfg: Config, known_targets: list[str],
    dry_run: bool = False, mark_started: bool = True,
) -> PullResult:
    result = PullResult()
    seen = already_ingested(store, run_id)

    for name, source in sources(cfg).items():
        for issue in source.fetch():
            result.fetched += 1
            identity = f"{name}:{issue.key}"
            if identity in seen:
                result.skipped.append(identity)
                continue

            payload = to_requirement_payload(issue, known_targets)
            if payload["warnings"]:
                result.warnings.append((identity, payload["warnings"]))

            if dry_run:
                result.posted.append(identity)
                continue

            ev.post(
                store, run_id, "requirement",
                agent="intake",
                target=payload["target_hint"],
                payload=payload,
                rationale=f"{name} {issue.key}: {issue.title[:80]}",
                secret_names=cfg.secret_names(),
            )
            result.posted.append(identity)

            on_start = getattr(source, "on_start", None)
            if mark_started and on_start:
                try:
                    source.transition(issue.key, on_start)
                except Exception as e:
                    # A tracker that will not move a card must not stop the work.
                    print(f"  warning: could not transition {issue.key}: {e}")

    return result


def push(store: Store, run_id: str, cfg: Config, dry_run: bool = False) -> list[str]:
    """Mirror notable events back to the ticket they came from."""
    mirrored: list[str] = []
    built = sources(cfg)
    if not built:
        return mirrored

    cursor = ev.get_cursor(store, run_id, "intake-out")
    origins = _issue_by_target(store, run_id)

    for row in ev.read_since(store, run_id, cursor, types=list(MIRRORED)):
        cursor = int(row["id"])
        origin = origins.get(row["target"]) if row["target"] else None
        if not origin:
            continue

        source_name, issue_key = origin
        source = built.get(source_name)
        if not source:
            continue

        try:
            payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}

        body = MIRRORED[row["type"]](payload if isinstance(payload, dict) else {})
        note = f"[metis] {body}"
        mirrored.append(f"{issue_key}: {note}")

        if not dry_run:
            source.comment(issue_key, note)
            on_done = getattr(source, "on_done", None)
            if row["type"] == "test_passed" and on_done:
                try:
                    source.transition(issue_key, on_done)
                except Exception as e:
                    print(f"  warning: could not transition {issue_key}: {e}")

    if not dry_run:
        ev.set_cursor(store, run_id, "intake-out", cursor)
    return mirrored


def _issue_by_target(store: Store, run_id: str) -> dict[str, tuple[str, str]]:
    """target -> (source, issue key), from the requirements already ingested."""
    origins: dict[str, tuple[str, str]] = {}
    for row in ev.read_since(store, run_id, 0, types=["requirement"], limit=10_000):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("issue_key") and row["target"]:
            origins[row["target"]] = (payload.get("source", ""), payload["issue_key"])
    return origins
