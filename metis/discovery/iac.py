"""Infrastructure-as-code index, built once per workspace.

IaC is *workspace*-scoped rather than target-scoped on purpose. Terraform
usually lives in its own directory beside the services, so attributing a bucket
to whichever service happens to use it is guesswork.

That drives the rule enforced in `capabilities.py`:

    IaC never creates a capability finding on its own. It only enriches a
    finding that source evidence already established.

What IaC *does* decide is how far a probe may go. A role granted only
`s3:GetObject` yields a read-only probe rather than a write that was never
going to succeed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# The provider/module cache mirrors upstream modules and would swamp the index
# with resources this workspace never declares.
PRUNE = {".terraform", ".git", "node_modules", "venv", ".venv", "__pycache__"}

IAC_SUFFIXES = (".tf", ".tf.json", ".tfvars")
CFN_NAMES = ("template.yaml", "template.yml", "cloudformation.yaml", "cloudformation.yml")
SERVERLESS_NAMES = ("serverless.yml", "serverless.yaml")

_TF_RESOURCE = re.compile(r'resource\s+"([a-zA-Z0-9_]+)"\s+"([a-zA-Z0-9_-]+)"')
_TF_MODULE = re.compile(r'module\s+"([a-zA-Z0-9_-]+)"')
_CFN_TYPE = re.compile(r"Type:\s*[\"']?(AWS::[A-Za-z0-9:]+)")
# Any "service:Action" literal -- catches IAM actions wherever they appear.
_IAM_ACTION = re.compile(r'"([a-z0-9-]+:[A-Z][A-Za-z0-9*]+)"')
_HEALTH_PATH = re.compile(r'health_check_path\s*=\s*"([^"]+)"')
_HEALTH_BLOCK = re.compile(r"health_check\s*=?\s*\{[^}]*?path\s*=\s*\"([^\"]+)\"", re.S)


@dataclass
class IacIndex:
    files: list[str] = field(default_factory=list)
    resources: dict[str, list[str]] = field(default_factory=dict)
    modules: list[str] = field(default_factory=list)
    iam_actions: dict[str, list[str]] = field(default_factory=dict)
    # Paths a load balancer polls. Deep dependency probes must never attach to
    # one of these -- see policy/bounds.py.
    lb_health_paths: dict[str, list[str]] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return bool(self.files)

    def has_resource(self, *types: str) -> list[str]:
        return [ev for t in types for ev in self.resources.get(t, [])]

    def granted(self, actions: list[str]) -> list[str]:
        """Evidence for any of `actions` being granted, wildcards honoured."""
        hits: list[str] = []
        for want in actions:
            for got, evidence in self.iam_actions.items():
                if got == want or (got.endswith("*") and want.startswith(got[:-1])):
                    hits += evidence
        return hits

    def to_dict(self) -> dict:
        return {
            "files": self.files,
            "resources": sorted(self.resources),
            "modules": sorted(set(self.modules)),
            "iam_actions": sorted(self.iam_actions),
            "lb_health_paths": self.lb_health_paths,
        }


def _record(bucket: dict[str, list[str]], key: str, evidence: str) -> None:
    bucket.setdefault(key, [])
    if evidence not in bucket[key]:
        bucket[key].append(evidence)


def build_index(root: Path, max_bytes: int = 1_000_000) -> IacIndex:
    index = IacIndex()

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in PRUNE]

        for name in filenames:
            is_tf = name.endswith(IAC_SUFFIXES)
            is_cfn = name in CFN_NAMES
            if not (is_tf or is_cfn or name in SERVERLESS_NAMES):
                continue

            path = Path(dirpath) / name
            try:
                if path.stat().st_size > max_bytes:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            rel = os.path.relpath(path, root)
            index.files.append(rel)

            def line_of(index_: int) -> int:
                return text.count("\n", 0, index_) + 1

            if is_tf:
                for m in _TF_RESOURCE.finditer(text):
                    _record(index.resources, m.group(1), f"{rel}:{line_of(m.start())}")
                index.modules += _TF_MODULE.findall(text)
            if is_cfn:
                for m in _CFN_TYPE.finditer(text):
                    _record(index.resources, m.group(1), f"{rel}:{line_of(m.start())}")

            for m in _IAM_ACTION.finditer(text):
                _record(index.iam_actions, m.group(1), f"{rel}:{line_of(m.start())}")

            for pattern in (_HEALTH_PATH, _HEALTH_BLOCK):
                for m in pattern.finditer(text):
                    _record(index.lb_health_paths, m.group(1), f"{rel}:{line_of(m.start())}")

    index.files.sort()
    return index
