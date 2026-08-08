"""`metis` command line.

Subcommands are registered per milestone; this file stays a dispatcher and holds
no logic of its own.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load, sample


def _load(args: argparse.Namespace) -> Config:
    return load(Path(args.config) if getattr(args, "config", None) else None)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or "metis.yaml")
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    target.write_text(sample(), encoding="utf-8")
    print(f"wrote {target}")
    print("\nNext: edit it, then run `metis setup jira` to store credentials.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from . import setup as setup_mod

    cfg = _load(args)

    if args.list or args.integration in (None, "list"):
        rows = setup_mod.status(cfg, verify=args.verify)
        from . import secrets

        print(f"store: {secrets.backend_description()}\n")
        for name, ok, detail in rows:
            mark = "ok " if ok else "-- "
            print(f"  {mark} {name:8} {detail}")
        print("\nValues are never displayed. Configure with: metis setup <integration>")
        return 0

    return setup_mod.run_setup(args.integration, cfg)


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import secrets
    from . import setup as setup_mod

    problems = 0

    try:
        cfg = _load(args)
    except ConfigError as e:
        print(f"config: FAIL {e}")
        return 1

    print(f"config      {cfg.path or '(defaults -- no metis.yaml found)'}")
    print(f"workspace   {cfg.workspace}" + ("" if cfg.workspace.is_dir() else "   MISSING"))
    if not cfg.workspace.is_dir():
        problems += 1
    print(f"environment {cfg.environment}")
    print(f"secrets     {secrets.backend_description()}")

    print("\nagents")
    for name, agent in sorted(cfg.agents.items()):
        resolved = cfg.role_path(name)
        if resolved is None:
            note = f"   no role prompt for '{name}' (add {agent.role})"
            problems += 1
        elif resolved.is_relative_to(Path(__file__).parent):
            note = "   (packaged prompt)"
        else:
            note = f"   {agent.role}"
        print(f"  {name:8} {agent.mode:9} wakes on {', '.join(agent.wake_on)}{note}")

    # An event nobody wakes on is delivered nowhere. This is the failure that
    # looks like perfect health and does nothing at all, so it is worth
    # surfacing before a run rather than during one.
    from .bus.audit import ROUTABLE_TYPES

    consumed = {e for a in cfg.agents.values() for e in a.wake_on}
    orphans = sorted(ROUTABLE_TYPES - consumed)
    if orphans:
        print(f"\n  note: no agent wakes on: {', '.join(orphans)}")
        print("        (fine if intentional -- but a typo here is silent)")

    # Run health, when there is a run to check.
    from .bus.audit import run_checks
    from .bus.store import Store

    store = Store(cfg.bus_path())
    if store.exists():
        run = store.latest_run()
        if run:
            print(f"\nrun {run['id']}")
            for check in run_checks(store, cfg, run["id"]):
                print(f"  {'ok ' if check.ok else '-- '} {check.name:15} {check.detail}")
                if not check.ok and check.hint:
                    print(f"      hint: {check.hint}")
                    problems += 1

    print("\nintegrations")
    if not cfg.intake:
        print("  (none configured in metis.yaml)")
    for name, ok, detail in setup_mod.status(cfg, verify=args.verify):
        print(f"  {'ok ' if ok else '-- '} {name:8} {detail}")
        if not ok and name in cfg.intake:
            problems += 1

    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metis",
        description="A substrate for autonomous engineering agents.",
    )
    parser.add_argument("--version", action="version", version=f"metis {__version__}")
    parser.add_argument("-c", "--config", help="path to metis.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write a starter metis.yaml")
    p_init.add_argument("path", nargs="?", help="defaults to ./metis.yaml")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_setup = sub.add_parser("setup", help="store and verify integration credentials")
    p_setup.add_argument("integration", nargs="?", help="jira | trello | git (omit to list)")
    p_setup.add_argument("--list", action="store_true", help="show what is configured")
    p_setup.add_argument("--verify", action="store_true", help="make a live call when listing")
    p_setup.set_defaults(func=cmd_setup)

    p_doctor = sub.add_parser("doctor", help="check configuration and integrations")
    p_doctor.add_argument("--verify", action="store_true", help="make live calls to integrations")
    p_doctor.set_defaults(func=cmd_doctor)

    from . import dispatch_commands, hook_commands
    from .bus import audit_commands
    from .bus import commands as bus_commands
    from .discovery import commands as discovery_commands
    from .intake import commands as intake_commands
    from .policy import commands as policy_commands

    bus_commands.register(sub)
    audit_commands.register(sub)
    discovery_commands.register(sub)
    intake_commands.register(sub)
    dispatch_commands.register(sub)
    policy_commands.register(sub)
    hook_commands.register(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # BusError and friends
        from .bus.store import BusError

        if isinstance(e, BusError):
            print(f"error: {e}", file=sys.stderr)
            return 2
        raise
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
