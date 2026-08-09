"""`metis` (the dashboard), and `metis work` (pick something up)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import work
from .bus import events as ev
from .bus import leases
from .bus.store import BusError, Store
from .config import load

AUTO_POLL_SECONDS = 60


def _open(args: argparse.Namespace):
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    return cfg, Store(cfg.bus_path())


def _run_or_start(store: Store, cfg, args) -> dict:
    """Resolve the current run, starting one if there is none.

    `intake` used to fail here and tell you to run `init-run` first, which meant
    inventing a requirement before the real ones had been fetched. The run is
    just a container; there is no reason a person should have to name it.
    """
    import datetime as _dt

    if not store.exists():
        store.initialize()
    else:
        try:
            return store.resolve_run(getattr(args, "run", None))
        except BusError:
            pass  # initialised, but nothing started yet

    run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    store.create_run(
        run_id=run_id, workspace=str(cfg.workspace), environment=cfg.environment,
        requirement="work picked up from the tracker",
        max_iterations=cfg.max_iterations,
    )
    run = store.resolve_run(run_id)
    print(f"started run {run['id']}\n")
    return run


# ------------------------------------------------------------------ display


def _show_tasks(tasks: list[work.Task], *, numbered: bool = False) -> None:
    for index, task in enumerate(tasks, 1):
        mark = f"{index:>2}. " if numbered else "    "
        where = f"[{task.target}]" if task.target else "[no target]"
        print(f"{mark}{task.issue_key:<12} {task.title[:44]:<44} {where}")
        print(f"        {task.state} -- {task.detail}")
        for warning in task.warnings:
            print(f"        ! {warning}")


def _show_issues(issues, *, numbered: bool = True) -> None:
    for index, issue in enumerate(issues, 1):
        mark = f"{index:>2}. " if numbered else "    "
        print(f"{mark}{issue.key:<12} {issue.title[:60]}")


# ---------------------------------------------------------------- dashboard


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Bare `metis`: what is happening, from the local ledger only.

    Deliberately offline. A status command that hits a tracker is slow, fails
    on a plane, and cannot be trusted to answer when you most want it to.
    """
    cfg, store = _open(args)

    if not store.exists():
        print("No run yet.\n\n  metis setup      configure this project"
              "\n  metis work       pick something up from your tracker")
        return 0

    try:
        run = store.resolve_run(getattr(args, "run", None))
    except BusError:
        print("No run yet. Start one with `metis work`.")
        return 0

    iteration = ev.current_iteration(store, run["id"])
    print(f"run {run['id']}   {run['status']}   "
          f"iteration {iteration}/{run['max_iterations']}   env={run['environment']}")

    tasks = work.in_flight(store, run["id"])
    live = [t for t in tasks if t.is_open]
    finished = [t for t in tasks if not t.is_open]

    print(f"\nin flight ({len(live)})")
    if live:
        _show_tasks(live)
    else:
        print("    nothing -- `metis work` to pick something up")

    if finished:
        print(f"\nfinished ({len(finished)})")
        _show_tasks(finished)

    from . import triage

    waiting = triage.pending(store, run["id"])
    if waiting:
        print(f"\nwaiting on you ({len(waiting)})")
        for item in waiting:
            print(f"    {item['issue_key']:<12} {item['title'][:44]:<44} "
                  f"[{item['target'] or 'no target'}]")
            print(f"        {'; '.join(item['reasons']) or 'flagged for review'}")
        print("    -> metis groom")

    holders = leases.held_by(store, run["id"])
    print(f"\nleases ({len(holders)})")
    for holder in holders:
        print(f"    {holder.describe()}")
    if not holders:
        print("    none held")

    if cfg.intake:
        print("\n`metis work` to see what is ready on your tracker.")
    return 0


# --------------------------------------------------------------------- work


