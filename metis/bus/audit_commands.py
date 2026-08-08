"""CLI handlers for the audit surface."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from ..config import load
from . import audit
from . import events as ev
from . import leases
from .store import Store


def _store(args: argparse.Namespace):
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    return Store(cfg.bus_path()), cfg


def cmd_log(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    rows = audit.log(store, run["id"], types=args.types.split(",") if args.types else None,
                     target=args.target, agent=args.agent, limit=args.limit)
    for row in rows:
        print(audit.format_row(row))
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    nodes = audit.trace(store, run["id"], args.event)
    if not nodes:
        print(f"no such event: {args.event}", file=sys.stderr)
        return 1
    for depth, row in nodes:
        print("  " * depth + ("└─ " if depth else "") + audit.format_row(row).splitlines()[0])
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    store.resolve_run(args.run)
    chain = audit.why(store, args.event)
    if not chain:
        print(f"no such event: {args.event}", file=sys.stderr)
        return 1
    for i, row in enumerate(chain):
        arrow = "" if i == 0 else "  ↓ caused\n"
        print(arrow + audit.format_row(row))
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    for agent, rows in audit.timeline(store, run["id"]).items():
        print(f"\n{agent}  ({len(rows)} events)")
        for row in rows[-args.limit:]:
            marker = "*" if row["tier"] == "ground_truth" else " "
            print(f"  {marker} #{row['id']:<4} {row['ts'][11:19]}  {row['type']:<16}"
                  f" {row['target'] or ''}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    store.resolve_run(args.run)
    body = audit.diff_for(store, args.event)
    if body is None:
        print(f"no diff stored for event {args.event}", file=sys.stderr)
        return 1
    print(body)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    if not args.at and args.before_event is None:
        print("pass --at <timestamp> or --before-event <id>", file=sys.stderr)
        return 2
    state = audit.replay(store, run["id"], at=args.at, before_event=args.before_event)

    print(f"state as of {state['at']}")
    print(f"  events so far : {state['events']}")
    print(f"  iteration     : {state['iteration']}")
    for target, phase in sorted(state["phases"].items()):
        print(f"  {target:24} {phase}")
    if state["last_event"]:
        print(f"\nlast event before that moment:\n{audit.format_row(state['last_event'])}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    store, cfg = _store(args)
    run = store.resolve_run(args.run)

    try:
        while True:
            rows = ev.read_since(store, run["id"], 0, limit=10_000)
            held = leases.held_by(store, run["id"])
            os.system("clear" if os.name != "nt" else "cls")

            print(f"metis · run {run['id']} · {store.get_run(run['id'])['status']}")
            print(f"iteration {ev.current_iteration(store, run['id'])} of {run['max_iterations']}"
                  f" · {len(rows)} events\n")

            print("agents")
            for name, agent in sorted(cfg.agents.items()):
                cursor = ev.get_cursor(store, run["id"], name)
                unread = sum(1 for r in rows
                             if int(r["id"]) > cursor and r["type"] in agent.wake_on)
                mine = [h.key for h in held if h.owner == name]
                print(f"  {name:9} {agent.mode:9} unread={unread:<3} "
                      f"holds={', '.join(mine) or '-'}")

            print("\nrecent")
            for row in rows[-args.tail:]:
                print("  " + audit.format_row(row).splitlines()[0])

            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_report(args: argparse.Namespace) -> int:
    store, cfg = _store(args)
    run = store.resolve_run(args.run)
    text = audit.report(store, cfg, run["id"])

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    common = dict(help="run id (defaults to the most recent)")

    p = sub.add_parser("log", help="interleaved event timeline")
    p.add_argument("--types")
    p.add_argument("--target")
    p.add_argument("--agent")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--run", **common)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("trace", help="everything that followed from an event")
    p.add_argument("event", type=int)
    p.add_argument("--run", **common)
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("why", help="what caused this, and what caused that")
    p.add_argument("event", type=int)
    p.add_argument("--run", **common)
    p.set_defaults(func=cmd_why)

    p = sub.add_parser("timeline", help="swimlane per agent")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--run", **common)
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("diff", help="the change made by a file_changed event")
    p.add_argument("event", type=int)
    p.add_argument("--run", **common)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("replay", help="state as of a moment")
    p.add_argument("--at", help="ISO timestamp")
    p.add_argument("--before-event", type=int, help="event id (exact)")
    p.add_argument("--run", **common)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("watch", help="live view of agents, leases, and events")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--tail", type=int, default=12)
    p.add_argument("--run", **common)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("report", help="post-run summary")
    p.add_argument("--out", help="write to a file instead of stdout")
    p.add_argument("--run", **common)
    p.set_defaults(func=cmd_report)
