"""Probe policy tests.

Both rules here are the kind that look like over-caution until the day they
matter, and are very expensive to rediscover afterwards.
"""

from __future__ import annotations

import pytest

from metis.discovery import pipeline
from metis.policy import bounds
from tests.test_discovery import FIXTURES


@pytest.fixture(scope="module")
def targets():
    _, discovered, report = pipeline.discover(str(FIXTURES), environment="dev")
    return {t.name: t for t in discovered}, set(
        report["workspace"]["load_balancer_polled_paths"]
    )


def _plan(targets, name):
    by_name, lb_paths = targets
    return bounds.plan(by_name[name], lb_paths)


def _probe(plan, capability):
    return next((p for p in plan.probes if p.capability == capability), None)


# ------------------------------------------------------------- endpoint


def test_endpoint_avoids_the_polled_path(targets):
    """A deep probe there turns a dependency blip into a restart storm."""
    _, lb_paths = targets
    assert "/actuator/health" in lb_paths

    endpoint, rationale = bounds.choose_endpoint(lb_paths)
    assert endpoint not in lb_paths
    assert "restart healthy instances" in rationale


def test_endpoint_falls_through_when_the_obvious_one_is_taken():
    endpoint, _ = bounds.choose_endpoint({"/health-check"})
    assert endpoint == "/health-check/deep"


def test_no_available_endpoint_is_an_error():
    with pytest.raises(ValueError, match="every candidate"):
        bounds.choose_endpoint(set(bounds.PREFERRED_PATHS))


def test_rendered_plan_warns_against_the_framework_indicator(targets):
    text = bounds.render(_plan(targets, "maven-service"))
    assert "must not contribute to the endpoint the load balancer polls" in text


# ---------------------------------------------------- permission bounds


def test_full_permissions_yield_a_full_round_trip(targets):
    """The fixture's IAM grants Put, Get and Delete."""
    s3 = _probe(_plan(targets, "maven-service"), "s3")
    assert s3.depth == "deep"
    assert s3.legs == ["write", "read", "delete"]


def test_missing_write_downgrades_deep_to_light():
    """Generating a write that was never going to succeed helps nobody."""
    families = bounds.load_families()
    legs, notes = bounds._legs_for(families["object_store"],
                                   {"read": True, "write": False, "delete": False})
    assert legs == ["read"]
    assert any("'write' not granted" in n for n in notes)


def test_unknown_permissions_default_to_read_only():
    legs, notes = bounds._legs_for(bounds.load_families()["object_store"], None)
    assert legs == ["read"]
    assert any("defaulting to read-only" in n for n in notes)


# ----------------------------------------------------------------- depth


def test_a_third_party_api_is_never_polled_live(targets):
    """Their rate limit must not become our outage."""
    plan = _plan(targets, "maven-service")
    stripe = _probe(plan, "stripe")
    if stripe:  # only when stripe is actionable in the fixture
        assert stripe.depth == "config_only"

    families = bounds.load_families()
    for family in ("http_dependency", "smtp", "push_messaging"):
        assert families[family]["depth"] == "config_only"


def test_a_database_gets_a_cheap_read_not_a_write(targets):
    postgres = _probe(_plan(targets, "maven-service"), "postgres")
    assert postgres.depth == "light"
    assert postgres.legs == ["query"]


def test_config_only_probes_carry_every_matched_key(targets):
    """Judging from one key reports NOT_CONFIGURED for working integrations."""
    families = bounds.load_families()
    by_name, lb_paths = targets

    for target in by_name.values():
        plan = bounds.plan(target, lb_paths)
        for probe in plan.probes:
            if probe.depth == "config_only" and probe.resources:
                assert probe.config_keys, f"{probe.capability} has no keys to check"


# -------------------------------------------------------------- excluded


def test_non_actionable_capabilities_are_explained_not_dropped(targets):
    plan = _plan(targets, "maven-service")
    reasons = {e["capability"]: e["reason"] for e in plan.excluded}

    assert "firestore" in reasons
    assert "not actionable" in reasons["firestore"]


def test_probes_are_only_planned_for_actionable_capabilities(targets):
    plan = _plan(targets, "maven-service")
    planned = {p.capability for p in plan.probes}
    assert "firestore" not in planned
