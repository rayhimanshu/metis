"""CLI handler for the dispatcher."""

from __future__ import annotations

import argparse
from pathlib import Path

from .bus.store import Store
from .config import load
from .dispatcher import DEFAULT_DEBOUNCE, DEFAULT_TIMEOUT, Dispatcher


def cmd_dispatch(args: argparse.Namespace) -> int:
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    store = Store(cfg.bus_path())
    run = store.resolve_run(args.run)

    dispatcher = Dispatcher(
        store=store, cfg=cfg, run_id=run["id"],
        dry_run=args.dry_run, timeout=args.timeout, debounce=args.debounce,
        only=args.agent.split(",") if args.agent else None,
    )

    if not dispatcher.agents():
        print("no agents are configured with mode: spawned")
        return 1

    if args.once:
        dispatcher.tick()
        return 0
    return dispatcher.run()


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("dispatch", help="wake spawned agents when their events arrive")
    p.add_argument("--dry-run", action="store_true", help="show what would be spawned")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--agent", help="comma-separated subset")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE)
    p.add_argument("--run", help="run id (defaults to the most recent)")
    p.set_defaults(func=cmd_dispatch)
