"""The event ledger: posting, waiting, and tailing.

`await` and `tail` are how a turn-based Claude session reacts to something. Both
poll, deliberately: polling a local SQLite file costs nothing measurable, works
identically on every platform, and cannot miss an event the way a filesystem
watcher can when the OS coalesces notifications.

The durability property is the one that earns the ledger its place. If an agent
is not running when something is posted, nothing is lost -- events are rows, and
a returning agent reads its stored cursor and catches up. A socket-based bus
loses every message sent while a peer was down.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterator

from .. import secrets
from .store import BusError, Store, now

POLL_SECONDS = 0.3

# Types the substrate itself posts. Agents must not fabricate these -- an agent
# announcing `halted` or `approved` would be granting itself permission.
SUBSTRATE_TYPES = {"stalled", "halted", "lease_expired"}

# Written by hooks rather than by agents, so they cannot be forgotten or
# misreported. See AUDIT.md on the two tiers of evidence.
GROUND_TRUTH_TYPES = {"command_run", "file_changed", "lease_acquired", "lease_released",
                      "lease_expired", "agent_spawned", "agent_exited"}

# Only a human may post these. Approval that an agent can grant itself is not
# approval, and issue text must never be able to manufacture one.
HUMAN_ONLY_TYPES = {"approved", "rejected"}


def current_iteration(store: Store, run_id: str) -> int:
    with store.read() as conn:
        row = conn.execute(
            "SELECT MAX(iteration) AS n FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
    return int(row["n"] or 1)


def post(
    store: Store,
    run_id: str,
    type: str,
    *,
    agent: str | None = None,
    target: str | None = None,
    environment: str | None = None,
    payload: Any = None,
    caused_by: int | None = None,
    session_id: str | None = None,
    rationale: str | None = None,
    tier: str | None = None,
    iteration: int | None = None,
    secret_names: list[str] | None = None,
    allow_human_only: bool = False,
) -> int:
    """Append an event. Returns its id."""
    if type in HUMAN_ONLY_TYPES and not allow_human_only:
        raise BusError(
            f"'{type}' may only be posted by a human (use --i-am-human). "
            "Approval an agent can grant itself is not approval."
        )

    if tier is None:
        tier = "ground_truth" if type in GROUND_TRUTH_TYPES else "testimony"

    body = payload if isinstance(payload, str) or payload is None else json.dumps(payload)

    # Backstop, not the primary defence. Agents are never handed tokens in the
    # first place; this catches a value that reached a payload some other way.
    if body and secret_names:
        body = secrets.redact(body, secret_names)
    if rationale and secret_names:
        rationale = secrets.redact(rationale, secret_names)

    with store.write() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            raise BusError(f"no such run: {run_id}")

        if caused_by is not None:
            exists = conn.execute("SELECT 1 FROM events WHERE id = ?", (caused_by,)).fetchone()
            if not exists:
                raise BusError(f"caused_by references a non-existent event: {caused_by}")

        if iteration is None:
            row = conn.execute(
                "SELECT MAX(iteration) AS n FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
            iteration = int(row["n"] or 1)

        cursor = conn.execute(
            "INSERT INTO events (run_id, ts, type, agent, target, environment, iteration,"
            " payload, tier, caused_by, session_id, rationale)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, now(), type, agent, target, environment or run["environment"],
             iteration, body, tier, caused_by, session_id, rationale),
        )
        return int(cursor.lastrowid)


def get(store: Store, event_id: int) -> sqlite3.Row | None:
    with store.read() as conn:
        return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def read_since(
    store: Store, run_id: str, since_id: int = 0,
    types: list[str] | None = None, target: str | None = None, limit: int = 500,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE run_id = ? AND id > ?"
    params: list[Any] = [run_id, since_id]

    if types:
        sql += f" AND type IN ({','.join('?' * len(types))})"
        params += types
    if target:
        sql += " AND target = ?"
        params.append(target)

    sql += " ORDER BY id LIMIT ?"
    params.append(limit)

    with store.read() as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------- cursors


def get_cursor(store: Store, run_id: str, agent: str) -> int:
    with store.read() as conn:
        row = conn.execute(
            "SELECT last_event_id FROM cursors WHERE agent = ? AND run_id = ?",
            (agent, run_id),
        ).fetchone()
    return int(row["last_event_id"]) if row else 0


def set_cursor(store: Store, run_id: str, agent: str, event_id: int) -> None:
    with store.write() as conn:
        conn.execute(
            "INSERT INTO cursors (agent, run_id, last_event_id, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(agent, run_id) DO UPDATE SET"
            " last_event_id = MAX(last_event_id, excluded.last_event_id),"
            " updated_at = excluded.updated_at",
            (agent, run_id, event_id, now()),
        )


# ------------------------------------------------------------ wait and tail


def await_event(
    store: Store, run_id: str, types: list[str] | None = None, *,
    agent: str | None = None, timeout: float = 600.0,
    since_id: int | None = None, poll: float = POLL_SECONDS,
) -> sqlite3.Row | None:
    """Block until a matching event appears, or the timeout elapses.

    The cursor advances only on a match, so a timeout never silently consumes
    events the caller has not seen.
    """
    start = since_id if since_id is not None else (
        get_cursor(store, run_id, agent) if agent else _max_id(store, run_id)
    )
    deadline = time.monotonic() + timeout

    while True:
        rows = read_since(store, run_id, start, types, limit=1)
        if rows:
            if agent:
                set_cursor(store, run_id, agent, int(rows[0]["id"]))
            return rows[0]

        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def tail(
    store: Store, run_id: str, agent: str, types: list[str] | None = None, *,
    from_start: bool = False, poll: float = POLL_SECONDS,
    once: bool = False,
) -> Iterator[sqlite3.Row]:
    """Yield matching events forever, advancing the agent's cursor.

    Callers must flush per event. Buffered output means a watching session gets
    its notifications in clumps, or never -- the single most likely silent bug
    in the whole wake-up path.
    """
    since = 0 if from_start else get_cursor(store, run_id, agent)

    while True:
        rows = read_since(store, run_id, since, types)
        for row in rows:
            since = int(row["id"])
            set_cursor(store, run_id, agent, since)
            yield row

        if once:
            return
        time.sleep(poll)


def _max_id(store: Store, run_id: str) -> int:
    with store.read() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS n FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
    return int(row["n"] or 0)


# --------------------------------------------------------------- messages


def send(store: Store, run_id: str, from_agent: str, to_agent: str,
         subject: str, body: str) -> int:
    with store.write() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (run_id, ts, from_agent, to_agent, subject, body)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, now(), from_agent, to_agent, subject, body),
        )
        return int(cursor.lastrowid)


def inbox(store: Store, run_id: str, agent: str, unread_only: bool = True,
          mark_read: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM messages WHERE run_id = ? AND to_agent = ?"
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id"

    with store.read() as conn:
        rows = conn.execute(sql, (run_id, agent)).fetchall()

    if rows and mark_read:
        with store.write() as conn:
            conn.executemany(
                "UPDATE messages SET read_at = ? WHERE id = ?",
                [(now(), r["id"]) for r in rows],
            )
    return rows
