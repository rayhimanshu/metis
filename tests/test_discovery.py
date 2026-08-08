"""Discovery tests, against synthetic fixtures so the suite runs anywhere.

Each test names the behaviour it protects. Most of these guard a decision that
looked obviously right and was obviously wrong the first time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metis.discovery import pipeline
from metis.discovery.capabilities import safe_value
from metis.discovery.scan import iter_candidates

FIXTURES = Path(__file__).parent.parent / "fixtures" / "workspace"


@pytest.fixture(scope="module")
def discovered():
    _, targets, report = pipeline.discover(str(FIXTURES), environment="dev")
    return {t.name: t for t in targets}, report


@pytest.fixture(scope="module")
def targets(discovered):
    return discovered[0]


@pytest.fixture(scope="module")
def report(discovered):
    return discovered[1]


# ------------------------------------------------------------------ scan


def test_finds_every_real_project(targets):
    assert set(targets) == {
        "maven-service", "gradle-service", "node-pnpm",
        "node-placeholder", "node-workspace", "python-suite",
    }


# Manifest-bearing decoys the fixtures carry on purpose. If one goes missing --
# dropped by a .gitignore rule, say -- the pruning test below would still pass
# while checking nothing at all, so their presence is asserted separately.
DECOYS = [
    "node-pnpm/node_modules/left-pad/package.json",
    "python-suite/venv/lib/requirements.txt",
    ".hidden-tool/package.json",
    "terraform/.terraform/modules/junk/main.tf",
]


@pytest.mark.parametrize("relative", DECOYS)
def test_the_decoys_are_actually_present(relative):
    """Silence is not success: pruning nothing looks identical to pruning well."""
    assert (FIXTURES / relative).is_file(), (
        f"missing decoy {relative} -- the pruning tests would pass vacuously"
    )


def test_vendor_and_hidden_dirs_never_surface():
    """Pruning happens before descending, not by filtering afterwards."""
    paths = [c.path for c in iter_candidates(FIXTURES)]
    for poison in ("node_modules", "/venv/", "/target/", "hidden-tool", ".terraform"):
        assert not any(poison in p for p in paths), f"{poison} leaked into candidates"


def test_declared_workspace_members_are_modules_not_targets(targets):
    """A manifest inside another project is a module only if the parent says so."""
    assert "pkg-a" not in targets and "pkg-b" not in targets
    assert targets["node-workspace"].modules == ["packages/a", "packages/b"]


# -------------------------------------------------------------- detectors


def test_maven_commands_are_derived_not_assumed(targets):
    stack = targets["maven-service"].stack
    assert stack["commands"]["build"] == ["./mvnw", "-B", "verify"]  # src/test exists
    assert stack["runtime"]["jdk"] == "21"


def test_maven_without_tests_packages_instead_of_verifying(targets):
    """`verify` on a project with no tests is a slower way to do `package`."""
    assert targets["gradle-service"].stack["test_paths"] == []


def test_node_reads_the_scripts_block(targets):
    stack = targets["node-pnpm"].stack
    assert stack["package_manager"] == "pnpm"
    assert stack["commands"]["install"] == ["pnpm", "install", "--frozen-lockfile"]
    assert stack["commands"]["test"] == ["pnpm", "run", "test"]


def test_npm_placeholder_test_is_not_a_test_phase(targets):
    """`npm init` writes a test script that exits 1.

    Treating it as a real failing test sends the loop chasing a bug that does
    not exist.
    """
    stack = targets["node-placeholder"].stack
    assert "test" not in stack["commands"]
    assert any("placeholder" in e for e in stack["evidence"])


def test_python_uses_a_checked_in_virtualenv_when_present(targets):
    assert targets["python-suite"].stack["commands"]["test"][:3] == [
        "python3", "-m", "pytest"
    ] or targets["python-suite"].stack["runtime"]["venv"]


# ----------------------------------------------------------- capabilities


def _finding(target, name):
    return next((c for c in target.capabilities if c["capability"] == name), None)


def _skipped(target, name):
    return next((c for c in target.skipped_capabilities if c["capability"] == name), None)


def test_declared_and_imported_is_actionable(targets):
    s3 = _finding(targets["maven-service"], "s3")
    assert s3 and s3["confidence"] == "high"
    assert s3["evidence"]["declared"] and s3["evidence"]["imported"]


def test_declared_but_never_imported_is_not_actionable(targets):
    """The rule that makes unattended operation defensible.

    firebase-admin is declared and firebase.* is configured, but no source file
    imports a Firestore client. Reported, not acted on.
    """
    assert _finding(targets["maven-service"], "firestore") is None

    skipped = _skipped(targets["maven-service"], "firestore")
    assert skipped and skipped["confidence"] == "medium"
    assert "no main-source usage" in skipped["reason"]


def test_test_only_usage_does_not_make_a_capability(targets):
    """boto3 used only from tests is not evidence the service uses S3."""
    skipped = _skipped(targets["python-suite"], "s3")
    assert skipped and "only from test sources" in skipped["reason"]


def test_driver_plus_orm_plus_connection_identifies_the_vendor(targets):
    """JDBC and JPA code never names its driver.

    `jakarta.persistence` proves relational access but not which database; the
    declared driver and configured URL supply the vendor.
    """
    postgres = _finding(targets["maven-service"], "postgres")
    assert postgres and postgres["confidence"] == "high"
    assert postgres["evidence"]["weak_usage"]


def test_one_orm_import_does_not_claim_every_database(targets):
    """The counterpart: MySQL has no declared driver, so it stays out."""
    assert _finding(targets["maven-service"], "mysql") is None


def test_gradle_dependencies_are_parsed(targets):
    redis = _finding(targets["gradle-service"], "redis")
    assert redis, "build.gradle declares jedis; it must count as declared"
    assert redis["evidence"]["declared"]


def test_iac_bounds_probe_depth(targets):
    """Permissions come from IaC, so behaviour is constrained by what is granted."""
    s3 = _finding(targets["maven-service"], "s3")
    assert s3["permissions"] == {"read": True, "write": True, "delete": True, "list": True}


def test_iac_alone_never_creates_a_finding(targets):
    """The index is workspace-wide.

    Without this rule every provisioned resource would attach to every target.
    node-pnpm has no database, though terraform declares one.
    """
    assert _finding(targets["node-pnpm"], "postgres") is None
    assert _skipped(targets["node-pnpm"], "postgres") is None


def test_terraform_module_cache_is_ignored(report):
    """`.terraform/` mirrors upstream modules and is not this workspace's IaC."""
    assert not any(".terraform" in f for f in report["workspace"]["iac"]["files"])


