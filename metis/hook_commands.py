"""`metis hook` and `metis install-hooks`."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import load
from .hooks import SETTINGS_TEMPLATE, dispatch

ROLES = ("swe", "devops", "tester")

# The coordination protocol itself. An agent runs these constantly -- claiming a
# lease, posting an event, waiting on one -- and stopping to ask permission for
# `metis post` is stopping to ask permission to speak. Pre-approving them keeps
# a run moving without touching what anything else may do.
PROTOCOL_PERMISSIONS = ["Bash(metis *)"]


def _allow_protocol(settings: dict) -> int:
    """Let agents run Metis's own commands without a prompt each time."""
    permissions = settings.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        return 0

    added = 0
    for entry in PROTOCOL_PERMISSIONS:
        if entry not in allow:
            allow.append(entry)
            added += 1
    return added


def cmd_hook(args: argparse.Namespace) -> int:
    return dispatch(args.which)


def _merge_hooks(existing: dict, incoming: dict) -> tuple[dict, int]:
    """Add Metis hooks without disturbing hooks that are already configured.

    Overwriting a settings file is a bad trade: whatever else was in there was
    put there deliberately, and clobbering it to save a merge is how a tool
    earns a reputation for being rude.
    """
    merged = json.loads(json.dumps(existing))  # deep copy
    merged.setdefault("hooks", {})
    added = 0

    for event, entries in incoming["hooks"].items():
        current = merged["hooks"].setdefault(event, [])
        for entry in entries:
            command = entry["hooks"][0]["command"]
            already = any(
                h.get("command") == command
                for block in current for h in block.get("hooks", [])
            )
            if not already:
                current.append(entry)
                added += 1

    return merged, added


