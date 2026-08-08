"""What a health probe may do, decided from discovered facts.

This is engineering judgement expressed as data plus two rules. It exists so an
agent asked for a health endpoint does not have to rediscover -- or fail to
rediscover -- the reasoning each time.

**Rule 1: never attach deep probes to a path a load balancer polls.** If a
dependency probe fed the endpoint that decides whether a container lives, a
thirty-second blip in that dependency would mark every task unhealthy, the
platform would drain and restart them all, and a minor hiccup would escalate
into a self-inflicted outage. The polled paths come from IaC, so this is checked
rather than remembered.

**Rule 2: a probe leg is generated only where the permission is granted.**
Unknown permissions degrade to read-only. Discovery constrains behaviour rather
than merely describing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..discovery.model import Target

FAMILIES_PATH = Path(__file__).with_name("families.yaml")

PREFERRED_PATHS = ("/health-check", "/health-check/deep", "/internal/health-check")


def load_families(path: Path = FAMILIES_PATH) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class ProbeSpec:
    capability: str
    label: str
    family: str
    depth: str
    legs: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    config_keys: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ProbePlan:
    target: str
    endpoint: str
    endpoint_rationale: str
    probes: list[ProbeSpec] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "endpoint": self.endpoint,
            "endpoint_rationale": self.endpoint_rationale,
            "probes": [p.__dict__ for p in self.probes],
            "excluded": self.excluded,
        }


def choose_endpoint(lb_paths: set[str]) -> tuple[str, str]:
    for candidate in PREFERRED_PATHS:
        if candidate not in lb_paths:
            polled = ", ".join(sorted(lb_paths)) or "none"
            return candidate, (
                f"{candidate} is not polled by a load balancer (IaC shows: {polled}), "
                "so a failing probe cannot cause the platform to restart healthy instances"
            )
    raise ValueError(f"every candidate endpoint is load-balancer polled: {sorted(lb_paths)}")


def _legs_for(family: dict[str, Any],
              permissions: dict[str, bool] | None) -> tuple[list[str], list[str]]:
    legs: list[str] = []
    notes: list[str] = []

    for leg in family.get("legs") or []:
        verb, name = leg.get("verb"), leg["name"]

        if permissions is None:
            # No IaC evidence. Read-only is the safe assumption -- a write we
            # cannot justify would fail loudly the first time it ran.
            if verb == "read":
                legs.append(name)
            else:
                notes.append(f"leg '{name}' skipped: no IaC permission evidence, "
                             "defaulting to read-only")
            continue

        if permissions.get(verb):
            legs.append(name)
        elif leg.get("required"):
            notes.append(f"leg '{name}' skipped: '{verb}' not granted by IaC")
        else:
            notes.append(f"leg '{name}' skipped: optional and '{verb}' not granted")

    return legs, notes


def _config_keys(finding: dict[str, Any]) -> list[str]:
    """Every config key the finding matched, populated or not.

    A config-only probe asks "is any of these set?", and answering from one
    arbitrarily chosen key reports NOT_CONFIGURED for working integrations.
    """
    import re

    pattern = re.compile(r'"([^"]+)"\s*$')
    keys: list[str] = []
    for entry in (finding.get("evidence") or {}).get("configured", []):
        m = pattern.search(entry)
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    return sorted(keys)


def plan(target: Target, lb_paths: set[str],
         families: dict[str, Any] | None = None) -> ProbePlan:
    fams = families if families is not None else load_families()
    endpoint, rationale = choose_endpoint(lb_paths)
    result = ProbePlan(target=target.name, endpoint=endpoint, endpoint_rationale=rationale)

    for finding in target.capabilities:
        family_name = finding.get("probe")
        family = fams.get(family_name or "")
        if not family:
            result.excluded.append({
                "capability": finding["capability"],
                "reason": f"no probe family '{family_name}'",
            })
            continue

        depth = family.get("depth", "config_only")
        legs: list[str] = []
        notes: list[str] = []

        if depth == "config_only":
            notes.append(" ".join(
                (family.get("rationale") or "reported as configuration state only").split()
            ))
        else:
            legs, notes = _legs_for(family, finding.get("permissions"))
            if not legs:
                result.excluded.append({
                    "capability": finding["capability"],
                    "reason": "no probe leg is permitted by discovered permissions",
                })
                continue
            if depth == "deep" and "write" not in legs:
                depth = "light"
                notes.append("downgraded from deep to light: write permission not granted")

        result.probes.append(ProbeSpec(
            capability=finding["capability"],
            label=finding.get("label", finding["capability"]),
            family=family_name,
            depth=depth,
            legs=legs,
            resources=finding.get("resources") or {},
            config_keys=_config_keys(finding),
            notes=notes,
        ))

    for finding in target.skipped_capabilities:
        result.excluded.append({
            "capability": finding["capability"],
            "reason": f"not actionable ({finding['confidence']}): {finding['reason']}",
        })

    return result


def render(plan_: ProbePlan) -> str:
    """Guidance an agent can act on, with the reasoning attached."""
    out = [f"# Probe plan — {plan_.target}", ""]
    out.append(f"**Endpoint: `{plan_.endpoint}`**")
    out.append(f"\n{plan_.endpoint_rationale}.")
    out.append("\nDo not register these as framework health indicators; they must not "
               "contribute to the endpoint the load balancer polls.\n")

    if not plan_.probes:
        out.append("No probes: this target has no actionable capabilities.")
    for probe in plan_.probes:
        out.append(f"## {probe.capability} — {probe.label}")
        out.append(f"\n- family: `{probe.family}` · depth: **{probe.depth}**")
        if probe.legs:
            out.append(f"- legs: {', '.join(probe.legs)}")
        if probe.resources:
            out.append(f"- resources: {probe.resources}")
        if probe.config_keys:
            out.append(f"- config keys to check: {', '.join(probe.config_keys)}")
        for note in probe.notes:
            out.append(f"- note: {note}")
        out.append("")

    if plan_.excluded:
        out.append("## Not probed\n")
        for item in plan_.excluded:
            out.append(f"- **{item['capability']}** — {item['reason']}")

    return "\n".join(out)
