"""CLI handlers for the bus. Thin: parse, call, format, choose an exit code.

Exit codes are part of the contract, because agents branch on them from shell:

    0  success / granted
    1  refused (no free slot, nothing matched, timed out)
    2  the run is over, or the request was invalid
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

from ..config import Config, load
from . import events as ev
from . import leases
from .store import BusError, Store, parse_ts


def _store(args: argparse.Namespace) -> tuple[Store, Config]:
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    return Store(cfg.bus_path()), cfg


def _owner(args: argparse.Namespace) -> str:
    """Who is acting. `$METIS_ROLE` is how a session identifies itself."""
    owner = getattr(args, "agent", None) or os.environ.get("METIS_ROLE")
    if not owner:
        raise BusError("no agent -- pass --agent or set METIS_ROLE")
    return owner


def _payload(raw: str | None):
    if not raw:
        return None
    if raw == "-":
        raw = sys.stdin.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw  # plain text is a legitimate payload


def _format(row) -> str:
    bits = [f"#{row['id']}", row["ts"], row["type"]]
    if row["agent"]:
        bits.append(f"by={row['agent']}")
    if row["target"]:
        bits.append(f"target={row['target']}")
    if row["caused_by"]:
        bits.append(f"caused_by=#{row['caused_by']}")
    if row["tier"] == "ground_truth":
        bits.append("[ground-truth]")
    line = "  ".join(bits)
    if row["rationale"]:
        line += f"\n    why: {row['rationale']}"
    if row["payload"]:
        body = row["payload"]
        line += f"\n    {body if len(body) <= 400 else body[:400] + ' ...'}"
    return line


# --------------------------------------------------------------- commands


def cmd_init_run(args: argparse.Namespace) -> int:
    store, cfg = _store(args)
    store.initialize()

    run_id = args.run_id or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    store.create_run(
        run_id=run_id,
        workspace=str(args.workspace or cfg.workspace),
        environment=args.environment or cfg.environment,
        requirement=args.requirement,
        max_iterations=args.max_iterations or cfg.max_iterations,
    )
    ev.post(store, run_id, "requirement", agent="human", payload=args.requirement,
            rationale="run started")
    print(run_id)
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    store, cfg = _store(args)
    run = store.resolve_run(args.run)

    event_id = ev.post(
        store, run["id"], args.type,
        agent=getattr(args, "agent", None) or os.environ.get("METIS_ROLE"),
        target=args.target,
        payload=_payload(args.payload),
        caused_by=args.caused_by,
        session_id=args.session_id or os.environ.get("METIS_SESSION_ID"),
        rationale=args.rationale,
        change_set=args.change_set or os.environ.get("METIS_CHANGE_SET"),
        secret_names=cfg.secret_names(),
        allow_human_only=args.i_am_human,
    )
    print(event_id)
    return 0


def cmd_await(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    types = args.For.split(",") if args.For else None

    row = ev.await_event(
        store, run["id"], types,
        agent=getattr(args, "agent", None) or os.environ.get("METIS_ROLE"),
        timeout=args.timeout,
    )
    if row is None:
        print(f"timed out after {args.timeout}s", file=sys.stderr)
        return 1
    print(_format(row), flush=True)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    agent = _owner(args)

    # An agent tails what it wakes on, unless told otherwise.
    types = args.types.split(",") if args.types else None
    if types is None:
        _, cfg = _store(args)
        if agent in cfg.agents:
            types = cfg.agents[agent].wake_on

    for row in ev.tail(store, run["id"], agent, types,
                       from_start=args.from_start, once=args.once):
        # flush=True is load-bearing: a watching session gets one notification
        # per line only if each line leaves the process immediately.
        print(_format(row), flush=True)
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    store, cfg = _store(args)
    run = store.resolve_run(args.run)
    owner = _owner(args)

    keys = args.keys
    granted, results, expired = leases.claim_all(
        store, run["id"], keys, owner, args.ttl
    )

    for holder in expired:
        ev.post(store, run["id"], "lease_expired", agent="substrate",
                payload={"key": holder.key, "owner": holder.owner},
                rationale="ttl elapsed")

    if not granted:
        for result in results:
            if not result.granted:
                print(f"refused: {result.key} -- {result.reason}", file=sys.stderr)
                for holder in result.holders:
                    print(f"  {holder.describe()}", file=sys.stderr)
                return 2 if "run is" in result.reason else 1
        return 1

    for result in results:
        ev.post(store, run["id"], "lease_acquired", agent=owner,
                payload={"key": result.key, "slot": result.slot, "ttl": args.ttl})
        print(f"granted: {result.key} (slot {result.slot}) for {args.ttl}s")
    return 0


def cmd_renew(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    store.resolve_run(args.run)
    owner = _owner(args)

    ok = all(leases.renew(store, key, owner, args.ttl) for key in args.keys)
    if not ok:
        print("not held by you (or already expired)", file=sys.stderr)
        return 1
    print(f"renewed {len(args.keys)} lease(s) for {args.ttl}s")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    owner = _owner(args)

    if args.all:
        count = leases.release_all(store, run["id"], owner)
        print(f"released {count} lease(s)")
        return 0

    released = 0
    for key in args.keys:
        if leases.release(store, key, owner):
            ev.post(store, run["id"], "lease_released", agent=owner, payload={"key": key})
            released += 1
    print(f"released {released} of {len(args.keys)} lease(s)")
    return 0 if released else 1


def cmd_leases(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    holders = leases.held_by(store, run["id"], args.agent)

    if not holders:
        print("no leases held")
        return 0
    for holder in holders:
        remaining = parse_ts(holder.expires_at) - _dt.datetime.now(_dt.UTC)
        secs = int(remaining.total_seconds())
        print(f"  {holder.key:34} {holder.owner:10} slot {holder.slot}  "
              f"{'expired' if secs < 0 else f'{secs}s left'}")
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)

    print(f"run          {run['id']}   status={run['status']}")
    print(f"requirement  {run['requirement']}")
    print(f"environment  {run['environment']}")

    iteration = ev.current_iteration(store, run["id"])
    print(f"iteration    {iteration} of {run['max_iterations']}"
          f"   ({run['max_iterations'] - iteration} remaining)")

    rows = ev.read_since(store, run["id"], 0, target=args.target, limit=10_000)
    print(f"events       {len(rows)}")

    if rows:
        print("\nrecent")
        for row in rows[-args.limit:]:
            print("  " + _format(row).replace("\n", "\n  "))

    holders = leases.held_by(store, run["id"])
    if holders:
        print("\nleases")
        for holder in holders:
            print(f"  {holder.describe()}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    from . import context as context_mod

    store, cfg = _store(args)
    agent = _owner(args)

    ctx = context_mod.build(store, cfg, agent, event_id=args.event, target=args.target)
    if args.json:
        print(json.dumps(ctx, indent=2, default=str))
    else:
        print(context_mod.render(ctx, include_role=not args.no_role))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    store, cfg = _store(args)
    run = store.resolve_run(args.run)
    from .. import secrets

    body = secrets.redact(args.body or "", cfg.secret_names())
    message_id = ev.send(store, run["id"], _owner(args), args.to, args.subject, body)
    print(message_id)
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    rows = ev.inbox(store, run["id"], _owner(args), unread_only=not args.all)

    if not rows:
        print("no messages")
        return 0
    for row in rows:
        print(f"#{row['id']}  {row['ts']}  from {row['from_agent']}: {row['subject']}")
        if row["body"]:
            print(f"    {row['body']}")
    return 0


# ---------------------------------------------------------------- parser


def register(sub: argparse._SubParsersAction) -> None:
    common_run = dict(help="run id (defaults to the most recent)")

    p = sub.add_parser("init-run", help="start a run")
    p.add_argument("--requirement", required=True)
    p.add_argument("--workspace")
    p.add_argument("--environment")
    p.add_argument("--max-iterations", type=int)
    p.add_argument("--run-id")
    p.set_defaults(func=cmd_init_run)

    p = sub.add_parser("post", help="append an event")
    p.add_argument("--type", required=True)
    p.add_argument("--target")
    p.add_argument("--payload", help="JSON or text; '-' reads stdin")
    p.add_argument("--caused-by", type=int, help="the event that triggered this")
    p.add_argument("--rationale", help="one line: why")
    p.add_argument("--session-id")
    p.add_argument("--change-set",
                   help="tag this event as part of a cross-repo change set "
                        "(or set METIS_CHANGE_SET)")
    p.add_argument("--agent")
    p.add_argument("--run", **common_run)
    p.add_argument("--i-am-human", action="store_true",
                   help="required for approval events")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("await", help="block until a matching event")
    p.add_argument("--for", dest="For", help="comma-separated event types")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--agent")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_await)

    p = sub.add_parser("tail", help="stream events, one line each")
    p.add_argument("--agent")
    p.add_argument("--types", help="override the agent's wake_on list")
    p.add_argument("--from-start", action="store_true")
    p.add_argument("--once", action="store_true", help="drain and exit")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_tail)

    p = sub.add_parser("claim", help="take lock keys (all or nothing, sorted order)")
    p.add_argument("keys", nargs="+")
    p.add_argument("--ttl", type=int, default=900)
    p.add_argument("--agent")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("renew", help="extend leases you hold")
    p.add_argument("keys", nargs="+")
    p.add_argument("--ttl", type=int, default=900)
    p.add_argument("--agent")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_renew)

    p = sub.add_parser("release", help="give back leases")
    p.add_argument("keys", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--agent")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("leases", help="show held leases")
    p.add_argument("--agent")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_leases)

    p = sub.add_parser("state", help="current state of a run")
    p.add_argument("--target")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("context", help="everything you need to act, from the ledger")
    p.add_argument("--agent")
    p.add_argument("--event", type=int, help="the event that woke you")
    p.add_argument("--target")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-role", action="store_true", help="omit the role file")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("send", help="message another agent")
    p.add_argument("--to", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--body")
    p.add_argument("--agent")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("inbox", help="read your messages")
    p.add_argument("--all", action="store_true", help="include already-read")
    p.add_argument("--agent")
    p.add_argument("--run", **common_run)
    p.set_defaults(func=cmd_inbox)
