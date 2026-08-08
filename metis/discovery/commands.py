"""CLI handlers for discovery."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import load
from . import pipeline, report


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    source = args.source or str(cfg.workspace)

    resolved, targets, built = pipeline.discover(
        source, ref=args.ref, environment=args.environment or cfg.environment
    )
    print(report.summarize(built))

    if not args.no_write:
        runs_dir = cfg.root / ".metis" / "discovery"
        path = report.write(built, runs_dir, built["run_id"])
        print(f"\nreport: {path}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("discover", help="scan a repo and write the evidence report")
    p.add_argument("source", nargs="?", help="local path or git URL (defaults to the workspace)")
    p.add_argument("--ref", help="branch or tag (git sources only)")
    p.add_argument("--environment")
    p.add_argument("--no-write", action="store_true", help="print only")
    p.set_defaults(func=cmd_discover)
