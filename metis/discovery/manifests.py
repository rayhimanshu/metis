"""Manifest readers, shared by the detector and capability stages.

Every dependency carries a `source` of the form `file:line`. That string is the
`declared` evidence a capability finding is built from, and it is what lets the
report point at the exact line that justified a decision rather than asserting
one.
"""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Dep:
    name: str  # "group:artifact" for Java, package name otherwise
    version: str | None = None
    scope: str | None = None
    source: str = ""

    def matches(self, pattern: str) -> bool:
        """Coordinate match, tolerant of how people write them in config.

        Exact match, or artifact-only match against the part after the colon --
        so a bare `s3` matches `software.amazon.awssdk:s3` but never `s3transfer`.
        """
        p, n = pattern.lower(), self.name.lower()
        if n == p:
            return True
        if ":" in n and ":" not in p:
            return n.rsplit(":", 1)[1] == p
        return False


def find_line(text: str, needle: str) -> int:
    """1-indexed line of `needle`, or 0 when absent."""
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx != -1 else 0


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(elem, name: str):
    return next((c for c in elem if _strip_ns(c.tag) == name), None)


def _text(elem, name: str) -> str | None:
    c = _child(elem, name)
    return c.text.strip() if c is not None and c.text else None


# ------------------------------------------------------------------ maven


@dataclass
class Pom:
    path: Path
    artifact_id: str | None = None
    group_id: str | None = None
    version: str | None = None
    parent: str | None = None
    parent_version: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    dependencies: list[Dep] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)

    def prop(self, *names: str) -> str | None:
        for name in names:
            if name in self.properties:
                return self.resolve(self.properties[name])
        return None

    def resolve(self, value: str | None) -> str | None:
        if not value:
            return value
        return re.sub(r"\$\{([^}]+)\}",
                      lambda m: self.properties.get(m.group(1), m.group(0)), value)


def read_pom(path: Path) -> Pom | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        root = ET.fromstring(raw)
    except (ET.ParseError, OSError):
        return None

    pom = Pom(path=path)
    pom.artifact_id = _text(root, "artifactId")
    pom.group_id = _text(root, "groupId")
    pom.version = _text(root, "version")

    parent = _child(root, "parent")
    if parent is not None:
        pom.parent = _text(parent, "artifactId")
        pom.parent_version = _text(parent, "version")
        pom.group_id = pom.group_id or _text(parent, "groupId")

    props = _child(root, "properties")
    if props is not None:
        pom.properties = {_strip_ns(p.tag): p.text.strip() for p in props if p.text}

    mods = _child(root, "modules")
    if mods is not None:
        pom.modules = [m.text.strip() for m in mods if m.text and m.text.strip()]

    deps = _child(root, "dependencies")
    if deps is not None:
        for d in deps:
            group, artifact = _text(d, "groupId"), _text(d, "artifactId")
            if not artifact:
                continue
            pom.dependencies.append(Dep(
                name=f"{group}:{artifact}" if group else artifact,
                version=pom.resolve(_text(d, "version")),
                scope=_text(d, "scope"),
                source=f"{path.name}:{find_line(raw, f'<artifactId>{artifact}<')}",
            ))

    return pom


# ------------------------------------------------------------------- node


@dataclass
class PackageJson:
    path: Path
    name: str | None = None
    version: str | None = None
    scripts: dict[str, str] = field(default_factory=dict)
    engines: dict[str, str] = field(default_factory=dict)
    dependencies: list[Dep] = field(default_factory=list)


def read_package_json(path: Path) -> PackageJson | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    pkg = PackageJson(
        path=path, name=data.get("name"), version=data.get("version"),
        scripts=data.get("scripts") or {}, engines=data.get("engines") or {},
    )
    for section, scope in (("dependencies", "runtime"), ("devDependencies", "dev"),
                           ("peerDependencies", "peer"), ("optionalDependencies", "optional")):
        for name, version in (data.get(section) or {}).items():
            pkg.dependencies.append(Dep(
                name=name, version=version if isinstance(version, str) else None,
                scope=scope, source=f'{path.name}:{find_line(raw, chr(34) + name + chr(34))}',
            ))
    return pkg


# ----------------------------------------------------------------- gradle

# implementation 'g:a:v' | api("g:a:v") | testImplementation 'g:a'
_GRADLE_DEP = re.compile(
    r"\b(implementation|api|compileOnly|runtimeOnly|developmentOnly|annotationProcessor"
    r"|testImplementation|testRuntimeOnly)\s*\(?\s*['\"]([^'\"]+)['\"]"
)


def read_gradle_deps(path: Path) -> list[Dep]:
    """Dependencies declared in a Gradle build script.

    Regex rather than evaluation: a build script is a program, and running an
    untrusted one to find out what it depends on is not a trade worth making.
    Declarations using variables or version catalogues are missed, which costs a
    `declared` signal and is reported as such rather than guessed at.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    deps: list[Dep] = []
    for m in _GRADLE_DEP.finditer(raw):
        configuration, coordinate = m.group(1), m.group(2)
        parts = coordinate.split(":")
        if len(parts) < 2:
            continue
        deps.append(Dep(
            name=f"{parts[0]}:{parts[1]}",
            version=parts[2] if len(parts) > 2 else None,
            scope="test" if configuration.startswith("test") else "runtime",
            source=f"{path.name}:{raw.count(chr(10), 0, m.start()) + 1}",
        ))
    return deps


# ----------------------------------------------------------------- python

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*([<>=!~].*)?$")


@dataclass
class PythonProject:
    path: Path
    name: str | None = None
    dependencies: list[Dep] = field(default_factory=list)
    pytest_configured: bool = False
    has_build_system: bool = False


def read_requirements(path: Path) -> list[Dep]:
    deps: list[Dep] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return deps

    for i, line in enumerate(lines, start=1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped or stripped.startswith("-"):
            continue
        m = _REQ_LINE.match(stripped)
        if m:
            deps.append(Dep(name=m.group(1), version=(m.group(2) or "").strip() or None,
                            scope="runtime", source=f"{path.name}:{i}"))
    return deps


def read_python_project(root: Path) -> PythonProject:
    proj = PythonProject(path=root)

    req = root / "requirements.txt"
    if req.is_file():
        proj.dependencies += read_requirements(req)

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            raw = pyproject.read_text(encoding="utf-8", errors="replace")
            data = tomllib.loads(raw)
        except (tomllib.TOMLDecodeError, OSError):
            data, raw = {}, ""

        proj.name = data.get("project", {}).get("name")
        proj.has_build_system = "build-system" in data
        proj.pytest_configured = "pytest" in data.get("tool", {})

        for spec in data.get("project", {}).get("dependencies", []) or []:
            m = _REQ_LINE.match(str(spec))
            if m:
                proj.dependencies.append(Dep(
                    name=m.group(1), version=(m.group(2) or "").strip() or None,
                    scope="runtime", source=f"pyproject.toml:{find_line(raw, str(spec))}"))

        for name, version in (data.get("tool", {}).get("poetry", {}).get("dependencies") or {}).items():
            if name.lower() != "python":
                proj.dependencies.append(Dep(
                    name=name, version=version if isinstance(version, str) else None,
                    scope="runtime", source=f"pyproject.toml:{find_line(raw, name)}"))

    if not proj.pytest_configured:
        proj.pytest_configured = (
            any((root / f).is_file() for f in ("pytest.ini", "tox.ini", "setup.cfg", "conftest.py"))
            or (root / "tests" / "conftest.py").is_file()
        )

    return proj
