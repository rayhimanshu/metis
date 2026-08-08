"""Packaging tests.

These exist because an editable install hides the failure completely. Every data
file here is loaded relative to `__file__`, so if setuptools does not ship it the
tool installs cleanly and then crashes on first use -- and the dev environment
never reproduces it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent / "metis"
PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

# Loaded at runtime relative to __file__, so each one must be declared as
# package data or the wheel is broken.
RUNTIME_DATA = [
    "bus/schema.sql",
    "discovery/capabilities.yaml",
    "policy/families.yaml",
    "hooks/settings.template.json",
    "roles/swe.md",
    "roles/devops.md",
    "roles/tester.md",
]


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative", RUNTIME_DATA)
def test_runtime_data_lives_inside_the_package(relative):
    assert (PACKAGE / relative).is_file(), f"{relative} is not inside the package"


@pytest.mark.parametrize("relative", RUNTIME_DATA)
def test_runtime_data_is_declared_as_package_data(relative, pyproject):
    """A file present in the tree but undeclared ships in neither wheel nor sdist."""
    import fnmatch

    patterns = pyproject["tool"]["setuptools"]["package-data"]["metis"]
    assert any(fnmatch.fnmatch(relative, p) for p in patterns), (
        f"{relative} matches no package-data pattern {patterns}"
    )


def test_console_script_is_declared(pyproject):
    assert pyproject["project"]["scripts"]["metis"] == "metis.cli:main"


def test_metadata_is_complete_enough_to_publish(pyproject):
    project = pyproject["project"]
    for field in ("name", "version", "description", "readme", "license", "requires-python"):
        assert project.get(field), f"pyproject is missing {field}"
    assert project["urls"]["Repository"].startswith("https://github.com/")


def test_no_loose_scripts_outside_the_package():
    """Hooks referenced by absolute path break the moment the tool is installed."""
    root = Path(__file__).parent.parent
    assert not (root / "hooks").exists(), "hooks must live inside the package"
    assert not (root / "roles").exists(), "role prompts must live inside the package"


def test_hook_template_uses_a_command_not_a_path():
    """`$METIS_HOME/hooks/x.py` cannot survive a pip install."""
    template = json.loads((PACKAGE / "hooks" / "settings.template.json").read_text())
    commands = [
        h["command"]
        for event in template["hooks"].values()
        for block in event for h in block["hooks"]
    ]
    assert commands == ["metis hook pre", "metis hook post"]
    assert not any("/" in c or "$" in c for c in commands)


def test_packaged_roles_cover_every_default_agent():
    """A fresh install must work with no files to create."""
    from metis.config import DEFAULTS

    for name in DEFAULTS["agents"]:
        assert (PACKAGE / "roles" / f"{name}.md").is_file(), f"no packaged prompt for {name}"


def test_project_roles_win_over_packaged(tmp_path):
    from metis.config import load

    (tmp_path / "metis.yaml").write_text("run:\n  workspace: .\n", encoding="utf-8")
    cfg = load(tmp_path / "metis.yaml")
    assert cfg.role_path("swe").is_relative_to(PACKAGE)

    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "swe.md").write_text("# custom\n", encoding="utf-8")
    cfg = load(tmp_path / "metis.yaml")

    resolved = cfg.role_path("swe")
    assert not resolved.is_relative_to(PACKAGE)
    assert resolved.read_text() == "# custom\n"
