"""Node projects.

The rule that matters: never assume `npm test`. `npm init` writes a placeholder
test script that exits 1, and a loop treating that as a real failing test will
chase a bug that does not exist.
"""

from __future__ import annotations

from pathlib import Path

from ..manifests import read_package_json
from ..model import Target
from ..registry import Stack

NAME = "node"
PRIORITY = 60

# lockfile -> (manager, run prefix, install argv)
LOCKFILES = {
    "pnpm-lock.yaml": ("pnpm", ["pnpm", "run"], ["pnpm", "install", "--frozen-lockfile"]),
    "yarn.lock": ("yarn", ["yarn"], ["yarn", "install", "--frozen-lockfile"]),
    "bun.lockb": ("bun", ["bun", "run"], ["bun", "install"]),
    "package-lock.json": ("npm", ["npm", "run"], ["npm", "ci"]),
}

PLACEHOLDER_TEST = "no test specified"
TEST_SCRIPTS = ("test", "test:unit", "test:ci", "jest", "vitest")
BUILD_SCRIPTS = ("build", "compile", "tsc")


def claims(target: Target) -> bool:
    return "package.json" in target.manifests


def _pick(scripts: dict[str, str], names: tuple[str, ...]) -> str | None:
    return next((n for n in names if n in scripts), None)


def describe(target: Target) -> Stack | None:
    root = Path(target.path)
    pkg = read_package_json(root / "package.json")
    if not pkg:
        return None

    evidence: list[str] = []
    manager, run_prefix, install = "npm", ["npm", "run"], ["npm", "install"]
    for lockfile, spec in LOCKFILES.items():
        if (root / lockfile).is_file():
            manager, run_prefix, install = spec
            evidence.append(f"{lockfile} -> package manager {manager}")
            break
    else:
        evidence.append("no lockfile found -> defaulting to npm install")

    commands: dict[str, list[str]] = {"install": install}

    build_script = _pick(pkg.scripts, BUILD_SCRIPTS)
    if build_script:
        commands["build"] = [*run_prefix, build_script]
        evidence.append(f"scripts.{build_script} -> build command")
    else:
        evidence.append("no build script in package.json -> build is a no-op")

    test_script = _pick(pkg.scripts, TEST_SCRIPTS)
    if test_script and PLACEHOLDER_TEST in pkg.scripts[test_script].lower():
        evidence.append(f"scripts.{test_script} is the npm placeholder -> treated as no tests")
        test_script = None

    if test_script:
        commands["test"] = [*run_prefix, test_script]
        evidence.append(f"scripts.{test_script} -> test command")
    else:
        evidence.append("no usable test script -> target has no test phase")

    if "lint" in pkg.scripts:
        commands["lint"] = [*run_prefix, "lint"]

    return Stack(
        kind=NAME, language="javascript", package_manager=manager,
        runtime={"node": pkg.engines.get("node"), "name": pkg.name, "version": pkg.version},
        commands=commands,
        test_paths=[d for d in ("test", "tests", "__tests__", "spec") if (root / d).is_dir()],
        source_paths=[d for d in ("src", "lib", "app") if (root / d).is_dir()] or ["."],
        config_files=[f for f in (".env", ".env.example", "config.json", "serverless.yml")
                      if (root / f).is_file()],
        confidence="high" if "build" in commands or "test" in commands else "medium",
        evidence=evidence,
    )
