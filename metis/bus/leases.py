"""Lock keys and leases.

Concurrency is governed by named resources with capacities, never by "one agent
per repo". Agent-per-repo does not survive many repositories or a second
environment; resource leases do, unchanged. Adding a staging environment turns
`env:dev` into `env:dev` and `env:staging` -- two concurrent testers, no code
change.

Three properties matter:

* **Arbitration is exclusive.** Claims are decided inside `BEGIN IMMEDIATE`, so
  two processes racing for one key serialise and exactly one sees a free slot.
* **Leases expire.** A worker killed mid-deploy would otherwise wedge a
  resource forever. TTL is the crash backstop; explicit release is the normal
  path.
* **Multiple keys are taken in sorted order.** Two multi-repo changes acquiring
  in different orders will deadlock, and sorting is the cheapest total order
  that every process agrees on without coordinating.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .store import BusError, Store, now, parse_ts

KEY_PATTERN = re.compile(r"^[a-z_]+:[A-Za-z0-9._@/-]+$")

# Capacity by key type. One is the safe default: a resource we have not thought
# about is a resource only one actor should touch at a time.
DEFAULT_CAPACITY: dict[str, int] = {
    "worktree": 1,
    "branch": 1,
    "schema": 1,
    "env": 1,
    "cluster": 3,
}


@dataclass
class Holder:
    key: str
    slot: int
    owner: str
    acquired_at: str
    expires_at: str

    def describe(self) -> str:
        return f"{self.owner} holds {self.key} (slot {self.slot}) until {self.expires_at}"


@dataclass
class ClaimResult:
    granted: bool
    key: str
    slot: int | None = None
    holders: list[Holder] = None  # populated when refused
    reason: str = ""

    def __post_init__(self) -> None:
        if self.holders is None:
            self.holders = []


def key_type(key: str) -> str:
    return key.split(":", 1)[0]


def validate(key: str) -> None:
    if not KEY_PATTERN.match(key):
        raise BusError(
            f"invalid lock key '{key}' -- expected <type>:<name>, e.g. worktree:api@main"
        )


def capacity(key: str, overrides: dict[str, int] | None = None) -> int:
    overrides = overrides or {}
    if key in overrides:
        return overrides[key]
    return overrides.get(key_type(key), DEFAULT_CAPACITY.get(key_type(key), 1))


def _purge_expired(conn, run_id: str) -> list[Holder]:
    """Drop leases past their TTL. Runs inside the caller's transaction.

    Doing this in the same transaction as the claim check is what stops a
    reaper and a claimant disagreeing about whether a slot is free.
    """
    rows = conn.execute(
        "SELECT key, slot, owner, acquired_at, expires_at FROM claims"
        " WHERE run_id = ? AND expires_at <= ?",
        (run_id, now()),
    ).fetchall()

    for row in rows:
        conn.execute("DELETE FROM claims WHERE key = ? AND slot = ?", (row["key"], row["slot"]))

    return [Holder(**dict(r)) for r in rows]


def _holders(conn, key: str) -> list[Holder]:
    rows = conn.execute(
        "SELECT key, slot, owner, acquired_at, expires_at FROM claims WHERE key = ?"
        " ORDER BY slot",
        (key,),
    ).fetchall()
    return [Holder(**dict(r)) for r in rows]


def claim(
    store: Store, run_id: str, key: str, owner: str, ttl_seconds: int,
    overrides: dict[str, int] | None = None,
) -> tuple[ClaimResult, list[Holder]]:
    """Try to take a slot. Never blocks.

    Returns (result, expired) -- `expired` is for the caller to turn into
    `lease_expired` events, which is done outside this transaction so event
    writing never nests inside lease arbitration.
    """
    validate(key)
    limit = capacity(key, overrides)
    expires = _expiry(ttl_seconds)

    with store.write() as conn:
        run = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            raise BusError(f"no such run: {run_id}")

        # The stop condition lives here rather than in any agent. In a peer
        # network nobody owns termination, so the substrate refuses to hand out
        # the means to act once the run is over.
        if run["status"] != "RUNNING":
            return ClaimResult(False, key, reason=f"run is {run['status']}"), []

        expired = _purge_expired(conn, run_id)
        held = _holders(conn, key)

        if len(held) >= limit:
            return ClaimResult(False, key, holders=held,
                               reason=f"all {limit} slot(s) held"), expired

        taken = {h.slot for h in held}
        slot = next(i for i in range(limit) if i not in taken)

        conn.execute(
            "INSERT INTO claims (key, slot, run_id, owner, pid, acquired_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, slot, run_id, owner, os.getpid(), now(), expires),
        )

    return ClaimResult(True, key, slot=slot), expired


def claim_all(
    store: Store, run_id: str, keys: list[str], owner: str, ttl_seconds: int,
    overrides: dict[str, int] | None = None,
) -> tuple[bool, list[ClaimResult], list[Holder]]:
    """All-or-nothing acquisition, in sorted order.

    Partial acquisition is released on failure. Holding half a set while waiting
    for the rest is precisely how two well-behaved agents deadlock.
    """
    ordered = sorted(set(keys))
    results: list[ClaimResult] = []
    expired: list[Holder] = []

    for key in ordered:
        result, just_expired = claim(store, run_id, key, owner, ttl_seconds, overrides)
        expired += just_expired
        results.append(result)
        if not result.granted:
            for granted in results:
                if granted.granted:
                    release(store, granted.key, owner)
            return False, results, expired

    return True, results, expired


def renew(store: Store, key: str, owner: str, ttl_seconds: int) -> bool:
    validate(key)
    with store.write() as conn:
        cursor = conn.execute(
            "UPDATE claims SET expires_at = ? WHERE key = ? AND owner = ?",
            (_expiry(ttl_seconds), key, owner),
        )
        return cursor.rowcount > 0


def release(store: Store, key: str, owner: str) -> bool:
    validate(key)
    with store.write() as conn:
        cursor = conn.execute(
            "DELETE FROM claims WHERE key = ? AND owner = ?", (key, owner)
        )
        return cursor.rowcount > 0


def release_all(store: Store, run_id: str, owner: str) -> int:
    """Drop everything an owner holds. Used when a spawned agent exits."""
    with store.write() as conn:
        cursor = conn.execute(
            "DELETE FROM claims WHERE run_id = ? AND owner = ?", (run_id, owner)
        )
        return cursor.rowcount


def held_by(store: Store, run_id: str, owner: str | None = None) -> list[Holder]:
    with store.read() as conn:
        if owner:
            rows = conn.execute(
                "SELECT key, slot, owner, acquired_at, expires_at FROM claims"
                " WHERE run_id = ? AND owner = ? ORDER BY key, slot",
                (run_id, owner),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, slot, owner, acquired_at, expires_at FROM claims"
                " WHERE run_id = ? ORDER BY key, slot",
                (run_id,),
            ).fetchall()
    return [Holder(**dict(r)) for r in rows]


def _expiry(ttl_seconds: int) -> str:
    import datetime as dt

    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
