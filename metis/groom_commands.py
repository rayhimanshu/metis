"""`metis groom` -- review what agents are not allowed to start alone."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import triage
from .bus import events as ev
from .bus.store import BusError, Store
from .config import load


def _open(args: argparse.Namespace):
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    return cfg, Store(cfg.bus_path())


def _show(waiting: dict, index: int | None = None, cfg=None) -> None:
    mark = f"{index}. " if index else ""
    print(f"\n{mark}{waiting['issue_key']}  {waiting['title']}")
    print(f"   target: {waiting['target'] or '(none resolved)'}")
    print(f"   why it stopped here: {'; '.join(waiting['reasons']) or 'flagged for review'}")
    if waiting["body"]:
        body = waiting["body"].strip().splitlines()
        print("   ---")
        for line in body[:12]:
            print(f"   {line}")
        if len(body) > 12:
            print(f"   ... {len(body) - 12} more lines")

    if waiting.get("design"):
        print("\n   approach proposed by SWE")
        for line in waiting["design"].strip().splitlines()[:15]:
            print(f"     {line}")

    if cfg is not None:
        _show_infrastructure(cfg)


def _show_infrastructure(cfg) -> None:
    """What is provisioned today, for deciding what a change would move.

    Facts read from IaC and nothing else. Metis does not attach a price to any
    of it: cloud pricing is regional, tiered, usage-dependent and moves
    constantly, so a figure generated here would be invented -- and would be
    believed precisely because it looked precise. The numbers below are what
    your repository declares; the cost of changing them comes from your
    provider's calculator.
    """
    try:
        from .discovery import iac

        index = iac.build_index(cfg.workspace)
    except Exception:
        return

    if not index.present:
        return

    print("\n   infrastructure declared in this workspace")
    if index.resources:
        kinds = sorted(index.resources)
        print(f"     resources: {', '.join(kinds[:10])}"
              + (f" (+{len(kinds) - 10} more)" if len(kinds) > 10 else ""))

    if index.sizing:
        print("     currently provisioned:")
        for attribute, values in sorted(index.sizing.items()):
            for value in values[:3]:
                print(f"       {attribute:22} {value}")

    if index.iam_actions:
        actions = sorted(index.iam_actions)
        print(f"     permissions: {', '.join(actions[:8])}"
              + (f" (+{len(actions) - 8} more)" if len(actions) > 8 else ""))

    print("     ^ read from your IaC. Metis attaches no prices -- check your")
    print("       provider's calculator before approving a sizing change.")


def cmd_groom(args: argparse.Namespace) -> int:
    cfg, store = _open(args)

    if not store.exists():
        print("No run yet.")
        return 0
    try:
        run = store.resolve_run(getattr(args, "run", None))
    except BusError:
        print("No run yet.")
        return 0

    waiting = triage.pending(store, run["id"])
    if not waiting:
        print("Nothing waiting on review.")
        return 0

    print(f"{len(waiting)} task(s) waiting on you")
    for index, item in enumerate(waiting, 1):
        _show(item, index, cfg=cfg)

    if args.list:
        print("\n`metis groom` without --list to review them.")
        return 0

    if not sys.stdin.isatty():
        print("\nNot a terminal -- run this interactively to approve or reject.",
              file=sys.stderr)
        return 1

    for item in waiting:
        if not _review(store, run["id"], cfg, item):
            break
    return 0


def _review(store: Store, run_id: str, cfg, item: dict) -> bool:
    """Returns False to stop going through the queue."""
    _show(item, cfg=cfg)
    print("\n  [a] approve   [e] edit the requirement, then approve"
          "   [r] reject   [s] skip   [q] quit")

    try:
        choice = input("  > ").strip().lower()[:1]
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if choice == "q":
        return False
    if choice == "s" or not choice:
        return True

    if choice == "e":
        print("\n  Refined requirement (one line, enter to keep as-is):")
        try:
            refined = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if refined:
            # Recorded as a new event rather than an edit: the ledger is
            # append-only, so what the tracker said and what a person decided
            # both survive.
            ev.post(store, run_id, "requirement", agent="human",
                    target=item["target"],
                    payload={"issue_key": item["issue_key"], "title": refined,
                             "body": refined, "source": "groomed",
                             "gate": triage.AUTONOMOUS, "gate_reasons": [],
                             "supersedes": item["event_id"]},
                    caused_by=item["event_id"],
                    rationale=f"groomed {item['issue_key']}: {refined[:80]}",
                    secret_names=cfg.secret_names())
            print("  refined")

    if choice in ("a", "e"):
        try:
            note = input("  Note for the record (optional): ").strip()
        except (EOFError, KeyboardInterrupt):
            note = ""
        ev.post(store, run_id, "approved", agent="human", target=item["target"],
                payload={"requirement": item["event_id"], "issue_key": item["issue_key"]},
                caused_by=item["event_id"],
                rationale=note or f"approved {item['issue_key']} after review",
                allow_human_only=True, secret_names=cfg.secret_names())
        print(f"  approved -- agents may now claim {item['target']}")

    elif choice == "r":
        try:
            why = input("  Why? ").strip()
        except (EOFError, KeyboardInterrupt):
            why = ""
        ev.post(store, run_id, "rejected", agent="human", target=item["target"],
                payload={"requirement": item["event_id"], "issue_key": item["issue_key"]},
                caused_by=item["event_id"],
                rationale=why or f"rejected {item['issue_key']}",
                allow_human_only=True, secret_names=cfg.secret_names())
        print("  rejected -- it stays blocked")

    return True


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("groom", help="review work agents may not start alone")
    p.add_argument("--list", action="store_true", help="show only, decide nothing")
    p.add_argument("--run")
    p.set_defaults(func=cmd_groom)
