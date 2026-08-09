"""Choosing what to work on: the queue between a tracker and the ledger.

`metis intake` was all-or-nothing -- every ready card became a requirement the
moment it ran. That is the right behaviour for a machine and the wrong one for a
person, who usually wants to see the board, start one thing, and watch it land.

So this module separates the two halves that `intake` had fused:

* **Looking** -- fetch from the tracker and report, touching neither the ledger
  nor the card. Nothing is committed by looking.
* **Taking** -- turn chosen issues into `requirement` events, and move their
  cards to the in-progress list.

Task state is *derived from the ledger*, never stored. A second copy of "what is
in flight" is a second thing that can be wrong, and the events already say it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .bus import events as ev
from .bus.store import Store
from .config import Config
from .intake import sync
from .intake.base import Issue, to_requirement_payload
from .triage import classify

# Where a task can end up. Anything else means it is still moving.
DONE = "done"
NEEDS_HUMAN = "needs human"
WAITING = "waiting"
UNTRACKED = "untracked"

_TERMINAL = {"test_passed": DONE, "halted": NEEDS_HUMAN}

# How an event type reads when it is the latest thing that happened to a task.
_PROGRESS = {
    "requirement": "picked up",
    "code_ready": "code ready, waiting on a build",
    "build_passed": "built, waiting on tests",
    "build_failed": "build failed, SWE repairing",
    "deployed": "deployed, waiting on tests",
    "deploy_failed": "deploy failed, DevOps repairing",
    "test_failed": "tests failed, SWE repairing",
    "test_passed": "tests passed",
    "halted": "iteration cap or a rule fired",
    "stalled": "stalled",
}


@dataclass
class Task:
    """A requirement that has been taken up, with state read from the ledger."""

    event_id: int
    source: str
    issue_key: str
    title: str
    target: str | None
    state: str
    detail: str
    url: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return f"{self.source}:{self.issue_key}"

    @property
    def is_open(self) -> bool:
        return self.state not in (DONE, NEEDS_HUMAN)


# ------------------------------------------------------------------ looking


def available(cfg: Config, store: Store, run_id: str) -> list[Issue]:
    """Ready issues on the tracker that the ledger has not taken up.

    Read-only in both directions: no event is posted and no card is moved, so
    a person can look at the board as often as they like.
    """
    seen = sync.already_ingested(store, run_id)
    found: list[Issue] = []

    for name, source in sync.sources(cfg).items():
        for issue in source.fetch():
            if f"{name}:{issue.key}" not in seen:
                found.append(issue)
    return found


def in_flight(store: Store, run_id: str) -> list[Task]:
    """Every requirement taken up, newest first, with its current state."""
    rows = ev.read_since(store, run_id, 0, limit=10_000)
    requirements = [r for r in rows if r["type"] == "requirement"]

    tasks: list[Task] = []
    for index, row in enumerate(requirements):
        payload, plain = _payload(row)
        target = row["target"]

        # Events belong to this task if they share its target and fall between
        # it and the next requirement for that same target. Target is the only
        # link the ledger has -- events carry no issue key -- so two concurrent
        # tasks on one target would blur together. Rare, and better than
        # maintaining a second copy of the truth that can drift from the events.
        following = _next_for_target(requirements[index + 1:], target)
        window = [
            r for r in rows
            if r["target"] == target
            and int(r["id"]) > int(row["id"])
            and (following is None or int(r["id"]) < following)
        ]

        if target:
            latest = window[-1] if window else row
            state, detail = _state_of(window, latest)
        else:
            # Target is the only thing linking events back to a requirement, so
            # an issue whose text named no known target cannot be followed at
            # all. Saying so is better than showing "picked up" forever and
            # letting someone believe it is being watched.
            state, detail = UNTRACKED, "no target hint -- progress cannot be followed"

        # A requirement typed by hand (`init-run --requirement`) carries a plain
        # string rather than a tracker payload. It is still a task; it just has
        # no issue behind it.
        tasks.append(Task(
            event_id=int(row["id"]),
            source=payload.get("source") or "local",
            issue_key=payload.get("issue_key") or "--",
            title=payload.get("title") or plain or (row["rationale"] or "").strip(),
            target=target,
            state=state,
            detail=detail,
            url=payload.get("url"),
            warnings=payload.get("warnings") or [],
        ))

    tasks.reverse()
    return tasks


def _next_for_target(later: list, target: str | None) -> int | None:
    for row in later:
        if row["target"] == target:
            return int(row["id"])
    return None


def _state_of(window: list, latest) -> tuple[str, str]:
    # A terminal event decides the state even if chatter followed it.
    for row in reversed(window):
        if row["type"] in _TERMINAL:
            return _TERMINAL[row["type"]], _PROGRESS.get(row["type"], row["type"])

    kind = latest["type"]
    return WAITING, _PROGRESS.get(kind, kind)


def _payload(row) -> tuple[dict, str]:
    """Returns (structured payload, plain text) -- a requirement is either."""
    raw = row["payload"] or ""
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}, raw.strip()
    if isinstance(body, dict):
        return body, ""
    return {}, str(body).strip()


# ------------------------------------------------------------------- taking


def take(
    store: Store, run_id: str, cfg: Config, issues: list[Issue],
    known_targets: list[str], mark_started: bool = True,
) -> list[Task]:
    """Turn chosen issues into requirements. The only writing half."""
    built = sync.sources(cfg)
    taken: list[Task] = []

    for issue in issues:
        payload = to_requirement_payload(issue, known_targets)

        # Classified here, at the boundary, so the verdict is recorded on the
        # requirement itself and survives in the ledger.
        verdict = classify(issue.title, issue.body, issue.labels,
                           issue.raw.get("issue_type") if issue.raw else None)
        payload["gate"] = verdict.risk
        payload["gate_reasons"] = verdict.reasons
        event_id = ev.post(
            store, run_id, "requirement",
            agent="intake",
            target=payload["target_hint"],
            payload=payload,
            rationale=f"{issue.source} {issue.key}: {issue.title[:80]}",
            secret_names=cfg.secret_names(),
        )

        taken.append(Task(
            event_id=event_id, source=issue.source, issue_key=issue.key,
            title=issue.title, target=payload["target_hint"],
            state=WAITING, detail=_PROGRESS["requirement"], url=issue.url,
            warnings=payload["warnings"],
        ))

        source = built.get(issue.source)
        on_start = getattr(source, "on_start", None) if source else None
        if mark_started and on_start:
            try:
                source.transition(issue.key, on_start)
            except Exception as e:
                # A tracker that will not move a card must not stop the work.
                print(f"  warning: could not move {issue.key}: {e}")

    return taken
