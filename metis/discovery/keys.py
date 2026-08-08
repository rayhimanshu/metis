"""Derive lock keys from what discovery already found.

The contention graph is inferred, not maintained. Every key here comes from a
fact another stage established -- a repository root, a cluster name lifted from
CI, a database schema in config. Nobody keeps a lock registry in sync with
reality, because there is no registry.

This is the same idea as bounding probe depth by discovered IAM: discovery
constrains behaviour rather than merely describing it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .model import Target

# jdbc:postgresql://host:5432/dbname  ->  dbname
_JDBC_DB = re.compile(r"jdbc:[a-z0-9]+://[^/]+/([A-Za-z0-9_-]+)")
_URL_DB = re.compile(r"(?:postgres|postgresql|mysql|mongodb)(?:\+\w+)?://[^/]+/([A-Za-z0-9_-]+)")

SCHEMA_CONFIG_HINTS = ("default_schema", "default-schema", "liquibase.schema",
                       "flyway.schemas", "hibernate.default_schema")


def _slug(value: str) -> str:
    """Lock keys are compared as strings, so they must be stable and safe."""
    return re.sub(r"[^A-Za-z0-9._@/-]+", "-", value).strip("-")


def worktree_key(target: Target) -> str | None:
    """Exclusive access to a checkout. Two writers in one tree corrupt a diff."""
    if not target.git.is_repo:
        return None
    name = Path(target.git.repo_root).name
    return f"worktree:{_slug(name)}@{_slug(target.git.branch or 'HEAD')}"


def branch_key(target: Target) -> str | None:
    """Exclusive right to push. Separate from worktree: building and pushing
    are different privileges, and only one of them is outward-facing."""
    if not target.git.is_repo:
        return None
    name = Path(target.git.repo_root).name
    return f"branch:{_slug(name)}@{_slug(target.git.branch or 'HEAD')}"


def cluster_key(target: Target) -> str | None:
    identifiers = (target.deployment or {}).get("identifiers") or {}
    for field in ("cluster", "namespace", "project", "stage"):
        if identifiers.get(field):
            return f"cluster:{_slug(identifiers[field])}"
    return None


def _database_names(target: Target) -> list[str]:
    names: list[str] = []
    for finding in target.capabilities:
        if finding.get("probe") != "relational":
            continue
        for key, value in (finding.get("resources") or {}).items():
            if not isinstance(value, str):
                continue
            match = _JDBC_DB.search(value) or _URL_DB.search(value)
            if match:
                names.append(match.group(1))
            elif any(hint in key.lower() for hint in SCHEMA_CONFIG_HINTS):
                names.append(value)
    return names


def schema_keys(target: Target) -> list[str]:
    """Migrations serialise on the database, not on the service.

    Two services deploying at once is fine; two services migrating overlapping
    schemas is not -- one blocks on the changelog lock or times out. The key is
    the database, which is why it cannot be derived from the service name.
    """
    keys: list[str] = []
    for name in _database_names(target):
        key = f"schema:{_slug(name)}"
        if key not in keys:
            keys.append(key)
    return keys


def env_key(environment: str) -> str:
    """Capacity 1 per environment.

    This is why a Tester is not "one agent": add a staging environment and
    `env:dev` and `env:staging` are different keys, giving two concurrent test
    runs with no code change.
    """
    return f"env:{_slug(environment)}"


def derive(target: Target, environment: str) -> dict[str, list[str]]:
    """Lock keys grouped by the action that needs them."""
    worktree = worktree_key(target)
    branch = branch_key(target)
    cluster = cluster_key(target)
    schemas = schema_keys(target)

    build = [worktree] if worktree else []
    deploy = ([branch] if branch else []) + ([cluster] if cluster else []) + schemas

    return {
        "edit": build,
        "build": build,
        # Sorted at derivation so every caller acquires in the same order.
        # Two multi-key actions taking locks in different orders deadlock.
        "deploy": sorted(deploy),
        "test": [env_key(environment)],
    }


def derive_all(targets: list[Target], environment: str) -> None:
    for target in targets:
        target.lock_keys = derive(target, environment)


def contention(targets: list[Target]) -> dict[str, list[str]]:
    """Which targets compete for each key -- the inferred contention graph."""
    graph: dict[str, list[str]] = {}
    for target in targets:
        for keys in (target.lock_keys or {}).values():
            for key in keys:
                graph.setdefault(key, [])
                if target.name not in graph[key]:
                    graph[key].append(target.name)
    return {k: sorted(v) for k, v in sorted(graph.items())}
