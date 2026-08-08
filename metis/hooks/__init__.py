"""Claude Code hooks, invoked as `metis hook pre` and `metis hook post`.

They live inside the package rather than as loose scripts so that wiring them up
needs no path resolution. A settings file that points at
`$METIS_HOME/hooks/pre_tool_use.py` breaks the moment the tool is pip-installed,
moved, or run from another checkout; `metis hook pre` works wherever `metis` is
on PATH.

The two have opposite failure policies, and the asymmetry is deliberate:

* **pre** runs *before* the tool and can block. It fails **open** -- a hook that
  crashes and blocks turns a bug in the hook into a total outage of every agent.
  Failing open leaves leases, human-only approvals, and the ledger standing.
* **post** runs *after* the tool has already succeeded. Failing there would
  block work that has already happened, so it never blocks.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
COMMAND_TOOLS = {"Bash"}
MAX_DIFF_BYTES = 200_000

SETTINGS_TEMPLATE = Path(__file__).with_name("settings.template.json")


def _deny(reason: str) -> int:
    print(f"[metis] blocked: {reason}", file=sys.stderr)
    return 2


def run_pre(payload: dict[str, Any]) -> int:
    from ..enforcement import check_command, check_write, current_role, denied_tests_from_bus

    role = current_role()
    if not role:
        return 0

    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}

    from ..config import load

    try:
        cfg = load()
        workspace = cfg.workspace
    except Exception:
        cfg, workspace = None, Path(payload.get("cwd") or ".").resolve()

    if tool in COMMAND_TOOLS:
        allowed, reason = check_command(role, str(tool_input.get("command") or ""))
        return 0 if allowed else _deny(reason)

    if tool in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not path:
            return 0

        denied_tests: set[str] = set()
        if cfg is not None:
            try:
                from ..bus.store import Store

                store = Store(cfg.bus_path())
                if store.exists():
                    run = store.latest_run()
                    if run:
                        denied_tests = denied_tests_from_bus(store, run["id"])
            except Exception:
                # No bus, or unreadable. Role scoping still applies; only the
                # "test that caught you" rule is unavailable.
                pass

        allowed, reason = check_write(role, str(path), workspace, denied_tests)
        return 0 if allowed else _deny(reason)

    return 0


def _diff_for(path: Path, workspace: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "diff", "--", str(path)], cwd=workspace,
            capture_output=True, text=True, check=False, timeout=10,
        )
        return proc.stdout[:MAX_DIFF_BYTES] if proc.returncode == 0 and proc.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None


def run_post(payload: dict[str, Any]) -> int:
    from ..bus import events as ev
    from ..bus.store import Store
    from ..config import load
    from ..enforcement import current_role

    role = current_role()
    if not role:
        return 0

    cfg = load()
    store = Store(cfg.bus_path())
    if not store.exists():
        return 0
    run = store.latest_run()
    if not run:
        return 0

    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response") or {}
    session_id = payload.get("session_id")

    if tool == "Bash":
        ev.post(
            store, run["id"], "command_run", agent=role, session_id=session_id,
            payload={
                "argv": str(tool_input.get("command") or "")[:2000],
                "exit": response.get("exit_code") if isinstance(response, dict) else None,
                "cwd": payload.get("cwd"),
            },
            secret_names=cfg.secret_names(),
        )
        return 0

    if tool in WRITE_TOOLS:
        raw_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not raw_path:
            return 0

        path = Path(raw_path)
        try:
            rel = str(path.resolve().relative_to(cfg.workspace.resolve()))
        except (ValueError, OSError):
            rel = str(path)

        diff = _diff_for(path, cfg.workspace)
        event_id = ev.post(
            store, run["id"], "file_changed", agent=role, session_id=session_id,
            payload={
                "path": rel, "tool": tool,
                "diff_sha": hashlib.sha256(diff.encode()).hexdigest()[:16] if diff else None,
                "insertions": diff.count("\n+") if diff else None,
                "deletions": diff.count("\n-") if diff else None,
            },
            secret_names=cfg.secret_names(),
        )
        if diff:
            with store.write() as conn:
                conn.execute(
                    "INSERT INTO artifacts (event_id, kind, sha256, body) VALUES (?, ?, ?, ?)",
                    (event_id, "diff", hashlib.sha256(diff.encode()).hexdigest(), diff.encode()),
                )
    return 0


def dispatch(which: str) -> int:
    """Entry point for `metis hook pre|post`. Reads the payload from stdin."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed input must never block a tool

    try:
        return run_pre(payload) if which == "pre" else run_post(payload)
    except Exception as e:
        print(f"[metis] hook error ({which}, allowing): {e}", file=sys.stderr)
        return 0
