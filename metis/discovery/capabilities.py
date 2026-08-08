"""Stage 3: what does this target talk to, and how sure are we?

Four independent signals are collected and weighed. The scoring rule is the
safety property that makes unattended runs defensible:

    actionable  <=>  declared AND imported

Everything else is reported with the reason it fell short. A dependency can be
declared and never used; a symbol can appear only in tests; IaC can provision
something no code touches. Those are different facts, and collapsing them into a
single yes/no is how a system starts inventing behaviour it cannot justify.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .iac import IacIndex
from .manifests import (
    Dep,
    read_gradle_deps,
    read_package_json,
    read_pom,
    read_python_project,
)
from .model import Target

SIGNATURES_PATH = Path(__file__).with_name("capabilities.yaml")

SOURCE_EXTENSIONS = {
    "java": (".java", ".kt", ".groovy", ".scala"),
    "javascript": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
    "python": (".py",),
}

PRUNE_DIRS = {"node_modules", "venv", ".venv", "target", "build", "dist", "out",
              "__pycache__", ".git", ".terraform", "site-packages"}

_TEST_FILE = re.compile(r"(^test_|_test\.|\.test\.|\.spec\.|Test\.[a-z]+$|Tests\.[a-z]+$)")
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_.-]+)(?::([^}]*))?\}")
_GETENV = re.compile(r"os\.getenv\(\s*[\"']([A-Za-z0-9_]+)[\"']\s*(?:,\s*[\"']([^\"']*)[\"'])?")

# Keys whose *values* must never reach the report. Discovery resolves
# `${ENV:default}` placeholders, and defaults for these are real credentials
# committed to a repository. The key is recorded; the value is not.
_SECRET_KEY = re.compile(
    r"(password|secret|token|credential|private[-_]?key|api[-_]?key|access[-_]?key|auth)", re.I
)
REDACTED = "<redacted>"


def load_signatures(path: Path = SIGNATURES_PATH) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ------------------------------------------------------- pass A: declared


def collect_deps(target: Target) -> list[Dep]:
    root = Path(target.path)
    deps: list[Dep] = []

    if (root / "pom.xml").is_file():
        pom = read_pom(root / "pom.xml")
        if pom:
            deps += pom.dependencies
    for script in ("build.gradle.kts", "build.gradle"):
        if (root / script).is_file():
            deps += read_gradle_deps(root / script)
            break
    if (root / "package.json").is_file():
        pkg = read_package_json(root / "package.json")
        if pkg:
            deps += pkg.dependencies
    if any((root / m).is_file() for m in ("requirements.txt", "pyproject.toml")):
        deps += read_python_project(root).dependencies

    return deps


# ------------------------------------------------------- pass B: imported


def _is_test_file(rel: str, test_paths: list[str]) -> bool:
    norm = rel.replace(os.sep, "/")
    if any(norm.startswith(tp.rstrip("/") + "/") for tp in test_paths):
        return True
    return bool(_TEST_FILE.search(os.path.basename(norm))) or "/test/" in norm


def scan_source_usage(
    target: Target, needles: dict[str, list[str]], max_bytes: int = 512_000
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Which needles appear in main source vs test source.

    The split matters: a client referenced only from tests is not evidence that
    the service uses it in production.
    """
    root = Path(target.path)
    language = (target.stack or {}).get("language", "")
    extensions = SOURCE_EXTENSIONS.get(language) or tuple(
        e for group in SOURCE_EXTENSIONS.values() for e in group
    )
    test_paths = (target.stack or {}).get("test_paths") or []

    main_hits: dict[str, list[str]] = {}
    test_hits: dict[str, list[str]] = {}

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".")]

        for name in filenames:
            if not name.endswith(extensions):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > max_bytes:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            rel = os.path.relpath(path, root)
            bucket = test_hits if _is_test_file(rel, test_paths) else main_hits

            for capability, patterns in needles.items():
                for pattern in patterns:
                    idx = text.find(pattern)
                    if idx != -1:
                        entry = f"{rel}:{text.count(chr(10), 0, idx) + 1} ({pattern})"
                        hits = bucket.setdefault(capability, [])
                        if len(hits) < 5 and entry not in hits:
                            hits.append(entry)
                        break

    return main_hits, test_hits


# ----------------------------------------------------- pass C: configured


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            flat.update(_flatten(value, f"{prefix}[{i}]"))
    elif prefix:
        flat[prefix] = node
    return flat