def _strip_metis(settings: dict) -> tuple[dict, int]:
    """Remove Metis's hooks, leaving anyone else's alone.

    A project's settings file is not ours. Someone may have added their own
    hooks beside these, and uninstalling one tool has no business deleting
    another's configuration -- so this removes entries by command rather than
    dropping the block.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, 0

    removed = 0
    for event, matchers in list(hooks.items()):
        if not isinstance(matchers, list):
            continue

        kept_matchers = []
        for matcher in matchers:
            entries = matcher.get("hooks") if isinstance(matcher, dict) else None
            if not isinstance(entries, list):
                kept_matchers.append(matcher)
                continue

            kept = [e for e in entries
                    if not str(e.get("command", "")).startswith("metis hook")]
            removed += len(entries) - len(kept)

            # A matcher with no hooks left is an empty rule, not a rule that
            # matches nothing -- drop it rather than leaving debris behind.
            if kept:
                matcher["hooks"] = kept
                kept_matchers.append(matcher)

        if kept_matchers:
            hooks[event] = kept_matchers
        else:
            del hooks[event]

    if not hooks:
        del settings["hooks"]
    return settings, removed


def _target(args: argparse.Namespace) -> Path:
    """Where the hooks belong.

    The configured workspace, not the current directory. Agents run in the
    workspace, and Claude Code reads `.claude/settings.json` from wherever the
    session started -- so hooks installed anywhere else are simply never read.
    Every other command already resolves the workspace this way; this one was
    the odd one out, and quietly wiring up whichever directory you happened to
    be standing in is a poor way to find that out.
    """
    if args.project:
        return Path(args.project).expanduser().resolve()
    try:
        cfg = load(Path(args.config) if getattr(args, "config", None) else None)
        return Path(cfg.workspace).expanduser().resolve()
    except Exception:
        return Path.cwd()


def cmd_install_hooks(args: argparse.Namespace) -> int:
    project = _target(args)
    if not project.is_dir():
        print(f"not a directory: {project}", file=sys.stderr)
        return 2

    if getattr(args, "remove", False):
        return _remove_hooks(project, args.dry_run)

    if not shutil.which("metis") and not args.force:
        print("`metis` is not on PATH, so the hooks would never fire.", file=sys.stderr)
        print("Install it first (pipx install ...), or pass --force.", file=sys.stderr)
        return 1

    template = json.loads(SETTINGS_TEMPLATE.read_text(encoding="utf-8"))
    settings_path = project / ".claude" / "settings.json"

    existing: dict = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"{settings_path} is not valid JSON; refusing to overwrite it",
                  file=sys.stderr)
            return 1

    merged, added = _merge_hooks(existing, template)
    allowed = _allow_protocol(merged)

    if args.dry_run:
        print(json.dumps(merged, indent=2))
        return 0

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    if added or allowed:
        parts = []
        if added:
            parts.append(f"{added} hook(s)")
        if allowed:
            parts.append(f"{allowed} permission(s) so agents can run metis commands")
        print(f"wrote {settings_path}: added {' and '.join(parts)}")
    else:
        # "0 added" after asking to install reads like a failure.
        print(f"already set up in {settings_path} -- nothing to do")
    print("\nLaunch an agent with its role in the environment:\n")
    for role in ROLES:
        print(f"  METIS_ROLE={role} claude")
    print("\nWithout METIS_ROLE the hooks stay inert, so an ordinary session is unaffected.")
    return 0


def _remove_hooks(project: Path, dry_run: bool) -> int:
    settings_path = project / ".claude" / "settings.json"
    if not settings_path.is_file():
        print(f"no settings at {settings_path} -- nothing to remove")
        return 0

    try:
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"{settings_path} is not valid JSON; refusing to touch it", file=sys.stderr)
        return 1

    cleaned, removed = _strip_metis(existing)

    dropped = 0
    allow = cleaned.get("permissions", {}).get("allow")
    if isinstance(allow, list):
        kept = [e for e in allow if e not in PROTOCOL_PERMISSIONS]
        dropped = len(allow) - len(kept)
        if kept:
            cleaned["permissions"]["allow"] = kept
        else:
            del cleaned["permissions"]["allow"]
            if not cleaned["permissions"]:
                del cleaned["permissions"]

    if not removed and not dropped:
        print(f"nothing of Metis's in {settings_path}")
        return 0

    if dry_run:
        print(json.dumps(cleaned, indent=2))
        return 0

    settings_path.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")

    parts = []
    if removed:
        parts.append(f"{removed} hook(s)")
    if dropped:
        parts.append(f"{dropped} permission(s)")
    print(f"removed {' and '.join(parts)} from {settings_path}")
    print("Anything you added yourself was left alone.")
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    cfg = load(Path(args.config) if getattr(args, "config", None) else None)

    if not args.eject:
        for name in sorted(cfg.agents):
            path = cfg.role_path(name)
            origin = "packaged" if path and "site-packages" in str(path) or (
                path and path.is_relative_to(Path(__file__).parent)
            ) else "project"
            print(f"  {name:8} {origin:8} {path or '(missing)'}")
        print("\nCopy them into your project to customise: metis roles --eject")
        return 0

    target = cfg.root / "roles"
    target.mkdir(parents=True, exist_ok=True)
    packaged = Path(__file__).parent / "roles"

    for source in sorted(packaged.glob("*.md")):
        destination = target / source.name
        if destination.exists() and not args.force:
            print(f"  skip {destination.relative_to(cfg.root)} (exists)")
            continue
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  wrote {destination.relative_to(cfg.root)}")

    print("\nProject copies now win over the packaged prompts.")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("hook", help="internal: run a Claude Code hook (reads stdin)")
    p.add_argument("which", choices=["pre", "post"])
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("install-hooks", help="wire Metis hooks into a project's Claude settings")
    p.add_argument("project", nargs="?",
                   help="defaults to the workspace in metis.yaml")
    p.add_argument("--dry-run", action="store_true", help="print the merged settings")
    p.add_argument("--force", action="store_true", help="proceed even if metis is not on PATH")
    p.add_argument("--remove", action="store_true",
                   help="take Metis's hooks out again, leaving any others alone")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("roles", help="show role prompts, or copy them into the project")
    p.add_argument("--eject", action="store_true", help="copy packaged prompts into ./roles")
    p.add_argument("--force", action="store_true", help="overwrite existing project copies")
    p.set_defaults(func=cmd_roles)
