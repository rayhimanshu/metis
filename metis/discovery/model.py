"""Shapes the discovery stages produce and enrich.

Keeping them here keeps the stages decoupled: `scan` knows nothing about
capabilities, `capabilities` knows nothing about deployment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class GitInfo:
    """Provenance, and the anchor to reset to if a run has to be abandoned.

    `head_sha` is captured before anything is modified. A target that is not in
    a repository still builds fine; it simply cannot be rolled back with git,
    which the report has to say out loud rather than leave implied.
    """

    repo_root: str | None = None
    remote: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    dirty: bool = False

    @property
    def is_repo(self) -> bool:
        return self.repo_root is not None


@dataclass
class Candidate:
    """A directory holding at least one build manifest.

    Raw scan output. Becomes a `Target` only after nesting is resolved -- a
    manifest inside another project's tree is usually a module, not a project.
    """

    path: str
    manifests: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


@dataclass
class Target:
    """One independently buildable project."""

    name: str
    path: str
    rel_path: str
    manifests: list[str] = field(default_factory=list)
    detector_hints: list[str] = field(default_factory=list)
    git: GitInfo = field(default_factory=GitInfo)

    # Sub-projects absorbed because the parent manifest declares them
    # (Maven <modules>, npm workspaces). They build as part of the parent.
    modules: list[str] = field(default_factory=list)

    # Set when a project sits inside another's tree but is NOT declared by it.
    # Kept as its own target, flagged so the report can explain why.
    nested_under: str | None = None

    # Filled in by later stages.
    stack: dict[str, Any] = field(default_factory=dict)
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    skipped_capabilities: list[dict[str, Any]] = field(default_factory=list)
    deployment: dict[str, Any] = field(default_factory=dict)
    tests: dict[str, Any] = field(default_factory=dict)
    lock_keys: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
