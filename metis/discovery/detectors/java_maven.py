"""Maven projects."""

from __future__ import annotations

from pathlib import Path

from ..manifests import read_pom
from ..model import Target
from ..registry import Stack

NAME = "java_maven"
PRIORITY = 80


def claims(target: Target) -> bool:
    return "pom.xml" in target.manifests


def describe(target: Target) -> Stack | None:
    root = Path(target.path)
    pom = read_pom(root / "pom.xml")
    if not pom:
        return None

    evidence: list[str] = []

    # The wrapper pins the Maven version the project was built against, so it
    # always beats whatever `mvn` happens to be on PATH.
    if (root / "mvnw").is_file():
        mvn, why = "./mvnw", "mvnw wrapper present -> using ./mvnw"
    else:
        mvn, why = "mvn", "no mvnw wrapper -> using mvn from PATH"
    evidence.append(why)

    has_tests = (root / "src" / "test").is_dir()
    goal = "verify" if has_tests else "package"
    evidence.append(f"src/test {'exists' if has_tests else 'absent'} -> build goal '{goal}'")

    jdk = pom.prop("java.version", "maven.compiler.release", "maven.compiler.source")
    if jdk:
        evidence.append(f"JDK {jdk} from pom properties")
    elif pom.parent:
        evidence.append(f"JDK unset in pom; inherited from parent {pom.parent}")

    resources = root / "src" / "main" / "resources"
    config_files = sorted(
        str(p.relative_to(root))
        for pattern in ("application*.y*ml", "application*.properties")
        for p in resources.glob(pattern)
    )

    # -B keeps Maven non-interactive and drops the download progress spam that
    # would otherwise dominate any captured log.
    return Stack(
        kind=NAME, language="java", package_manager="maven",
        runtime={"jdk": jdk, "artifact": pom.artifact_id, "version": pom.version,
                 "parent": pom.parent, "parent_version": pom.parent_version},
        commands={
            "compile": [mvn, "-B", "-q", "compile"],
            "build": [mvn, "-B", goal],
            "test": [mvn, "-B", "test"],
            "package_skip_tests": [mvn, "-B", "package", "-DskipTests"],
        },
        test_paths=["src/test/java"] if has_tests else [],
        source_paths=["src/main/java"],
        config_files=config_files,
        evidence=evidence,
    )