# --------------------------------------------------------------- secrets


def test_secret_config_values_are_redacted(targets):
    postgres = _finding(targets["maven-service"], "postgres")
    resources = postgres["resources"]
    assert resources["spring.datasource.password"] == "<redacted>"
    assert "hunter2" not in str(resources)


def test_unset_secret_is_absent_rather_than_redacted():
    """Resolve first, then redact.

    Redacting first makes an unset secret look configured, because the
    placeholder "<redacted>" is non-empty.
    """
    assert safe_value("twilio.auth_token", "${TWILIO_AUTH_TOKEN:}") == ""
    assert safe_value("twilio.auth_token", "${TWILIO_AUTH_TOKEN:abcdef}") == "<redacted>"
    assert safe_value("spring.datasource.url", "jdbc:postgresql://h/db") == "jdbc:postgresql://h/db"


# ------------------------------------------------------------ deployment


def test_deploy_identifiers_are_lifted_from_ci(targets):
    deployment = targets["maven-service"].deployment
    assert deployment["kind"] == "aws_ecs"
    assert deployment["identifiers"]["cluster"] == "demo-cluster"
    assert deployment["identifiers"]["region"] == "eu-west-1"
    assert deployment["trigger"]["branches"] == ["main"]


def test_unrecognised_deploy_is_reported_not_guessed(targets):
    """Silently not deploying is a bug; reporting that you cannot is correct."""
    deployment = targets["gradle-service"].deployment
    assert deployment["kind"] == "unknown"
    assert any("locally only" in e for e in deployment["evidence"])


def test_load_balancer_polled_paths_are_surfaced(report):
    """A deep probe on this path turns a dependency blip into a restart storm."""
    assert report["workspace"]["load_balancer_polled_paths"] == ["/actuator/health"]


# ---------------------------------------------------------------- testing


def test_suite_maps_to_the_targets_it_drives(targets):
    """A failure must route to the service that broke, not the suite."""
    assert set(targets["python-suite"].tests["exercises"]) == {
        "maven-service", "gradle-service"
    }
    assert "python-suite" in targets["maven-service"].tests["covered_by"]


def test_a_deployable_service_is_not_a_test_suite(targets):
    """Config naming other services is wiring, not coverage.

    Otherwise an API gateway looks like it tests everything it proxies.
    """
    assert targets["maven-service"].tests["exercises"] == {}


# -------------------------------------------------------------- lock keys


def test_schema_key_comes_from_the_connection_string(targets):
    """Migrations serialise on the database, so the key cannot be the service."""
    assert "schema:demodb" in targets["maven-service"].lock_keys["deploy"]


def test_cluster_key_comes_from_ci_identifiers(targets):
    assert "cluster:demo-cluster" in targets["maven-service"].lock_keys["deploy"]


def test_deploy_keys_are_sorted(targets):
    """Two multi-key actions acquiring in different orders deadlock."""
    keys = targets["maven-service"].lock_keys["deploy"]
    assert keys == sorted(keys)


def test_env_key_is_per_environment(targets):
    assert targets["python-suite"].lock_keys["test"] == ["env:dev"]
