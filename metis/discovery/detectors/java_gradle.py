"""Gradle projects."""

from __future__ import annotations

import re
from pathlib import Path

from ..model import Target
from ..registry import Stack

NAME = "java_gradle"
PRIORITY = 80

# sourceCompatibility = 17 | JavaVersion.VERSION_17 | JavaLanguageVersion.of(21)
_JDK = re.compile(
    r"(?:sourceCompatibility|targetCompatibility)\s*=?\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+)"
    r"|JavaLanguageVersion\.of\((\d+)\)"
)


def claims(target: Target) -> bool:
    return any(m.startswith("build.gradle") for m in target.manifests)


def describe(target: Target) -> Stack | None:
    root = Path(target.path)
    script = next((root / n for n in ("build.gradle.kts", "build.gradle")
                   if (root / n).is_file()), None)
    if not script:
        return None

    evidence: list[str] = []
    if (root / "gradlew").is_file():
        gradle, why = "./gradlew", "gradlew wrapper present -> using ./gradlew"
    else:
        gradle, why = "gradle", "no gradlew wrapper -> using gradle from PATH"
    evidence.append(why)

    match = _JDK.search(script.read_text(encoding="utf-8", errors="replace"))
    jdk = (match.group(1) or match.group(2)) if match else None
    if jdk:
        evidence.append(f"JDK {jdk} from {script.name}")

    has_tests = (root / "src" / "test").is_dir()
    evidence.append(f"src/test {'exists' if has_tests else 'absent'}")

    resources = root / "src" / "main" / "resources"
    config_files = sorted(str(p.relative_to(root)) for p in resources.glob("application*.y*ml"))

    return Stack(
        kind=NAME, language="java", package_manager="gradle",
        runtime={"jdk": jdk},
        commands={
            "compile": [gradle, "--console=plain", "-q", "compileJava"],
            "build": [gradle, "--console=plain", "build"],
            "test": [gradle, "--console=plain", "test"],
            "package_skip_tests": [gradle, "--console=plain", "build", "-x", "test"],
        },
        test_paths=["src/test/java"] if has_tests else [],
        source_paths=["src/main/java"],
        config_files=config_files,
        evidence=evidence,
    )
