"""Run the discovery stages in order."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from . import capabilities, deployment, iac, keys, registry, report, testing
from .model import Target
from .resolve import ResolvedSource, default_work_dir, resolve
from .scan import scan


def new_run_id() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def discover(
    source: str, *, ref: str | None = None, environment: str = "dev",
    run_id: str | None = None, work_dir: Path | None = None,
) -> tuple[ResolvedSource, list[Target], dict[str, Any]]:
    """Resolve, scan, detect. Writes nothing into the target tree."""
    run_id = run_id or new_run_id()
    resolved = resolve(source, work_dir or default_work_dir(), ref=ref)

    targets = scan(resolved.root)
    registry.detect_all(targets)

    index = iac.build_index(resolved.root)
    capabilities.detect_all(targets, index)
    deployment.detect_all(targets, index)
    testing.detect_all(targets)
    keys.derive_all(targets, environment)

    return resolved, targets, report.build(targets, index, resolved, run_id, environment)
