"""`metis start` -- open the ledger and all three agents, each in its own window.

Four terminals, each needing an environment variable and a pasted briefing, is
four chances to get something subtly wrong -- and the usual mistake is silent.
Launch without `METIS_ROLE` and the session looks fine, behaves normally, and no
safety rail fires. Nothing tells you.

Nothing clever happens here. Each window runs exactly the command you would type
yourself: `metis watch`, or `claude` with a role exported and a briefing passed
as its first argument.

Those commands are written to small generated scripts rather than composed
inline, because the briefing contains backticks and quotes and has to survive
Python, then AppleScript, then the shell. A path crosses all three untouched;
a quoted string crossing three layers is a bug waiting for a different machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .bus.store import BusError, Store
from .config import load

SESSION = "metis"
AGENTS = ("swe", "devops", "tester")

TITLES = {
    "ledger": " LEDGER ",
    "swe": " SWE - writes code ",
    "devops": " DEVOPS - builds and deploys ",
    "tester": " TESTER - verifies ",
}


# Agents run unattended, so a session that stops for a click has stopped for
# good -- nobody is watching to answer it.
#
# This is defensible here for one specific reason, verified rather than assumed:
# Metis's hooks fire independently of Claude Code's permission system. Launching
# with permissions bypassed, a DevOps session asked to edit source is still
# refused by `metis hook pre`, and the file is untouched. The role boundaries,
# the test-tampering rule, the change-set push gate and the universal denies
# (force-push, rm -rf /, curl | sh, fork bombs) all still apply.
#
# What is genuinely given up: the hook matcher covers Edit, Write, MultiEdit,
# NotebookEdit and Bash. Any other tool -- WebFetch, MCP -- Metis never
# inspects, and with permissions bypassed nothing else does either. That is the
# trade, and `--ask` declines it.
AUTONOMOUS_MODE = "bypassPermissions"


def briefing(role: str) -> str:
    return (
        f"You are the {role.upper()} agent in a Metis run. "
        f"Run `metis context --agent {role}` to get your role and the current "
        "state, then follow it. Arm a Monitor on "
        f"`metis tail --agent {role}` so events wake you, and keep working "
        "until the requirement is done or the iteration cap is reached."
    )


# ------------------------------------------------------------- preflight


def _trusted(workspace: Path) -> bool:
    """Has Claude Code been trusted in this directory?

    Untrusted, every window opens on a confirmation prompt and sits there. That
    looks identical to agents having nothing to do, which is the most confusing
    way for a run to fail.
    """
    config = Path.home() / ".claude.json"
    if not config.exists():
        return False
    try:
        projects = json.loads(config.read_text(errors="replace")).get("projects", {})
    except (ValueError, OSError):
        return False
    entry = projects.get(str(workspace.resolve()))
    return bool(entry and entry.get("hasTrustDialogAccepted"))


def _preflight(cfg, store: Store, workspace: Path) -> list[str]:
    """Everything that would otherwise fail quietly once the windows are open."""
    problems: list[str] = []

    if not shutil.which("claude"):
        problems.append("claude is not on PATH")

    if not shutil.which("metis"):
        # The hooks shell out to `metis hook pre`. Without it they never fire,
        # and a run that enforces nothing looks exactly like one that does.
        problems.append("metis is not on PATH -- the hooks would never fire")

    settings = workspace / ".claude" / "settings.json"
    if not settings.exists() or "metis hook" not in settings.read_text(errors="replace"):
        problems.append("hooks are not installed here (run: metis install-hooks)")

    if not _trusted(workspace):
        problems.append(
            f"Claude Code has not been trusted in {workspace}. Every window would "
            "open on a confirmation prompt. Run `claude` here once, accept it, "
            "then /exit."
        )

    if not store.exists():
        problems.append("no run yet (run: metis work, or metis init-run)")
    else:
        try:
            store.resolve_run(None)
        except BusError:
            problems.append("no run yet (run: metis work, or metis init-run)")

    spawned = [n for n, a in cfg.agents.items() if a.mode != "attached"]
    if spawned:
        problems.append(
            f"these agents are not 'attached': {', '.join(sorted(spawned))}. "
            "A spawned agent writes to a log file rather than a terminal, so "
            "its window would sit empty."
        )

    return problems


# --------------------------------------------------------------- launching


def _launcher(workspace: Path, name: str, body: str) -> Path:
    directory = workspace / ".metis" / "launch"
    directory.mkdir(parents=True, exist_ok=True)

    script = directory / f"{name}.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"cd {shlex.quote(str(workspace))} || exit 1\n"
        f"{body}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _scripts(cfg, workspace: Path, ask: bool = False) -> list[tuple[str, Path]]:
    made = [("ledger", _launcher(workspace, "ledger", "exec metis watch"))]
    for role in AGENTS:
        if role not in cfg.agents:
            continue
        flags = "" if ask else f" --permission-mode {AUTONOMOUS_MODE}"
        made.append((role, _launcher(
            workspace, role,
            f"export METIS_ROLE={role}\n"
            f"exec claude{flags} {shlex.quote(briefing(role))}",
        )))
    return made


def _terminal_app() -> str:
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        return "iTerm"
    return "iTerm" if Path("/Applications/iTerm.app").exists() else "Terminal"


def _open_window(app: str, script: Path) -> bool:
    if app == "iTerm":
        source = ('tell application "iTerm"\n'
                  "  create window with default profile\n"
                  "  tell current session of current window\n"
                  f'    write text "{script}"\n'
                  "  end tell\n"
                  "end tell\n")
    else:
        source = f'tell application "Terminal" to do script "{script}"\n'

    return subprocess.run(["osascript", "-e", source],
                          capture_output=True).returncode == 0


def cmd_start(args: argparse.Namespace) -> int:
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)
    store = Store(cfg.bus_path())
    workspace = Path(cfg.workspace).resolve()

    problems = _preflight(cfg, store, workspace)
    if problems and not args.force:
        print("Not ready to start:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nFix these, or pass --force to start anyway.", file=sys.stderr)
        return 1

    scripts = _scripts(cfg, workspace, ask=args.ask)

    if args.tmux:
        return _start_tmux(args, workspace, scripts)

    if args.print:
        print("Run each of these in its own terminal:\n")
        for _, script in scripts:
            print(f"  {script}")
        return 0

    if sys.platform != "darwin":
        print("Opening windows automatically is macOS-only. Run these yourself,"
              " one per terminal:\n", file=sys.stderr)
        for _, script in scripts:
            print(f"  {script}", file=sys.stderr)
        return 1

    app = _terminal_app()
    if not args.ask:
        print("Agents run without permission prompts -- a session that stops for a")
        print("click has stopped for good when nobody is watching. Metis's own")
        print("refusals still apply: they are hooks, not permissions. Use --ask to")
        print("be prompted instead.\n")
    print(f"Opening {len(scripts)} {app} windows\n")

    opened = 0
    for name, script in scripts:
        if _open_window(app, script):
            print(f"  {TITLES.get(name, name).strip()}")
            opened += 1
        else:
            print(f"  {name}: failed -- run {script} yourself", file=sys.stderr)

    if opened:
        print("\nThe ledger window carries the story. Closing a window stops that")
        print("agent; the run survives, and `metis` shows where it got to.")
    return 0 if opened == len(scripts) else 1


def _start_tmux(args, workspace: Path, scripts) -> int:
    if not shutil.which("tmux"):
        print("tmux is not installed: brew install tmux", file=sys.stderr)
        return 1

    session = args.session
    exists = subprocess.run(["tmux", "has-session", "-t", session],
                            capture_output=True).returncode == 0
    if exists:
        if not args.replace:
            print(f"Session '{session}' already exists. "
                  f"Attach with: tmux attach -t {session}\n"
                  "Or pass --replace to start over.")
            return 1
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)

    build = ["tmux", "new-session", "-d", "-s", session, "-c", str(workspace),
             "-x", "240", "-y", "64", ";"]
    for index, (name, script) in enumerate(scripts):
        if index:
            build += ["split-window", "-c", str(workspace), ";"]
        build += ["send-keys", str(script), "C-m", ";",
                  "select-pane", "-T", TITLES.get(name, name), ";"]
    # Split order alone leaves the panes in a surprising arrangement.
    build += ["select-layout", "main-vertical"]

    subprocess.run(build, check=True)
    for option, value in (("main-pane-width", "96"), ("pane-border-status", "top"),
                          ("pane-border-format", "#[bold]#{pane_title}"), ("status", "off")):
        subprocess.run(["tmux", "set", "-t", session, "-g", option, value], check=False)
    subprocess.run(["tmux", "select-layout", "-t", session, "main-vertical"], check=False)

    if args.no_attach:
        print(f"Started '{session}'. Attach with: tmux attach -t {session}")
        return 0
    return subprocess.run(["tmux", "attach", "-t", session]).returncode


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("start", help="open the ledger and all three agents")
    p.add_argument("--tmux", action="store_true",
                   help="one tmux window with panes, instead of separate windows")
    p.add_argument("--print", action="store_true",
                   help="write the launchers and print them, opening nothing")
    p.add_argument("--session", default=SESSION, help=f"tmux session name (default {SESSION})")
    p.add_argument("--replace", action="store_true", help="tmux: kill an existing session first")
    p.add_argument("--no-attach", action="store_true", help="tmux: set up without attaching")
    p.add_argument("--ask", action="store_true",
                   help="prompt for permissions (default is unattended; Metis's "
                        "own refusals apply either way)")
    p.add_argument("--force", action="store_true", help="start despite preflight problems")
    p.set_defaults(func=cmd_start)
