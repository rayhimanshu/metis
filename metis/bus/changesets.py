"""One feature, several repositories, one context.

A contract change touches the producer and its consumers. A renamed field
touches everything that reads it. Those repositories are not several changes
that happen to coincide -- they are one change, and they have to be treated as
one: same intent, same identity, built together, pushed together or not at all,
rolled back together.

Without a container holding them, each repository is an independent change that
can land on its own. Two consequences follow, and only the second is dangerous:

* **Identity.** Months later there is no way to see that the repositories were
  changed for the same reason. Solved by stamping a trailer, so the feature has
  one id everywhere it touched.

* **Partial application.** Repo A pushes, repo B's build fails, and production
  now runs two services disagreeing about a contract -- with nothing recording
  that they were meant to go together. This is the one worth machinery.

The rule that prevents it: **no repository in a set may be pushed until every
repository in that set has built.** Enforced by the pre-tool hook rather than
requested in a prompt, because it is exactly the rule an agent under pressure
to ship half a change would talk itself out of.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import events as ev
from .store import BusError, Store, now

OPEN, BUILT, PUSHED, ABANDONED = "OPEN", "BUILT", "PUSHED", "ABANDONED"

TRAILER_KEY = "Metis-Change-Id"

# Per-target progress, derived from the ledger rather than tracked separately.
PENDING, READY, FAILED, PASSED = "pending", "code_ready", "build_failed", "build_passed"


@dataclass
class ChangeSet:
    id: str
    run_id: str
    targets: list[str]
    reason: str | None
    created_by: str
    created_at: str
    status: str

    @property
    def trailer(self) -> str:
        return f"{TRAILER_KEY}: {self.id}"


def _row_to_changeset(row) -> ChangeSet:
    return ChangeSet(
        id=row["id"], run_id=row["run_id"], targets=json.loads(row["targets"]),
        reason=row["reason"], created_by=row["created_by"],
        created_at=row["created_at"], status=row["status"],
    )


def create(store: Store, run_id: str, targets: list[str], created_by: str,
           reason: str | None = None) -> ChangeSet:
    """Mint a set. Targets are sorted so the id is stable regardless of order."""
    ordered = sorted(set(t for t in targets if t))
    if len(ordered) < 2:
        raise BusError(
            "a change set needs at least two targets -- a single-repo change "
            "needs no coordination"
        )

    with store.write() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM changesets WHERE run_id = ?", (run_id,)
        ).fetchone()["n"]
        cs_id = f"{run_id}/cs-{count + 1}"

        conn.execute(
            "INSERT INTO changesets (id, run_id, targets, reason, created_by,"
            " created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cs_id, run_id, json.dumps(ordered), reason, created_by, now(), OPEN),
        )

    return ChangeSet(id=cs_id, run_id=run_id, targets=ordered, reason=reason,
                     created_by=created_by, created_at=now(), status=OPEN)


def get(store: Store, cs_id: str) -> ChangeSet | None:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM changesets WHERE id = ?", (cs_id,)).fetchone()
    return _row_to_changeset(row) if row else None


def listing(store: Store, run_id: str, status: str | None = None) -> list[ChangeSet]:
    sql = "SELECT * FROM changesets WHERE run_id = ?"
    params: list[Any] = [run_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at"

    with store.read() as conn:
        return [_row_to_changeset(r) for r in conn.execute(sql, params).fetchall()]


def open_for_target(store: Store, run_id: str, target: str) -> ChangeSet | None:
    """The open set this target belongs to, if any. Used by the push gate."""
    for changeset in listing(store, run_id, OPEN):
        if target in changeset.targets:
            return changeset
    return None


def progress(store: Store, cs_id: str) -> dict[str, str]:
    """Per-target state, derived from events rather than tracked separately.

    Deriving means there is no second source of truth to fall out of step with
    the ledger.
    """
    changeset = get(store, cs_id)
    if not changeset:
        raise BusError(f"no such change set: {cs_id}")

    state = {target: PENDING for target in changeset.targets}
    rows = ev.read_since(store, changeset.run_id, 0,
                         types=["code_ready", "build_passed", "build_failed"], limit=10_000)

    for row in rows:
        target = row["target"]
        if target not in state:
            continue
        # Only events explicitly tagged with this set count, and the gate fails
        # closed. Letting untagged events count would mean an unrelated build of
        # the same repository could satisfy the set -- exactly the false
        # positive that lets a half-applied change through. An untagged build
        # instead leaves the set blocked, which is recoverable: tag it and
        # rebuild.
        if row["change_set"] != cs_id:
            continue
        state[target] = row["type"]

    return state


def is_built(store: Store, cs_id: str) -> bool:
    return all(v == PASSED for v in progress(store, cs_id).values())


def blocking_targets(store: Store, cs_id: str) -> list[str]:
    """Which targets are not yet built -- the reason a push is refused."""
    return sorted(t for t, s in progress(store, cs_id).items() if s != PASSED)


def set_status(store: Store, cs_id: str, status: str) -> None:
    if status not in (OPEN, BUILT, PUSHED, ABANDONED):
        raise BusError(f"unknown change set status: {status}")
    with store.write() as conn:
        conn.execute("UPDATE changesets SET status = ? WHERE id = ?", (status, cs_id))


def may_push(store: Store, run_id: str, target: str) -> tuple[bool, str]:
    """May this repository be pushed right now?

    The whole point of a change set. A target outside any open set is free to
    go; a target inside one waits for its siblings.
    """
    changeset = open_for_target(store, run_id, target)
    if not changeset:
        return True, ""

    blocking = blocking_targets(store, changeset.id)
    if not blocking:
        return True, ""

    if target in blocking:
        blocking = [t for t in blocking if t != target] or blocking

    return False, (
        f"'{target}' is part of change set {changeset.id}, and "
        f"{', '.join(blocking)} {'has' if len(blocking) == 1 else 'have'} not built yet. "
        "Pushing now would leave the change half-applied. Build every target in "
        "the set first."
    )


def rollback_plan(store: Store, cs_id: str) -> list[str]:
    """Per-repository reset, emitted and never executed.

    The anchors come from discovery, captured before anything was modified.
    """
    changeset = get(store, cs_id)
    if not changeset:
        raise BusError(f"no such change set: {cs_id}")

    lines = [f"# Rollback plan for change set {cs_id}",
             f"# {len(changeset.targets)} repositories. Review before running any of it."]

    for target in changeset.targets:
        anchor = _anchor_for(store, changeset.run_id, target)
        lines.append("")
        lines.append(f"# {target}")
        lines.append(f"git -C <path-to-{target}> reset --hard {anchor or '<rollback anchor unknown>'}")

    return lines


def _anchor_for(store: Store, run_id: str, target: str) -> str | None:
    """The rollback anchor discovery recorded for a target, if available."""
    from pathlib import Path

    import yaml

    from ..config import find_config

    config = find_config()
    if not config:
        return None

    latest = config.parent / ".metis" / "discovery" / "latest" / "discovered.yaml"
    if not latest.is_file():
        return None
    try:
        report = yaml.safe_load(latest.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None

    for entry in report.get("targets", []):
        if entry.get("name") == target:
            return (entry.get("git") or {}).get("rollback_anchor")
    return None
