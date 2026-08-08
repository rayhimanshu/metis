"""What each role may touch, and what it may run.

Every rule here exists because a prompt cannot carry it. A prompt is advisory:
a model under pressure to make a build green will find the cheapest path, and
the cheapest path is often the one that quietly removes the safety net. These
are refusals, evaluated by a hook before the tool runs.

Two rules carry most of the weight:

* **DevOps cannot edit source.** The tempting fix for a failing deploy is a
  one-line source change, which makes the same actor both the cause of a change
  and the judge of whether it deployed cleanly.

* **Nobody may modify the test that caught them.** Weakening an assertion turns
  a build green in seconds and destroys the signal permanently. This one is
  derived from the ledger rather than configured, so it cannot be forgotten:
  the denied set is whatever tests are named in fault slices posted since the
  last `code_ready`.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

SOURCE_SUFFIXES = (".java", ".kt", ".py", ".js", ".ts", ".jsx", ".tsx", ".go",
                   ".rb", ".cs", ".rs", ".scala", ".groovy", ".php")

TEST_PATTERNS = (
    "**/test/**", "**/tests/**", "**/__tests__/**", "**/spec/**",
    "**/src/test/**", "test_*.py", "*_test.py", "*.test.*", "*.spec.*",
    "*Test.java", "*Tests.java", "conftest.py",
)

# Commands that change something outside this machine. Denied for every role
# except the one whose job it is.
OUTWARD_COMMANDS = [
    (r"\bgit\s+push\b", "pushes to a remote"),
    (r"\bgit\s+tag\s+.*-\w*f", "force-tags a remote-visible ref"),
    (r"\baws\s+ecs\b", "changes an ECS service"),
    (r"\baws\s+lambda\s+update", "updates a Lambda"),
    (r"\baws\s+s3\s+(rm|sync)\b", "mutates S3 outside a probe"),
    (r"\bkubectl\s+(apply|delete|rollout|scale)\b", "changes a Kubernetes cluster"),
    (r"\bhelm\s+(upgrade|install|uninstall)\b", "changes a Helm release"),
    (r"\bterraform\s+(apply|destroy)\b", "changes infrastructure"),
    (r"\bdocker\s+push\b", "publishes an image"),
    (r"\b(firebase|vercel|netlify)\s+deploy\b", "deploys a site"),
    (r"\b(serverless|sls)\s+deploy\b", "deploys a stack"),
    (r"\bgh\s+(pr\s+merge|release\s+create)\b", "merges or releases"),
]

# Denied for everyone. Nothing in this system needs them, and an agent reaching
# for one is a signal in itself.
UNIVERSAL_DENY = [
    (r"\bgit\s+push\s+.*--force\b|\bgit\s+push\s+.*-f\b", "force-pushes"),
    (r"\bgit\s+reset\s+--hard\s+.*origin", "discards remote history"),
    (r"\brm\s+-rf\s+[/~]", "recursive delete of a root or home path"),
    (r":\(\)\s*\{.*\|.*&\s*\}\s*;", "fork bomb"),
    (r"\bcurl\b[^|]*\|\s*(ba)?sh", "pipes a download into a shell"),
]

_OUTWARD = [(re.compile(p), why) for p, why in OUTWARD_COMMANDS]
_UNIVERSAL = [(re.compile(p), why) for p, why in UNIVERSAL_DENY]


@dataclass
class RolePolicy:
    name: str
    write_allow: list[str] = field(default_factory=list)
    write_deny: list[str] = field(default_factory=list)
    may_run_outward: bool = False
    summary: str = ""


POLICIES: dict[str, RolePolicy] = {
    "swe": RolePolicy(
        name="swe",
        write_allow=["**"],
        write_deny=[],
        may_run_outward=False,
        summary="edits source and tests; cannot deploy, push, or migrate",
    ),
    "devops": RolePolicy(
        name="devops",
        # Nothing. Building writes into ignored output directories, which are
        # not tool-writes; anything DevOps would Edit is source by definition.
        write_allow=[],
        write_deny=["**"],
        may_run_outward=True,
        summary="builds and deploys; cannot edit source",
    ),
    "tester": RolePolicy(
        name="tester",
        write_allow=list(TEST_PATTERNS),
        write_deny=[],
        may_run_outward=False,
        summary="runs suites and authors tests; cannot edit production source or deploy",
    ),
}


def current_role() -> str | None:
    return os.environ.get("METIS_ROLE")


def policy_for(role: str | None) -> RolePolicy | None:
    return POLICIES.get(role) if role else None


def _matches(rel: str, patterns: list[str]) -> bool:
    posix = PurePosixPath(rel).as_posix()
    return any(
        fnmatch.fnmatch(posix, pattern) or fnmatch.fnmatch(PurePosixPath(posix).name, pattern)
        for pattern in patterns
    )


def is_test_path(rel: str) -> bool:
    return _matches(rel, list(TEST_PATTERNS))


def is_source_path(rel: str) -> bool:
    return PurePosixPath(rel).suffix in SOURCE_SUFFIXES


def check_write(
    role: str | None, path: str, workspace: Path, denied_tests: set[str] | None = None
) -> tuple[bool, str]:
    """May this role write this path?"""
    policy = policy_for(role)
    if policy is None:
        return True, ""  # unroled session: the hook is not in force

    target = Path(path).expanduser()
    if not target.is_absolute():
        target = (workspace / target)
    try:
        resolved = target.resolve()
        rel = str(resolved.relative_to(workspace.resolve()))
    except (ValueError, OSError):
        return False, (
            f"{role} may only write inside the workspace ({workspace}); "
            f"'{path}' is outside it"
        )

    # The test that caught you is off limits, whatever your role allows.
    for denied in denied_tests or set():
        if rel == denied or PurePosixPath(rel).name == PurePosixPath(denied).name:
            return False, (
                f"'{rel}' is the test that reported the current failure. Repairing code "
                "and changing a test are separate, visible acts -- fix the cause, or "
                "raise the test as its own change."
            )

    if policy.write_deny and _matches(rel, policy.write_deny):
        return False, f"{role} cannot write source ({policy.summary})"

    if policy.write_allow and not _matches(rel, policy.write_allow):
        return False, (
            f"{role} may only write test paths ({policy.summary}); '{rel}' is not one"
        )

    return True, ""


_PUSH = re.compile(r"\bgit\s+push\b")


def check_command(role: str | None, command: str) -> tuple[bool, str]:
    """May this role run this shell command?"""
    for pattern, why in _UNIVERSAL:
        if pattern.search(command):
            return False, f"refused: command {why}"

    policy = policy_for(role)
    if policy is None or policy.may_run_outward:
        return True, ""

    for pattern, why in _OUTWARD:
        if pattern.search(command):
            return False, (
                f"{role} cannot run a command that {why} ({policy.summary}). "
                "Post an event and let the responsible agent act."
            )
    return True, ""


def check_changeset_push(store, run_id: str, command: str, cwd: str) -> tuple[bool, str]:
    """Refuse to push one repository of a change set while a sibling is unbuilt.

    Git cannot commit across repositories, so a cross-repo change is always N
    pushes. Letting them go independently is how repo A lands, repo B fails, and
    production ends up with two services disagreeing about a contract.

    Checked rather than asked for, because it is precisely the rule an agent
    under pressure to ship half a change would reason its way around.
    """
    if not _PUSH.search(command):
        return True, ""

    from .bus import changesets

    target = _repo_name(cwd)
    if not target:
        return True, ""

    return changesets.may_push(store, run_id, target)


def _repo_name(cwd: str) -> str | None:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(proc.stdout.strip()).name if proc.returncode == 0 and proc.stdout.strip() else None


def denied_tests_from_bus(store, run_id: str) -> set[str]:
    """Tests named in fault slices posted since the last `code_ready`.

    Derived rather than configured, so it cannot be forgotten or omitted. A
    fresh `code_ready` clears the set, because the failure it describes has been
    superseded.
    """
    from .bus import events as ev

    rows = ev.read_since(store, run_id, 0,
                         types=["code_ready", "build_failed", "test_failed"], limit=1000)
    denied: set[str] = set()
    for row in rows:
        if row["type"] == "code_ready":
            denied.clear()
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("test_file", "test_files"):
            value = payload.get(key)
            if isinstance(value, str):
                denied.add(value)
            elif isinstance(value, list):
                denied.update(v for v in value if isinstance(v, str))
    return denied
