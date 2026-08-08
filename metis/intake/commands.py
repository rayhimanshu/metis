"""CLI handlers for intake."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ..bus.store import Store
from ..config import load
from . import sync


def _known_targets(cfg) -> list[str]:
    """Targets discovery found, used to bound what a ticket may point at.

    Best-effort: if discovery has not run, targets cannot be resolved and every
    requirement arrives without a hint. That is the safe direction to fail.
    """
    try:
        from ..discovery import pipeline

        _, targets, _ = pipeline.discover(str(cfg.workspace), environment=cfg.environment)
        return [t.name for t in targets]
    except Exception:
        return []


def cmd_intake(args: argparse.Namespace) -> int:
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    if not cfg.intake:
        print("no intake sources configured in metis.yaml")
        return 1

    store = Store(cfg.bus_path())
    run = store.resolve_run(args.run)
    known = _known_targets(cfg) if not args.no_discover else []

    def once() -> None:
        result = sync.pull(store, run["id"], cfg, known,
                           dry_run=args.dry_run, mark_started=not args.dry_run)
        prefix = "would post" if args.dry_run else "posted"
        print(f"fetched {result.fetched}, {prefix} {len(result.posted)}, "
              f"already ingested {len(result.skipped)}")
        for identity in result.posted:
            print(f"  + {identity}")

        for identity, warnings in result.warnings:
            # Flagged, never stripped. Filtering gives a false sense of safety;
            # a warning that travels with the requirement is honest.
            print(f"  ! {identity} contains instruction-shaped text:")
            for warning in warnings:
                print(f"      - {warning}")

        mirrored = sync.push(store, run["id"], cfg, dry_run=args.dry_run)
        for line in mirrored:
            print(f"  -> {'would comment' if args.dry_run else 'commented'} {line}")

    if not args.watch:
        once()
        return 0

    interval = args.interval or max(
        int(s.get("poll_seconds", 120)) for s in cfg.intake.values()
    )
    print(f"polling every {interval}s (ctrl-c to stop)", flush=True)
    while True:
        try:
            once()
        except Exception as e:
            print(f"  error: {e}", flush=True)
        time.sleep(interval)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("intake", help="pull work from trackers, mirror progress back")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    p.add_argument("--watch", action="store_true", help="poll continuously")
    p.add_argument("--interval", type=int, help="seconds between polls")
    p.add_argument("--no-discover", action="store_true",
                   help="skip discovery (target hints will be empty)")
    p.add_argument("--run", help="run id (defaults to the most recent)")
    p.set_defaults(func=cmd_intake)
