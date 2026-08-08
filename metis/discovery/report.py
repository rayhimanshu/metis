"""Stage 6: write the evidence report.

Discovery auto-proceeds on the actionable set without asking, so the report has
to answer "why did it think that?" for every decision -- including the ones it
declined to make, which live under `skipped_capabilities` with the reason
attached.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import yaml

from .iac import IacIndex
from .keys import contention
from .model import Target


def _target_block(t: Target) -> dict[str, Any]:
    stack = t.stack or {}
    return {
        "name": t.name,
        "path": t.rel_path,
        "stack": {
            "kind": stack.get("kind"),
            "language": stack.get("language"),
            "package_manager": stack.get("package_manager"),
            "runtime": stack.get("runtime"),
            "confidence": stack.get("confidence"),
            "evidence": stack.get("evidence"),
        },
        "commands": {k: " ".join(v) for k, v in (stack.get("commands") or {}).items()},
        "git": {
            "repo": t.git.is_repo,
            "remote": t.git.remote,
            "branch": t.git.branch,
            "rollback_anchor": t.git.head_sha,
            "dirty": t.git.dirty,
            "note": None if t.git.is_repo else "not a git repo -- cannot be rolled back with git",
        },
        "lock_keys": t.lock_keys or None,
        "modules": t.modules or None,
        "nested_under": t.nested_under,
        "capabilities": t.capabilities,
        "deployment": t.deployment,
        "tests": t.tests,
    }


def build(targets: list[Target], iac: IacIndex, source: Any, run_id: str,
          environment: str) -> dict[str, Any]:
    deployable = [t.name for t in targets if (t.deployment or {}).get("kind") != "unknown"]
    skipped = [
        {"target": t.name, "capability": f["capability"], "confidence": f["confidence"],
         "reason": f["reason"], "evidence": f["evidence"]}
        for t in targets for f in t.skipped_capabilities
    ]

    return {
        "run_id": run_id,
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "source": {"kind": source.kind, "root": str(source.root),
                   "url": source.url, "ref": source.ref},
        "environment": environment,
        "summary": {
            "targets": len(targets),
            "languages": sorted({(t.stack or {}).get("language") for t in targets} - {None}),
            "actionable_capabilities": sum(len(t.capabilities) for t in targets),
            "skipped_capabilities": len(skipped),
            "deployable_targets": deployable,
            "local_only_targets": [t.name for t in targets if t.name not in deployable],
        },
        "workspace": {
            "iac": iac.to_dict(),
            # Restated at the top level because it is a hard constraint on what
            # may be generated, not a detail buried in one target's block.
            "load_balancer_polled_paths": sorted(iac.lb_health_paths),
            "lock_contention": contention(targets),
        },
        "targets": [_target_block(t) for t in targets],
        "skipped_capabilities": skipped,
    }


def write(report: dict[str, Any], runs_dir: Path, run_id: str) -> Path:
    out_dir = runs_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "discovered.yaml"

    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(report, fh, sort_keys=False, width=100, allow_unicode=True)

    latest = runs_dir / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(out_dir.name)
    return path


def summarize(report: dict[str, Any]) -> str:
    lines: list[str] = []
    s, src = report["summary"], report["source"]

    lines.append(f"source      {src['root']}  ({src['kind']})")
    lines.append(f"targets     {s['targets']}  languages: {', '.join(s['languages']) or 'none'}")
    lines.append(
        f"deployable  {', '.join(s['deployable_targets']) or 'none'}"
        + (f"   local-only: {', '.join(s['local_only_targets'])}" if s["local_only_targets"] else "")
    )

    polled = report["workspace"]["load_balancer_polled_paths"]
    if polled:
        lines.append(f"LB polls    {', '.join(polled)}   (deep probes must not attach here)")
    lines.append("")

    for t in report["targets"]:
        deploy = t["deployment"].get("kind", "unknown")
        identifiers = t["deployment"].get("identifiers") or {}
        lines.append(f"  {t['name']}  [{t['stack']['kind']}]  deploy={deploy}"
                     + (f" {identifiers}" if identifiers else ""))

        for c in t["capabilities"]:
            perms = c.get("permissions")
            verbs = " perms=" + ",".join(k for k, v in perms.items() if v) if perms else ""
            lines.append(f"      + {c['capability']:15} {c['probe'] or '':15}{verbs}")
        if not t["capabilities"]:
            lines.append("      + (no actionable capabilities)")

        for action, keys in (t["lock_keys"] or {}).items():
            if keys and action in ("build", "deploy"):
                lines.append(f"      ~ {action:6} needs {', '.join(keys)}")

        exercises = t["tests"].get("exercises") or {}
        if exercises:
            lines.append(f"      ~ tests exercise: {', '.join(exercises)}")
        if t["tests"].get("covered_by"):
            lines.append(f"      ~ covered by:     {', '.join(t['tests']['covered_by'])}")

    if report["skipped_capabilities"]:
        lines.append("")
        lines.append("  skipped (reported, not acted on):")
        for item in report["skipped_capabilities"]:
            lines.append(f"      - {item['target']}/{item['capability']} "
                         f"[{item['confidence']}] {item['reason']}")

    return "\n".join(lines)
