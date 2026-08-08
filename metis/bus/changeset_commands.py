"""CLI handlers for change sets."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..config import load
from . import changesets as cs
from . import events as ev
from .store import Store


def _store(args: argparse.Namespace):
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    return Store(cfg.bus_path()), cfg


def _owner(args: argparse.Namespace) -> str:
    return getattr(args, "agent", None) or os.environ.get("METIS_ROLE") or "unknown"


def cmd_new(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]

    changeset = cs.create(store, run["id"], targets, _owner(args), args.reason)
    ev.post(store, run["id"], "changeset_opened", agent=_owner(args),
            payload={"change_set": changeset.id, "targets": changeset.targets,
                     "reason": args.reason},
            rationale=args.reason or f"change spanning {len(changeset.targets)} repositories",
            change_set=changeset.id)

    print(changeset.id)
    print(f"\nAdd this trailer to every commit in the set:\n\n  {changeset.trailer}\n")
    print("No repository in the set may be pushed until all of them have built.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    store.resolve_run(args.run)

    changeset = cs.get(store, args.id)
    if not changeset:
        print(f"no such change set: {args.id}", file=sys.stderr)
        return 1

    state = cs.progress(store, changeset.id)
    print(f"{changeset.id}   status={changeset.status}")
    if changeset.reason:
        print(f"reason: {changeset.reason}")
    print(f"opened by {changeset.created_by} at {changeset.created_at}")
    print(f"\ncommit trailer:\n  {changeset.trailer}\n")

    print("targets")
    for target, progress in sorted(state.items()):
        mark = "ok " if progress == cs.PASSED else "-- "
        print(f"  {mark} {target:28} {progress}")

    blocking = cs.blocking_targets(store, changeset.id)
    print()
    if blocking:
        print(f"push blocked: {', '.join(blocking)} not built")
    else:
        print("all targets built -- the set may be pushed")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)

    sets = cs.listing(store, run["id"], args.status)
    if not sets:
        print("no change sets")
        return 0
    for changeset in sets:
        blocking = cs.blocking_targets(store, changeset.id) if changeset.status == cs.OPEN else []
        note = f"blocked on {', '.join(blocking)}" if blocking else "ready"
        print(f"  {changeset.id:28} {changeset.status:10} "
              f"{', '.join(changeset.targets):40} {note}")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    """Ask whether a repository may be pushed. Exit 1 means no."""
    store, _ = _store(args)
    run = store.resolve_run(args.run)

    allowed, reason = cs.may_push(store, run["id"], args.target)
    if allowed:
        print(f"{args.target}: clear to push")
        return 0
    print(reason, file=sys.stderr)
    return 1


def cmd_rollback_plan(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    store.resolve_run(args.run)
    for line in cs.rollback_plan(store, args.id):
        print(line)
    print("\n# Emitted, never executed. Review each line before running it.")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    store, _ = _store(args)
    run = store.resolve_run(args.run)

    changeset = cs.get(store, args.id)
    if not changeset:
        print(f"no such change set: {args.id}", file=sys.stderr)
        return 1

    status = cs.ABANDONED if args.abandon else cs.PUSHED
    if status == cs.PUSHED and not cs.is_built(store, args.id):
        blocking = cs.blocking_targets(store, args.id)
        print(f"refusing: {', '.join(blocking)} have not built", file=sys.stderr)
        return 1

    cs.set_status(store, args.id, status)
    ev.post(store, run["id"], "changeset_closed", agent=_owner(args),
            payload={"change_set": args.id, "status": status},
            rationale=f"change set {status.lower()}", change_set=args.id)
    print(f"{args.id}: {status}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("changeset", help="changes that span repositories")
    inner = p.add_subparsers(dest="changeset_command", required=True)

    q = inner.add_parser("new", help="open a set over two or more targets")
    q.add_argument("--targets", required=True, help="comma-separated")
    q.add_argument("--reason")
    q.add_argument("--agent")
    q.add_argument("--run")
    q.set_defaults(func=cmd_new)

    q = inner.add_parser("show", help="per-target progress and whether it may push")
    q.add_argument("id")
    q.add_argument("--run")
    q.set_defaults(func=cmd_show)

    q = inner.add_parser("list", help="change sets in this run")
    q.add_argument("--status", choices=[cs.OPEN, cs.BUILT, cs.PUSHED, cs.ABANDONED])
    q.add_argument("--run")
    q.set_defaults(func=cmd_list)

    q = inner.add_parser("gate", help="may this repository be pushed? exit 1 = no")
    q.add_argument("target")
    q.add_argument("--run")
    q.set_defaults(func=cmd_gate)

    q = inner.add_parser("rollback-plan", help="per-repository reset, emitted not executed")
    q.add_argument("id")
    q.add_argument("--run")
    q.set_defaults(func=cmd_rollback_plan)

    q = inner.add_parser("close", help="mark a set pushed, or abandon it")
    q.add_argument("id")
    q.add_argument("--abandon", action="store_true")
    q.add_argument("--agent")
    q.add_argument("--run")
    q.set_defaults(func=cmd_close)