def cmd_work(args: argparse.Namespace) -> int:
    cfg, store = _open(args)
    if not cfg.intake:
        print("No tracker configured. Run `metis setup` and choose Jira or Trello.",
              file=sys.stderr)
        return 1

    run = _run_or_start(store, cfg, args)
    if args.auto:
        return _auto(store, run, cfg, args)

    known = _known_targets(cfg, args)

    tasks = work.in_flight(store, run["id"])
    live = [t for t in tasks if t.is_open]
    if live:
        print(f"already in flight ({len(live)})")
        _show_tasks(live)
        print()

    print("fetching from your tracker ...")
    issues = work.available(cfg, store, run["id"])
    if not issues:
        print("\nNothing ready. Move a card into the ready list and run this again.")
        return 0

    print(f"\nready to pick up ({len(issues)})")
    _show_issues(issues)

    if args.list:
        return 0

    chosen = _choose(issues, args.all)
    if not chosen:
        print("nothing taken")
        return 0

    taken = work.take(store, run["id"], cfg, chosen, known)
    print(f"\ntaken ({len(taken)})")
    _show_tasks(taken)
    print("\nAgents will pick these up. `metis` to watch, `metis watch` to stream.")
    return 0


def _choose(issues, take_all: bool):
    if take_all:
        return issues
    if not sys.stdin.isatty():
        print("\nNot a terminal -- pass --all, or run this interactively.",
              file=sys.stderr)
        return []

    print("\nWhich? numbers like 1 or 1,3 -- 'a' for all, enter to skip")
    try:
        reply = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return []

    if not reply:
        return []
    if reply in ("a", "all"):
        return issues

    chosen = []
    for bit in reply.replace(" ", ",").split(","):
        if not bit:
            continue
        if not bit.isdigit() or not (1 <= int(bit) <= len(issues)):
            print(f"  ignoring '{bit}'")
            continue
        chosen.append(issues[int(bit) - 1])
    return chosen


# --------------------------------------------------------------------- auto


def _auto(store: Store, run, cfg, args: argparse.Namespace) -> int:
    """Keep the queue fed, without asking.

    This feeds work in; it does not run agents. Metis stays a substrate -- the
    agents remain sessions you can watch, interrupt, and inspect. Handing a
    machine the ability to both choose the work and perform it unattended is a
    much larger promise than this makes.
    """
    interval = args.interval or AUTO_POLL_SECONDS
    limit = args.max_in_flight
    known = _known_targets(cfg, args)

    print(f"auto mode: keeping up to {limit} task(s) in flight, "
          f"checking every {interval}s (ctrl-c to stop)\n", flush=True)

    while True:
        try:
            tasks = work.in_flight(store, run["id"])
            live = [t for t in tasks if t.is_open]
            stuck = [t for t in tasks if t.state == work.NEEDS_HUMAN]

            if stuck:
                # Halting means a rule fired or the cap was reached. Taking on
                # more work while something needs a human buries the thing that
                # needs a human.
                print(f"paused: {len(stuck)} task(s) need a human "
                      f"({', '.join(t.issue_key for t in stuck)})", flush=True)

            elif len(live) >= limit:
                print(f"{len(live)} in flight, at the limit -- waiting", flush=True)

            else:
                issues = work.available(cfg, store, run["id"])
                room = limit - len(live)
                if issues:
                    taken = work.take(store, run["id"], cfg, issues[:room], known)
                    for task in taken:
                        print(f"took {task.issue_key}: {task.title[:56]}", flush=True)
                else:
                    print("nothing ready", flush=True)

            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped")
            return 0


def _known_targets(cfg, args) -> list[str]:
    if getattr(args, "no_discover", False):
        return []
    try:
        from .discovery import pipeline

        _, targets, _ = pipeline.discover(str(cfg.workspace), environment=cfg.environment)
        return [t.name for t in targets]
    except Exception:
        # Best-effort: without discovery a requirement arrives with no target
        # hint, which is the safe direction to fail.
        return []


# ----------------------------------------------------------------- register


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("work", help="see what is ready on your tracker and pick it up")
    p.add_argument("--list", action="store_true", help="show only, take nothing")
    p.add_argument("--all", action="store_true", help="take everything ready, no prompt")
    p.add_argument("--auto", action="store_true",
                   help="keep the queue fed without asking (agents still run in their own sessions)")
    p.add_argument("--max-in-flight", type=int, default=1,
                   help="auto mode: how many tasks to keep going at once (default 1)")
    p.add_argument("--interval", type=int, help="auto mode: seconds between checks")
    p.add_argument("--no-discover", action="store_true", help="skip target hints")
    p.add_argument("--run")
    p.set_defaults(func=cmd_work)
