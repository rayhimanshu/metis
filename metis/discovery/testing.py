"""Stage 5: what tests exist, and which target does each actually exercise?

Ownership by path is the easy half. The half that matters is cross-target
mapping: an integration suite often lives in its own project and drives services
that ship from entirely different repositories. When such a test fails, the
repair has to be aimed at the service that broke, not the suite that noticed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .model import Target

TEST_GLOBS = ("test_*.py", "*_test.py", "*.test.js", "*.test.ts",
              "*.spec.ts", "*.spec.js", "*Test.java", "*Tests.java")

PRUNE_DIRS = {"node_modules", "venv", ".venv", "target", "build", "dist",
              "__pycache__", ".git"}


def _name_variants(name: str) -> set[str]:
    base = name.lower()
    stem = re.sub(r"^(app|svc|service)[-_]", "", base)
    variants = {base, stem}
    for v in list(variants):
        variants |= {v.replace("-", "_"), v.replace("_", "-"),
                     v.replace("-", "").replace("_", "")}
    return {v for v in variants if len(v) > 3}


def _iter_test_files(root: Path, test_paths: list[str]) -> list[Path]:
    search_roots = [root / p for p in test_paths] if test_paths else [root]
    files: list[Path] = []

    for base in search_roots:
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, topdown=True):
            dirnames[:] = [d for d in dirnames
                           if d not in PRUNE_DIRS and not d.startswith(".")]
            files += [Path(dirpath) / n for n in filenames
                      if any(Path(n).match(g) for g in TEST_GLOBS)]
    return sorted(files)


def detect_tests(target: Target, all_targets: list[Target],
                 max_bytes: int = 512_000) -> dict[str, Any]:
    root = Path(target.path)
    stack = target.stack or {}
    test_files = _iter_test_files(root, stack.get("test_paths") or [])

    info: dict[str, Any] = {
        "framework": None,
        "command": stack.get("commands", {}).get("test"),
        "paths": stack.get("test_paths") or [],
        "file_count": len(test_files),
        "files": [os.path.relpath(f, root) for f in test_files][:50],
        "exercises": {},
        "evidence": [],
    }

    if not test_files:
        info["evidence"].append("no test files matched the known naming conventions")
        return info

    info["framework"] = {
        "python": "pytest" if "pytest" in " ".join(info["command"] or []) else "unittest",
        "java": "junit",
        "javascript": "jest/vitest",
    }.get(stack.get("language"))

    vocab = {other.name: _name_variants(other.name)
             for other in all_targets if other.name != target.name}
    if not vocab:
        return info

    # Config shared by a suite (service base URLs and the like) attributes to
    # every test in it -- but only for a project that *is* a suite.
    #
    # A deployable service also names other services in its config, for routing
    # or outbound calls. Reading that as test coverage would make an API gateway
    # look like it tests everything it proxies. A dedicated suite is one nothing
    # knows how to deploy.
    is_test_suite = (target.deployment or {}).get("kind", "unknown") == "unknown"
    shared_text = ""
    if is_test_suite:
        for rel in stack.get("config_files") or []:
            path = root / rel
            if path.is_file():
                try:
                    shared_text += path.read_text(encoding="utf-8", errors="replace").lower()
                except OSError:
                    pass
    else:
        info["evidence"].append(
            "deployable target -- config references to other services treated as wiring, "
            "not test coverage"
        )

    exercises: dict[str, list[str]] = {}
    for path in test_files:
        try:
            if path.stat().st_size > max_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue

        rel = os.path.relpath(path, root)
        haystack = f"{rel.lower()} {text}"
        for other_name, variants in vocab.items():
            if any(v in haystack for v in variants):
                exercises.setdefault(other_name, []).append(rel)

    for other_name, variants in vocab.items():
        if other_name not in exercises and any(v in shared_text for v in variants):
            exercises[other_name] = ["<all tests, via shared config>"]

    info["exercises"] = {k: sorted(v)[:20] for k, v in sorted(exercises.items())}
    if exercises:
        info["evidence"].append(
            f"suite references {len(exercises)} other target(s) -- failures route to them, "
            f"not to {target.name}"
        )
    return info


def detect_all(targets: list[Target]) -> None:
    for target in targets:
        target.tests = detect_tests(target, targets)

    # Inverse view: which external suites cover each target. This is what a
    # Tester runs after a deploy.
    covered: dict[str, list[str]] = {}
    for target in targets:
        for other_name in target.tests.get("exercises") or {}:
            covered.setdefault(other_name, []).append(target.name)

    for target in targets:
        target.tests["covered_by"] = sorted(covered.get(target.name, []))
