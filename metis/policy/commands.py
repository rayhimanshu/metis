"""CLI handler for probe policy."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import load
from ..discovery import pipeline
from . import bounds


def cmd_probes(args: argparse.Namespace) -> int:
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    _, targets, report = pipeline.discover(
        args.source or str(cfg.workspace), environment=cfg.environment
    )
    lb_paths = set(report["workspace"]["load_balancer_polled_paths"])

    chosen = [t for t in targets if not args.target or t.name == args.target]
    if args.target and not chosen:
        print(f"unknown target '{args.target}'. Known: "
              f"{', '.join(t.name for t in targets)}")
        return 2

    for target in chosen:
        if not target.capabilities and args.target is None:
            continue
        print(bounds.render(bounds.plan(target, lb_paths)))
        print()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("probes", help="what a health endpoint should check, and why")
    p.add_argument("source", nargs="?", help="path or git URL (defaults to the workspace)")
    p.add_argument("--target")
    p.set_defaults(func=cmd_probes)