def read_config_keys(target: Target) -> dict[str, tuple[Any, str]]:
    root = Path(target.path)
    files = list((target.stack or {}).get("config_files") or [])
    for extra in (".env", ".env.example"):
        if (root / extra).is_file() and extra not in files:
            files.append(extra)

    keys: dict[str, tuple[Any, str]] = {}

    for rel in files:
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if path.suffix in (".yml", ".yaml"):
            try:
                for doc in yaml.safe_load_all(text):
                    for key, value in _flatten(doc).items():
                        keys[key] = (value, rel)
            except yaml.YAMLError:
                continue
        elif path.suffix == ".py":
            for m in _GETENV.finditer(text):
                keys[m.group(1)] = (m.group(2), f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
        else:  # .properties, .env
            for i, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, value = stripped.partition("=")
                    keys[key.strip()] = (value.strip(), f"{rel}:{i}")

    return keys


def _literal(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    m = _PLACEHOLDER.fullmatch(value.strip())
    return m.group(2) if m and m.group(2) is not None else value


def safe_value(key: str, value: Any) -> Any:
    """Resolve first, then redact.

    Order is load-bearing. Redacting before resolving makes an *unset* secret
    (`${TWILIO_AUTH_TOKEN:}`, default empty) look like a configured one, because
    the placeholder "<redacted>" is non-empty. Downstream that reads as a working
    integration when nothing is configured at all.
    """
    literal = _literal(value)
    if literal in (None, ""):
        return literal
    return REDACTED if _SECRET_KEY.search(key) else literal


# -------------------------------------------------------------- scoring


def _score(ev: dict[str, list[str]]) -> tuple[str, bool, str]:
    declared, imported = bool(ev["declared"]), bool(ev["imported"])
    configured, test_only = bool(ev["configured"]), bool(ev["test_only"])
    weak = bool(ev["weak_usage"])

    if declared and imported:
        return "high", True, "declared in a manifest and used in main source"
    if declared and weak and configured:
        # The weak import proves the category but not the vendor; a declared
        # driver plus a configured connection supplies the vendor. Together they
        # are as conclusive as a direct client import -- which matters because
        # JDBC and JPA code never names its driver.
        return ("high", True,
                "declared driver, category-level usage in main source, and a configured "
                "connection -- vendor identified by the driver")
    if declared and weak:
        return ("medium", False,
                "declared, with category-level usage only -- no vendor-specific client "
                "reference or configured connection")
    if declared and test_only:
        return "low", False, "declared but referenced only from test sources"
    if declared and configured:
        return ("medium", False,
                "declared and configured, but no main-source usage found -- the dependency "
                "is present without evidence the code exercises it")
    if imported and configured:
        return "medium", False, "used in source and configured, but not a declared dependency"
    if imported:
        return "medium", False, "used in source but not a declared dependency (transitive?)"
    if declared:
        return "low", False, "declared only; no usage or configuration found"
    if configured:
        return "low", False, "configuration keys match, but nothing declares or uses it"
    return "low", False, "weak signal only"


def _permissions(sig: dict, iac: IacIndex) -> tuple[dict[str, bool] | None, list[str]]:
    spec = sig.get("permissions")
    if not spec or not iac.present:
        return None, []

    perms: dict[str, bool] = {}
    evidence: list[str] = []
    for verb, actions in spec.items():
        hits = iac.granted(actions)
        perms[verb] = bool(hits)
        evidence += hits[:2]
    return perms, sorted(set(evidence))


def detect_capabilities(
    target: Target, iac: IacIndex, signatures: dict[str, dict[str, Any]] | None = None
) -> None:
    sigs = signatures if signatures is not None else load_signatures()

    deps = collect_deps(target)
    config_keys = read_config_keys(target)

    # Strong and weak needles scan in one walk, split apart by the |kind suffix,
    # so the source tree is only read once.
    needles: dict[str, list[str]] = {}
    for name, sig in sigs.items():
        needles[f"{name}|strong"] = sig.get("imports") or []
        needles[f"{name}|weak"] = sig.get("weak_imports") or []
    main_hits, test_hits = scan_source_usage(target, needles)

    findings: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for name, sig in sigs.items():
        evidence: dict[str, list[str]] = {
            "declared": [], "imported": [], "configured": [],
            "provisioned": [], "test_only": [], "weak_usage": [],
        }

        for pattern in sig.get("deps") or []:
            for dep in deps:
                if dep.matches(pattern):
                    entry = f'{dep.source} "{dep.name}"'
                    if entry not in evidence["declared"]:
                        evidence["declared"].append(entry)

        evidence["imported"] = main_hits.get(f"{name}|strong", [])
        evidence["test_only"] = test_hits.get(f"{name}|strong", [])
        evidence["weak_usage"] = main_hits.get(f"{name}|weak", [])

        resources: dict[str, Any] = {}
        for pattern in sig.get("config") or []:
            low = pattern.lower()
            for key, (value, source) in config_keys.items():
                if fnmatch.fnmatch(key.lower(), low):
                    entry = f'{source} "{key}"'
                    if entry not in evidence["configured"]:
                        evidence["configured"].append(entry)
                    literal = safe_value(key, value)
                    if literal not in (None, "", {}, []):
                        resources[key] = literal

        evidence["provisioned"] = iac.has_resource(*(sig.get("iac") or []))[:4]

        # A capability nobody declares, imports, or configures is not a finding.
        # In particular IaC alone never creates one: the index is workspace-wide,
        # so it would otherwise attach every provisioned resource to every target.
        if not (evidence["declared"] or evidence["imported"] or evidence["configured"]):
            continue

        confidence, actionable, reason = _score(evidence)
        perms, perm_evidence = _permissions(sig, iac)
        evidence["provisioned"] += perm_evidence

        finding = {
            "capability": name,
            "label": sig.get("label", name),
            "probe": sig.get("probe"),
            "confidence": confidence,
            "actionable": actionable,
            "reason": reason,
            "evidence": {k: v for k, v in evidence.items() if v},
            "permissions": perms,
            "resources": resources or None,
            "health_url": sig.get("health_url"),
        }
        (findings if actionable else skipped).append(finding)

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f["confidence"]], f["capability"]))
    skipped.sort(key=lambda f: (order[f["confidence"]], f["capability"]))

    target.capabilities = findings
    target.skipped_capabilities = skipped


def detect_all(targets: list[Target], iac: IacIndex) -> None:
    sigs = load_signatures()
    for target in targets:
        detect_capabilities(target, iac, sigs)
