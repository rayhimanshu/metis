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


def cmd_install_hooks(args: argparse.Namespace) -> int:
    project = Path(args.project or ".").expanduser().resolve()
    if not project.is_dir():
        print(f"not a directory: {project}", file=sys.stderr)
        return 2

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

    if args.dry_run:
        print(json.dumps(merged, indent=2))
        return 0

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {settings_path} ({added} hook(s) added)")
    print("\nLaunch an agent with its role in the environment:\n")
    for role in ROLES:
        print(f"  METIS_ROLE={role} claude")
    print("\nWithout METIS_ROLE the hooks stay inert, so an ordinary session is unaffected.")
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
    p.add_argument("project", nargs="?", help="defaults to the current directory")
    p.add_argument("--dry-run", action="store_true", help="print the merged settings")
    p.add_argument("--force", action="store_true", help="proceed even if metis is not on PATH")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("roles", help="show role prompts, or copy them into the project")
    p.add_argument("--eject", action="store_true", help="copy packaged prompts into ./roles")
    p.add_argument("--force", action="store_true", help="overwrite existing project copies")
    p.set_defaults(func=cmd_roles)
