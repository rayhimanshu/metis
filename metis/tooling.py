"""Command-line tools a run needs, and whether installing one is justified.

An agent that hits a missing `aws` mid-deploy has two bad options: stop, which
wastes the run, or install something, which is a larger grant than it appears.
"Install whatever you need" makes an agent's guess about its own requirements
sufficient authority to put software on your machine.

So the same rule applies here as everywhere else in Metis: **evidence decides.**
Discovery already knows the platform a target deploys to. A CLI that platform
requires is justified; one nothing in the workspace uses is not, and asking for
it is a signal worth reading rather than a request worth granting.

Two further limits, both deliberate:

* **Only a package manager already on the machine.** Never `curl | sh` -- that
  is in the universal deny list precisely because it is how a missing tool turns
  into an arbitrary script running as you.
* **Checked before the run, not during.** A deploy that dies halfway because a
  binary is absent has already taken a lease, pushed an image, or half-applied a
  change. Finding out at `metis doctor` costs nothing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    binary: str
    why: str
    brew: str | None = None
    apt: str | None = None
    note: str | None = None

    @property
    def present(self) -> bool:
        return shutil.which(self.binary) is not None


# What each deployment platform needs to build, deploy, and read its own logs.
# Keyed on the `kind` discovery reports.
REQUIRED: dict[str, list[Tool]] = {
    "aws_ecs": [
        Tool("aws", "deploy to ECS and read CloudWatch logs", brew="awscli",
             apt="awscli"),
        Tool("docker", "build and push the image", brew="docker",
             note="Docker Desktop, or colima"),
    ],
    "aws_lambda": [
        Tool("aws", "deploy the function and read its logs", brew="awscli",
             apt="awscli"),
    ],
    "kubernetes": [
        Tool("kubectl", "roll out and read pod logs", brew="kubernetes-cli",
             apt="kubectl"),
    ],
    "container_registry": [
        Tool("docker", "build and push the image", brew="docker",
             note="Docker Desktop, or colima"),
    ],
    "firebase": [
        Tool("firebase", "deploy", note="npm install -g firebase-tools"),
    ],
    "vercel": [
        Tool("vercel", "deploy", note="npm install -g vercel"),
    ],
    "serverless": [
        Tool("serverless", "deploy", note="npm install -g serverless"),
    ],
}

# Cloud CLIs, keyed by the provider name used in `metis setup`. Needed for logs
# and identity even when the deploy itself goes through CI.
PROVIDER_TOOLS: dict[str, Tool] = {
    "aws": Tool("aws", "read logs and confirm which identity is deploying",
                brew="awscli", apt="awscli"),
    "gcp": Tool("gcloud", "read logs and confirm the active project",
                brew="google-cloud-sdk"),
    "azure": Tool("az", "read logs and confirm the subscription",
                  brew="azure-cli"),
    "alicloud": Tool("aliyun", "read logs and confirm the account",
                     brew="aliyun-cli"),
}


def needed(kinds: list[str], providers: list[str] | None = None) -> list[Tool]:
    """Tools justified by what this workspace actually deploys to."""
    seen: dict[str, Tool] = {}
    for kind in kinds:
        for tool in REQUIRED.get(kind, []):
            seen.setdefault(tool.binary, tool)
    for provider in providers or []:
        tool = PROVIDER_TOOLS.get(provider)
        if tool:
            seen.setdefault(tool.binary, tool)
    return list(seen.values())


def missing(kinds: list[str], providers: list[str] | None = None) -> list[Tool]:
    return [t for t in needed(kinds, providers) if not t.present]


def install_hint(tool: Tool) -> str:
    """How to get it, using a package manager rather than a piped script."""
    if tool.brew and shutil.which("brew"):
        return f"brew install {tool.brew}"
    if tool.apt and shutil.which("apt-get"):
        return f"sudo apt-get install -y {tool.apt}"
    if tool.note:
        return tool.note
    return f"install {tool.binary}"


def report(kinds: list[str], providers: list[str] | None = None) -> list[str]:
    """Lines for `metis doctor`. Empty when nothing is missing."""
    gaps = missing(kinds, providers)
    if not gaps:
        return []

    lines = []
    for tool in gaps:
        lines.append(f"{tool.binary} is missing -- needed to {tool.why}")
        lines.append(f"    {install_hint(tool)}")
    return lines
