"""Python projects.

A checked-in virtualenv is the norm in test-automation repositories, and its
interpreter already has the dependencies installed. Finding it is the difference
between a test command that runs and one that ImportErrors on line one.
"""

from __future__ import annotations

from pathlib import Path

from ..manifests import read_python_project
from ..model import Target
from ..registry import Stack

NAME = "python"
PRIORITY = 60

VENV_DIRS = ("venv", ".venv", "env", ".env")


def claims(target: Target) -> bool:
    return any(m in target.manifests
               for m in ("requirements.txt", "pyproject.toml", "setup.py"))


def _find_venv(root: Path) -> Path | None:
    return next((root / n / "bin" / "python" for n in VENV_DIRS
                 if (root / n / "bin" / "python").is_file()), None)


def describe(target: Target) -> Stack | None:
    root = Path(target.path)
    proj = read_python_project(root)
    evidence: list[str] = []

    venv_python = _find_venv(root)
    if venv_python:
        python = str(venv_python.relative_to(root))
        evidence.append(f"virtualenv found -> using {python}")
    else:
        python = "python3"
        evidence.append("no virtualenv found -> using python3 from PATH")

    uses_pytest = proj.pytest_configured or any(d.name.lower() == "pytest"
                                                for d in proj.dependencies)
    test_paths = [d for d in ("tests", "test") if (root / d).is_dir()]
    commands: dict[str, list[str]] = {}

    if (root / "requirements.txt").is_file():
        commands["install"] = [python, "-m", "pip", "install", "-r", "requirements.txt"]
    elif proj.has_build_system:
        commands["install"] = [python, "-m", "pip", "install", "-e", "."]

    if uses_pytest:
        # A short traceback keeps a failure small enough to feed back into the
        # loop without truncating the part that identifies the fault.
        commands["test"] = [python, "-m", "pytest", "-q", "--tb=short"]
        evidence.append("pytest configured or declared -> pytest test command")
    elif test_paths:
        commands["test"] = [python, "-m", "unittest", "discover", "-s", test_paths[0]]
        evidence.append(f"{test_paths[0]}/ present, no pytest -> unittest discover")
    else:
        evidence.append("no tests detected -> target has no test phase")

    if proj.has_build_system:
        commands["build"] = [python, "-m", "build"]
        evidence.append("pyproject [build-system] -> python -m build")
    else:
        # Nothing to compile, but a syntax check still gives the loop a fast,
        # meaningful build signal rather than a silent pass.
        commands["build"] = [python, "-m", "compileall", "-q", "."]
        evidence.append("no build backend -> compileall used as the build check")

    return Stack(
        kind=NAME, language="python", package_manager="pip",
        runtime={"python": python, "name": proj.name, "venv": bool(venv_python)},
        commands=commands,
        test_paths=test_paths,
        source_paths=[d for d in ("src", "utils", "lib") if (root / d).is_dir()] or ["."],
        config_files=[f for f in ("config.py", "settings.py", ".env", "pytest.ini", "conftest.py")
                      if (root / f).is_file()],
        evidence=evidence,
    )
