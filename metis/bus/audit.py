"""Reading what happened, and diagnosing what is not happening.

Three agents acting unattended is only acceptable if you can reconstruct
afterwards exactly what occurred and why. Two ideas do most of the work:

* **`caused_by` turns a log into a graph.** Without it you have a
  timestamp-ordered list and have to infer causality by eye, which stops being
  possible the moment three agents overlap.

* **The most common failure is silence, not a crash.** Nothing is happening and
  it is not obvious why. `doctor` exists for that, and its first check is
  orphan events -- a typo in a `wake_on` list produces a system where every
  process is healthy, every log is clean, and nothing happens, forever.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from typing import Any

from ..config import Config
from . import events as ev
from . import leases
from .store import Store, now, parse_ts

# Types an agent might post that somebody ought to be listening for.
ROUTABLE_TYPES = {
    "requirement", "code_ready", "build_passed", "build_failed",
    "deploy_requested", "deployed", "deploy_failed",
    "test_passed", "test_failed", "review_findings", "approved", "halted",
}

# A lease held this long with no activity from its owner is suspicious.
STALE_LEASE_SECONDS = 900
# No events at all for this long, with agents holding nothing, reads as stalled.
IDLE_SECONDS = 1800


@dataclass
class Check:
    ok: bool
    name: str
    detail: str
    hint: str = ""


def _payload(row) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {"text": str(value)}


def _age_seconds(ts: str) -> float:
    return (_dt.datetime.now(_dt.UTC) - parse_ts(ts)).total_seconds()


# ------------------------------------------------------------------- log


def format_row(row, width: int = 400) -> str:
    bits = [f"#{row['id']:<4}", row["ts"][11:19], f"{row['type']:<16}"]
    if row["agent"]:
        bits.append(f"{row['agent']:<9}")
    if row["target"]:
        bits.append(f"[{row['target']}]")
    if row["caused_by"]:
        bits.append(f"<-#{row['caused_by']}")
    if row["tier"] == "ground_truth":
        bits.append("(ground-truth)")

    line = " ".join(bits)
    if row["rationale"]:
        line += f"\n       why: {row['rationale']}"
    if row["payload"]:
        body = row["payload"]
        line += f"\n       {body if len(body) <= width else body[:width] + ' ...'}"
    return line


def log(store: Store, run_id: str, *, types: list[str] | None = None,
        target: str | None = None, agent: str | None = None,
        limit: int = 200) -> list[Any]:
    rows = ev.read_since(store, run_id, 0, types=types, target=target, limit=10_000)
    if agent:
        rows = [r for r in rows if r["agent"] == agent]
    return rows[-limit:]


# -------------------------------------------------------------- causality


def trace(store: Store, run_id: str, event_id: int) -> list[tuple[int, Any]]:
    """Everything that followed from an event, depth-first."""
    rows = ev.read_since(store, run_id, 0, limit=10_000)
    children: dict[int, list[Any]] = {}
    for row in rows:
        if row["caused_by"]:
            children.setdefault(int(row["caused_by"]), []).append(row)

    root = ev.get(store, event_id)
    if not root:
        return []

    out: list[tuple[int, Any]] = []

    def walk(row, depth: int) -> None:
        out.append((depth, row))
        for child in children.get(int(row["id"]), []):
            walk(child, depth + 1)

    walk(root, 0)
    return out


def why(store: Store, event_id: int, limit: int = 30) -> list[Any]:
    """The chain backwards -- how we ended up here."""
    chain: list[Any] = []
    current = ev.get(store, event_id)

    while current is not None and len(chain) < limit:
        chain.append(current)
        parent_id = current["caused_by"]
        current = ev.get(store, int(parent_id)) if parent_id else None

    return list(reversed(chain))


def timeline(store: Store, run_id: str) -> dict[str, list[Any]]:
    """Swimlane per agent, so overlap and contention are visible."""
    lanes: dict[str, list[Any]] = {}
    for row in ev.read_since(store, run_id, 0, limit=10_000):
        lanes.setdefault(row["agent"] or "(unattributed)", []).append(row)
    return lanes


def replay(store: Store, run_id: str, at: str | None = None,
           before_event: int | None = None) -> dict[str, Any]:
    """State as of an instant, rebuilt by replaying up to it.

    Only possible because events are append-only -- the concrete payoff of that
    constraint, worth remembering when someone proposes that updating a row in
    place would be simpler.

    `before_event` is the exact form. Timestamps, even at millisecond
    resolution, cannot always separate two events; event ids always can.
    """
    rows = ev.read_since(store, run_id, 0, limit=10_000)

    if before_event is not None:
        rows = [r for r in rows if int(r["id"]) <= before_event]
        at = rows[-1]["ts"] if rows else None
    elif at is not None:
        cutoff = parse_ts(at)
        rows = [r for r in rows if parse_ts(r["ts"]) <= cutoff]
    else:
        raise ValueError("replay needs either `at` or `before_event`")

    phases: dict[str, str] = {}
    from .context import PHASE_BY_EVENT

    for row in rows:
        if row["target"]:
            phases[row["target"]] = PHASE_BY_EVENT.get(row["type"], phases.get(row["target"], "SWE"))

    return {
        "at": at,
        "events": len(rows),
        "last_event": dict(rows[-1]) if rows else None,
        "iteration": max((int(r["iteration"]) for r in rows), default=1),
        "phases": phases,
    }


def diff_for(store: Store, event_id: int) -> str | None:
    with store.read() as conn:
        row = conn.execute(
            "SELECT body FROM artifacts WHERE event_id = ? AND kind = 'diff' LIMIT 1",
            (event_id,),
        ).fetchone()
    return row["body"].decode("utf-8", errors="replace") if row else None


# ---------------------------------------------------------------- doctor


def run_checks(store: Store, cfg: Config, run_id: str) -> list[Check]:
    checks: list[Check] = []
    run = store.get_run(run_id)
    if not run:
        return [Check(False, "run", f"no such run: {run_id}")]

    rows = ev.read_since(store, run_id, 0, limit=10_000)
    consumed = {t for agent in cfg.agents.values() for t in agent.wake_on}

    # 1. Orphan events. The failure that looks like perfect health and does
    #    nothing at all. Cheap to detect, expensive to discover by waiting.
    posted = {r["type"] for r in rows} & ROUTABLE_TYPES
    orphans = sorted(posted - consumed)
    checks.append(Check(
        not orphans, "orphan events",
        f"{', '.join(orphans)} posted but no agent wakes on them" if orphans
        else "every posted event type has a listener",
        hint="add the type to an agent's wake_on, or stop posting it",
    ))

    # 2. Cursor lag -- an agent whose Monitor died stops listening silently.
    max_id = max((int(r["id"]) for r in rows), default=0)
    lagging: list[str] = []
    for name in cfg.agents:
        behind = max_id - ev.get_cursor(store, run_id, name)
        relevant = [r for r in rows if r["type"] in cfg.agents[name].wake_on]
        if relevant and behind > 0:
            unread = sum(1 for r in relevant if int(r["id"]) > ev.get_cursor(store, run_id, name))
            if unread:
                lagging.append(f"{name} ({unread} unread)")
    checks.append(Check(
        not lagging, "cursor lag",
        f"{', '.join(lagging)}" if lagging else "all agents current",
        hint="the agent is not running, or its tail died",
    ))

    # 3. Leases held with no recent activity from the holder.
    stale: list[str] = []
    for holder in leases.held_by(store, run_id):
        last = max((r["ts"] for r in rows if r["agent"] == holder.owner), default=None)
        idle = _age_seconds(last) if last else _age_seconds(holder.acquired_at)
        if idle > STALE_LEASE_SECONDS:
            stale.append(f"{holder.key} held by {holder.owner}, idle {int(idle)}s")
    checks.append(Check(
        not stale, "stale leases",
        "; ".join(stale) if stale else "no stale leases",
        hint="the holder likely died; the TTL will free it, or release it explicitly",
    ))

    # 4. Everything quiet, nothing held -- a livelock none of the agents can see.
    if rows:
        quiet_for = _age_seconds(rows[-1]["ts"])
        idle = quiet_for > IDLE_SECONDS and not leases.held_by(store, run_id)
        checks.append(Check(
            not idle, "activity",
            f"no events for {int(quiet_for)}s" if idle else f"last event {int(quiet_for)}s ago",
            hint="nobody is acting -- check that agents are running and wake_on is right",
        ))

    # 5. Budget.
    iteration = ev.current_iteration(store, run_id)
    remaining = int(run["max_iterations"]) - iteration
    checks.append(Check(
        remaining > 0 and run["status"] == "RUNNING",
        "budget",
        f"iteration {iteration} of {run['max_iterations']}, run is {run['status']}",
        hint="at the cap, claims are refused and the run halts",
    ))

    # 6. Causality coverage -- a gap here degrades trace and why.
    #
    # Only agent-posted events are expected to name a cause. Ground-truth rows
    # are side effects of a command that already happened, not reactions to
    # another event, so requiring caused_by on them would flag every lease and
    # every file write as a defect and train people to ignore this check.
    missing = [
        int(r["id"]) for r in rows[1:]
        if not r["caused_by"] and r["tier"] != "ground_truth" and r["type"] in ROUTABLE_TYPES
    ]
    checks.append(Check(
        not missing, "causality",
        f"{len(missing)} event(s) without caused_by: {missing[:8]}" if missing
        else "every event after the first records what caused it",
        hint="pass --caused-by when posting, or trace/why cannot follow the chain",
    ))

    return checks


# ---------------------------------------------------------------- report


def report(store: Store, cfg: Config, run_id: str) -> str:
    run = store.get_run(run_id)
    rows = ev.read_since(store, run_id, 0, limit=10_000)
    out: list[str] = []

    out.append(f"# Run {run_id}\n")
    out.append(f"- requirement: {run['requirement']}")
    out.append(f"- environment: {run['environment']}")
    out.append(f"- status: {run['status']}")
    out.append(f"- iterations used: {ev.current_iteration(store, run_id)} "
               f"of {run['max_iterations']}")
    out.append(f"- events: {len(rows)}")

    ground = [r for r in rows if r["tier"] == "ground_truth"]
    out.append(f"- ground-truth events: {len(ground)} "
               f"(hook-written; not the agents' own account)\n")

    changed = [r for r in rows if r["type"] == "file_changed"]
    if changed:
        out.append("## Files changed\n")
        for row in changed:
            payload = _payload(row)
            out.append(f"- `{payload.get('path')}` by {row['agent']} "
                       f"(+{payload.get('insertions') or 0}/-{payload.get('deletions') or 0}) "
                       f"— event #{row['id']}")
        out.append("")

    commands = [r for r in rows if r["type"] == "command_run"]
    if commands:
        out.append("## Commands run\n")
        for row in commands:
            payload = _payload(row)
            out.append(f"- `{payload.get('argv', '')[:120]}` "
                       f"→ exit {payload.get('exit')} ({row['agent']})")
        out.append("")

    failures = [r for r in rows if r["type"].endswith("_failed")]
    if failures:
        out.append("## Failures\n")
        for row in failures:
            payload = _payload(row)
            out.append(f"- #{row['id']} {row['type']} [{row['target'] or '-'}]: "
                       f"{payload.get('summary', '(no summary)')}")
        out.append("")

    from .context import prior_attempts

    targets = sorted({r["target"] for r in rows if r["target"]})
    if targets:
        out.append("## Per target\n")
        for target in targets:
            attempts = prior_attempts(store, run_id, target)
            out.append(f"### {target}\n")
            out.append(f"- attempts: {len(attempts)}")
            for attempt in attempts:
                out.append(f"  - iteration {attempt.iteration}: {attempt.outcome} "
                           f"— {attempt.rationale or '(no reason given)'}")
            out.append("")

    return "\n".join(out)
