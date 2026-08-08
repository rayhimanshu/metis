"""Stage 1: find the independently buildable projects inside a tree.

Three things make this harder than globbing for `pom.xml`:

1. Vendor and build directories dwarf real source, so they must be pruned
   *before* descending rather than filtered afterwards.
2. A manifest inside another project's tree is usually a module -- but only if
   the parent actually declares it.
3. Sibling repositories checked out under one folder are separate projects even
   though nothing at the top level connects them.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from .model import Candidate, GitInfo, Target

# Manifest -> the detector likely to claim it. Advisory; detectors confirm.
MANIFESTS: dict[str, str] = {
    "pom.xml": "java_maven",
    "build.gradle": "java_gradle",
    "build.gradle.kts": "java_gradle",
    "package.json": "node",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
}

PRUNE_DIRS: set[str] = {
    "node_modules", "venv", "env", "virtualenv", "site-packages",
    "target", "build", "dist", "out", "vendor", "__pycache__",
    "coverage", "htmlcov", "tmp", "temp",
}


def _is_pruned(name: str) -> bool:
    """Hidden directories are pruned wholesale.

    Deliberately blunt. Tool metadata, caches, and virtualenvs all hide behind a
    dot, and any of them can contain a manifest that would register as a bogus
    project. Files genuinely needed from dot directories -- `.github/workflows`,
    git metadata -- are read directly by later stages, never found by walking.
    """
    return name.startswith(".") or name in PRUNE_DIRS


def iter_candidates(root: Path) -> list[Candidate]:
    found: list[Candidate] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(d for d in dirnames if not _is_pruned(d))

        present = [f for f in filenames if f in MANIFESTS]
        if present:
            found.append(Candidate(
                path=str(Path(dirpath).resolve()),
                manifests=sorted(present),
                hints=sorted({MANIFESTS[f] for f in present}),
            ))

    return found


# ------------------------------------------------------------------- git


def _git(args: list[str], cwd: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, check=False)
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_info(path: str) -> GitInfo:
    top = _git(["rev-parse", "--show-toplevel"], path)
    if not top:
        return GitInfo()

    return GitInfo(
        repo_root=str(Path(top).resolve()),
        remote=_git(["remote", "get-url", "origin"], path),
        branch=_git(["rev-parse", "--abbrev-ref", "HEAD"], path),
        head_sha=_git(["rev-parse", "HEAD"], path),
        dirty=bool(_git(["status", "--porcelain"], path)),
    )


# ------------------------------------------------------ declared submodules


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def maven_modules(pom: Path) -> list[str]:
    try:
        root = ET.parse(pom).getroot()
    except (ET.ParseError, OSError):
        return []
    for child in root:
        if _strip_ns(child.tag) == "modules":
            return [m.text.strip() for m in child if m.text and m.text.strip()]
    return []


def node_workspaces(pkg: Path) -> list[str]:
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    workspaces = data.get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    return [w for w in workspaces if isinstance(w, str)] if isinstance(workspaces, list) else []


def python_members(pyproject: Path) -> list[str]:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return []
    members = data.get("tool", {}).get("uv", {}).get("workspace", {}).get("members")
    return [m for m in members if isinstance(m, str)] if isinstance(members, list) else []


def declared_children(candidate: Candidate) -> list[str]:
    base = Path(candidate.path)
    declared: list[str] = []
    if "pom.xml" in candidate.manifests:
        declared += maven_modules(base / "pom.xml")
    if "package.json" in candidate.manifests:
        declared += node_workspaces(base / "package.json")
    if "pyproject.toml" in candidate.manifests:
        declared += python_members(base / "pyproject.toml")
    return declared


def _claims(parent: Candidate, child_path: str, patterns: list[str]) -> bool:
    rel = os.path.relpath(child_path, parent.path)
    return any(
        fnmatch.fnmatch(rel, p.rstrip("/")) or rel == p.rstrip("/") for p in patterns
    )


# -------------------------------------------------------------- assembly


def _disambiguate(targets: list[Target], root: Path) -> None:
    by_name: dict[str, list[Target]] = {}
    for t in targets:
        by_name.setdefault(t.name, []).append(t)

    for name, group in by_name.items():
        if len(group) > 1:
            for t in group:
                rel = os.path.relpath(t.path, root)
                t.name = rel.replace(os.sep, "-").strip("-.") or name


def scan(root: Path) -> list[Target]:
    candidates = iter_candidates(root)
    by_path = {c.path: c for c in candidates}
    child_patterns = {c.path: declared_children(c) for c in candidates}
    git_cache: dict[str, GitInfo] = {}

    def info(path: str) -> GitInfo:
        if path not in git_cache:
            git_cache[path] = git_info(path)
        return git_cache[path]

    absorbed: dict[str, str] = {}   # child -> parent that declares it
    nested: dict[str, str] = {}     # child -> enclosing project that does not

    for path in by_path:
        parents = [p for p in by_path if p != path and path.startswith(p + os.sep)]
        if not parents:
            continue
        parent = max(parents, key=len)

        # A different repository is always its own project, however the
        # directories nest. This is what keeps sibling service checkouts apart.
        if info(path).repo_root != info(parent).repo_root:
            continue

        if _claims(by_path[parent], path, child_patterns[parent]):
            absorbed[path] = parent
        else:
            nested[path] = parent

    targets: list[Target] = []
    for path, candidate in sorted(by_path.items()):
        if path in absorbed:
            continue
        targets.append(Target(
            name=Path(path).name,
            path=path,
            rel_path=os.path.relpath(path, root),
            manifests=candidate.manifests,
            detector_hints=candidate.hints,
            git=info(path),
            modules=sorted(os.path.relpath(c, path) for c, p in absorbed.items() if p == path),
            nested_under=os.path.relpath(nested[path], root) if path in nested else None,
        ))

    _disambiguate(targets, root)
    return targets
