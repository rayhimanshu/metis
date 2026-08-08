"""Stage 4: how does a target ship?

When nothing matches, the answer is `unknown` and the loop degrades to local
build and test. That distinction is load-bearing: silently not deploying is a
bug, while reporting "I could not tell how this deploys" is correct behaviour.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .iac import IacIndex
from .model import Target

# Most specific first. An ECS workflow also pushes a Docker image, so
# `container_registry` must never win ahead of `aws_ecs`.
DEPLOY_SIGNATURES: list[dict[str, Any]] = [
    {
        "kind": "aws_ecs",
        "uses": ["aws-actions/amazon-ecs-deploy-task-definition"],
        "run": ["aws ecs update-service", "aws ecs deploy"],
        "identifiers": {
            "cluster": ["ECS_CLUSTER", "CLUSTER", "CLUSTER_NAME"],
            "service": ["ECS_SERVICE", "SERVICE", "SERVICE_NAME"],
            "registry": ["ECR_REPOSITORY", "ECR_REPO"],
            "container": ["CONTAINER_NAME"],
            "region": ["AWS_REGION", "REGION"],
        },
    },
    {
        "kind": "kubernetes",
        "uses": ["azure/k8s-deploy", "Azure/k8s-deploy"],
        "run": ["kubectl apply", "kubectl rollout", "helm upgrade"],
        "identifiers": {"namespace": ["NAMESPACE", "K8S_NAMESPACE"],
                        "cluster": ["CLUSTER", "EKS_CLUSTER"]},
    },
    {
        "kind": "aws_lambda",
        "run": ["aws lambda update-function-code", "sam deploy"],
        "identifiers": {"function": ["FUNCTION_NAME", "LAMBDA_FUNCTION"],
                        "region": ["AWS_REGION"]},
    },
    {
        "kind": "serverless",
        "run": ["serverless deploy", "sls deploy"],
        "identifiers": {"stage": ["STAGE"], "region": ["AWS_REGION", "REGION"]},
    },
    {
        "kind": "firebase",
        "uses": ["FirebaseExtended/action-hosting-deploy"],
        "run": ["firebase deploy"],
        "identifiers": {"project": ["FIREBASE_PROJECT", "PROJECT_ID", "GCP_PROJECT"]},
    },
    {
        "kind": "vercel",
        "uses": ["amondnet/vercel-action"],
        "run": ["vercel deploy", "vercel --prod"],
        "identifiers": {},
    },
    {
        "kind": "container_registry",
        "uses": ["docker/build-push-action"],
        "run": ["docker push"],
        "identifiers": {"registry": ["ECR_REPOSITORY", "IMAGE_NAME", "REGISTRY"]},
    },
]

_ENV_REF = re.compile(r"\$\{\{\s*env\.([A-Za-z0-9_]+)\s*\}\}")
_EXPOSE = re.compile(r"^\s*EXPOSE\s+(\d+)", re.M | re.I)


def _walk_steps(workflow: dict) -> list[dict]:
    return [
        step
        for job in (workflow.get("jobs") or {}).values() if isinstance(job, dict)
        for step in (job.get("steps") or []) if isinstance(step, dict)
    ]


def _collect_env(workflow: dict) -> dict[str, str]:
    env = {str(k): str(v) for k, v in (workflow.get("env") or {}).items()}
    for job in (workflow.get("jobs") or {}).values():
        if isinstance(job, dict):
            for key, value in (job.get("env") or {}).items():
                env.setdefault(str(key), str(value))

    for key, value in list(env.items()):
        env[key] = _ENV_REF.sub(lambda m: env.get(m.group(1), m.group(0)), value)
    return env


def _trigger_branches(workflow: dict) -> list[str]:
    # YAML parses a bare `on:` key as the boolean True.
    on = workflow.get("on") or workflow.get(True) or {}
    if not isinstance(on, dict):
        return []
    push = on.get("push")
    return list(push.get("branches") or []) if isinstance(push, dict) else []


def _match_signature(steps: list[dict]) -> tuple[dict | None, str]:
    for sig in DEPLOY_SIGNATURES:
        for step in steps:
            uses, run = str(step.get("uses") or ""), str(step.get("run") or "")
            for token in sig.get("uses") or []:
                if token in uses:
                    return sig, f"uses: {uses}"
            for token in sig.get("run") or []:
                if token in run:
                    return sig, f"run: {token}"
    return None, ""


def detect_deployment(target: Target, iac: IacIndex) -> dict[str, Any]:
    root = Path(target.path)
    result: dict[str, Any] = {
        "kind": "unknown", "evidence": [], "identifiers": {},
        "trigger": {}, "containerized": False,
    }

    dockerfile = root / "Dockerfile"
    if dockerfile.is_file():
        result["containerized"] = True
        result["evidence"].append("Dockerfile present")
        try:
            ports = _EXPOSE.findall(dockerfile.read_text(encoding="utf-8", errors="replace"))
            if ports:
                result["exposed_ports"] = [int(p) for p in ports]
        except OSError:
            pass

    workflow_dir = root / ".github" / "workflows"
    for path in sorted(workflow_dir.glob("*.y*ml")) if workflow_dir.is_dir() else []:
        try:
            workflow = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(workflow, dict):
            continue

        steps = _walk_steps(workflow)
        sig, why = _match_signature(steps)
        if not sig:
            continue

        env = _collect_env(workflow)
        rel = os.path.relpath(path, root)
        result["kind"] = sig["kind"]
        result["workflow"] = rel
        result["evidence"].append(f"{rel}: {why}")

        for field, candidates in (sig.get("identifiers") or {}).items():
            for name in candidates:
                if name in env:
                    result["identifiers"][field] = env[name]
                    break

        branches = _trigger_branches(workflow)
        if branches:
            result["trigger"] = {"on": "push", "branches": branches}
            result["evidence"].append(f"{rel}: push to {', '.join(branches)}")

        # What CI itself builds with is a useful cross-check against what the
        # stack detector derived independently.
        ci_builds = [
            str(s.get("run", "")).strip() for s in steps
            if any(t in str(s.get("run", "")) for t in ("mvnw", "gradlew", "npm ", "pytest", "make "))
        ]
        if ci_builds:
            result["ci_build_commands"] = ci_builds[:4]
        break

    if result["kind"] == "unknown":
        result["evidence"].append(
            "no recognised deploy step in CI -- the loop will build and test locally only"
        )

    if iac.lb_health_paths:
        result["lb_health_paths"] = {p: e[:2] for p, e in iac.lb_health_paths.items()}

    return result


def detect_all(targets: list[Target], iac: IacIndex) -> None:
    for target in targets:
        target.deployment = detect_deployment(target, iac)
