"""Stage 2: how does a target build and test itself?

Detectors are plugins, auto-loaded from `detectors/`. The core never names a
language -- supporting Go means dropping in `detectors/go.py` and nothing else.

Every command is *derived* from what is on disk (wrapper scripts, lockfiles,
declared scripts) and records the evidence that produced it. `npm test` is never
assumed; the `scripts` block is read.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import asdict, dataclass, field
from typing import Any

from .model import Target


@dataclass
class Stack:
    kind: str
    language: str
    package_manager: str | None = None
    runtime: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, list[str]] = field(default_factory=dict)
    test_paths: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    confidence: str = "high"
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_detectors() -> list[Any]:
    from . import detectors as pkg

    modules = [
        importlib.import_module(f"{pkg.__name__}.{info.name}")
        for info in pkgutil.iter_modules(pkg.__path__)
        if not info.name.startswith("_")
    ]
    return sorted(modules, key=lambda m: getattr(m, "PRIORITY", 50), reverse=True)


def detect(target: Target) -> Stack | None:
    """First detector that claims the target and can describe it wins.

    Priority breaks ties for a project carrying more than one manifest -- a
    Gradle build with a stray package.json for tooling, say.
    """
    for module in _load_detectors():
        if module.claims(target):
            stack = module.describe(target)
            if stack:
                return stack
    return None


def detect_all(targets: list[Target]) -> None:
    for target in targets:
        stack = detect(target)
        target.stack = stack.to_dict() if stack else {
            "kind": "unknown", "language": "unknown", "confidence": "none",
            "evidence": [f"no detector claimed manifests {target.manifests}"],
        }
