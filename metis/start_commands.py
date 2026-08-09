"""`metis start` -- open the whole run in one window.

Four terminals, each needing an environment variable and a pasted briefing, is
four chances to get something subtly wrong. The usual mistake is silent: launch
without `METIS_ROLE` and the session looks fine, behaves normally, and none of
the safety rails fire. Nothing tells you.

So this launches them, and refuses to launch when the things that make a run
work are missing.

`claude` takes an initial prompt as an argument, so each pane starts already
briefed. Typing the briefing in afterwards would mean guessing how long the
session takes to become ready, which is exactly the kind of timing assumption
that works on the machine it was written on.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .bus.store import BusError, Store
from .config import load

SESSION = "metis"

PANES = [
    ("swe", " SWE - writes code "),
    ("devops", " DEVOPS - builds and deploys "),
    ("tester", " TESTER - verifies "),
]


def briefing(role: str) -> str:
    return (
        f"You are the {role.upper()} agent in a Metis run. "
        f"Run `metis context --agent {role}` to get your role and the current "
        "state, then follow it. Arm a Monitor on "
        f"`metis tail --agent {role}` so events wake you, and keep working "
        "until the requirement is done or the iteration cap is reached."
    )


def _preflight(cfg, store: Store) -> list[str]:
    """Everything that would otherwise fail quietly once the panes are open."""
    problems: list[str] = []

    if not shutil.which("claude"):
        problems.append("claude is not on PATH")

    if not shutil.which("metis"):
        # The hooks shell out to `metis hook pre`. Without it on PATH they never
        # fire, and a run that enforces nothing looks exactly like one that does.
        problems.append("metis is not on PATH -- the hooks would never fire")

    settings = Path(cfg.root or Path.cwd()) / ".claude" / "settings.json"
    if not settings.exists() or "metis hook" not in settings.read_text(errors="replace"):
        problems.append("hooks are not installed here (run: metis install-hooks)")

    if not store.exists():
        problems.append("no run yet (run: metis work, or metis init-run)")
    else:
        try:
            store.resolve_run(None)
        except BusError:
            problems.append("no run yet (run: metis work, or metis init-run)")

    attached = [n for n, a in cfg.agents.items() if a.mode != "attached"]
    if attached:
        problems.append(
            f"these agents are not 'attached': {', '.join(sorted(attached))}. "
            "A spawned agent writes to a log file rather than a terminal, so "
            "there would be nothing to watch in its pane."
        )

    return problems


def _manual(cwd: Path) -> None:
    print("\nWithout tmux, open four terminals in this directory:\n")
    print("  metis watch")
    for role, _ in PANES:
        print(f"  METIS_ROLE={role} claude {shlex.quote(briefing(role))}")


def cmd_start(args: argparse.Namespace) -> int:
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    store = Store(cfg.bus_path())
    cwd = Path(cfg.root or Path.cwd())

    problems = _preflight(cfg, store)
    if problems and not args.force:
        print("Not ready to start:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nFix these, or pass --force to start anyway.", file=sys.stderr)
        return 1

    if not shutil.which("tmux"):
        print("tmux is not installed: brew install tmux")
        _manual(cwd)
        return 1

    session = args.session
    if subprocess.run(["tmux", "has-session", "-t", session],
                      capture_output=True).returncode == 0:
        if not args.replace:
            print(f"Session '{session}' already exists. "
                  f"Attach with: tmux attach -t {session}\n"
                  "Or pass --replace to start over.")
            return 1
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)

    build = [
        "tmux", "new-session", "-d", "-s", session, "-c", str(cwd),
        "-x", "240", "-y", "64", ";",
        "send-keys", "metis watch", "C-m", ";",
        "select-pane", "-T", " LEDGER ", ";",
    ]
    for role, title in PANES:
        command = f"METIS_ROLE={role} claude {shlex.quote(briefing(role))}"
        build += [
            "split-window", "-c", str(cwd), ";",
            "send-keys", command, "C-m", ";",
            "select-pane", "-T", title, ";",
        ]
    # Split order alone leaves the panes in a surprising arrangement; the layout
    # is what puts the ledger down the left and the agents stacked beside it.
    build += ["select-layout", "main-vertical"]

    subprocess.run(build, check=True)
    for option, value in (("main-pane-width", "96"),
                          ("pane-border-status", "top"),
                          ("pane-border-format", "#[bold]#{pane_title}"),
                          ("status", "off")):
        subprocess.run(["tmux", "set", "-t", session, "-g", option, value], check=False)
    subprocess.run(["tmux", "select-layout", "-t", session, "main-vertical"], check=False)
    subprocess.run(["tmux", "select-pane", "-t", f"{session}.0"], check=False)

    if args.no_attach:
        print(f"Started '{session}'. Attach with: tmux attach -t {session}")
        return 0

    print(f"Attaching to '{session}'. Detach with ctrl-b d.")
    return subprocess.run(["tmux", "attach", "-t", session]).returncode


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("start", help="open the ledger and all three agents in one window")
    p.add_argument("--session", default=SESSION, help=f"tmux session name (default {SESSION})")
    p.add_argument("--replace", action="store_true", help="kill an existing session first")
    p.add_argument("--no-attach", action="store_true", help="set it up without attaching")
    p.add_argument("--force", action="store_true", help="start despite preflight problems")
    p.set_defaults(func=cmd_start)
