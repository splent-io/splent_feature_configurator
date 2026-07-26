"""
Functional tests for splent_feature_configurator.

Functional tests use Flask's test client to exercise full HTTP
request/response cycles (GET, POST, redirects, rendered HTML).
The ConfiguratorService is patched in the service locator with a fake
that serves a fixture index, so no marketplace/network is needed.
"""

import pytest

from splent_io.splent_feature_configurator.services import ConfiguratorService
from splent_framework.services.service_locator import (
    get_service_class,
    register_service,
)


def _feature(package, presence="optional", group=None, group_kind=None):
    return {
        "org": "splent-io",
        "package": package,
        "presence": presence,
        "group": group,
        "group_kind": group_kind,
    }


FAKE_INDEX = {
    "schema": 1,
    "spls": [
        {
            "name": "demo_spl",
            "description": "Demo product line",
            "model": {
                "features": {
                    "base": _feature("splent_feature_base", presence="mandatory"),
                    "skin_a": _feature(
                        "splent_feature_skin_a", presence="mandatory",
                        group="skin", group_kind="alternative",
                    ),
                    "skin_b": _feature(
                        "splent_feature_skin_b", presence="mandatory",
                        group="skin", group_kind="alternative",
                    ),
                    "blog": _feature("splent_feature_blog"),
                },
                "alternative_groups": [
                    {"owner": "skin", "kind": "alternative", "members": ["skin_a", "skin_b"]},
                ],
                "constraints": [["blog", "base"]],
            },
        },
        {"name": "empty_spl", "description": "No model yet", "model": None},
    ],
}


class FakeConfiguratorService(ConfiguratorService):
    """Same validation logic, fixture index instead of marketplace/network."""

    def get_index(self):
        return FAKE_INDEX


@pytest.fixture()
def configurator_client(test_client):
    app = test_client.application
    original = get_service_class(app, "ConfiguratorService")
    register_service(app, "ConfiguratorService", FakeConfiguratorService)
    yield test_client
    register_service(app, "ConfiguratorService", original)


# ── GET /configurator ─────────────────────────────────────────────────


def test_index_lists_spls(configurator_client):
    response = configurator_client.get("/configurator")
    assert response.status_code == 200
    assert b"demo_spl" in response.data
    assert b"empty_spl" in response.data


# ── GET /configurator/<spl> ───────────────────────────────────────────


def test_configure_renders_feature_tree(configurator_client):
    response = configurator_client.get("/configurator/demo_spl")
    assert response.status_code == 200
    assert b"base" in response.data
    assert b"skin_a" in response.data
    assert b"skin_b" in response.data
    assert b"blog" in response.data
    # alternative group members render as radios, optionals as checkboxes
    assert b'type="radio"' in response.data
    assert b'type="checkbox"' in response.data


def test_configure_unknown_spl_is_404(configurator_client):
    assert configurator_client.get("/configurator/ghost_spl").status_code == 404


def test_configure_spl_without_model_is_404(configurator_client):
    assert configurator_client.get("/configurator/empty_spl").status_code == 404


# ── POST /configurator/<spl>/validate ─────────────────────────────────


def test_validate_valid_selection_returns_commands(configurator_client):
    response = configurator_client.post(
        "/configurator/demo_spl/validate",
        json={"selected": ["skin_a", "blog"], "product_name": "My Demo"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["selection_valid"] is True
    assert data["violations"] == []
    assert "base" in data["auto_added"]
    assert data["commands"][0] == "splent product:create my_demo --spl demo_spl"
    assert data["commands"][-1] == "splent product:derive --dev"
    assert "splent feature:add splent-io/splent_feature_blog" in data["commands"]


def test_validate_group_conflict_reports_violation(configurator_client):
    response = configurator_client.post(
        "/configurator/demo_spl/validate",
        json={"selected": ["skin_a", "skin_b"], "product_name": "demo"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["selection_valid"] is False
    assert data["commands"] == []
    assert data["violations"]
    violation = data["violations"][0]
    assert violation["code"] == "group_conflict"
    assert violation["message"]  # translated, human-readable


def test_validate_unknown_spl_is_404(configurator_client):
    response = configurator_client.post(
        "/configurator/ghost_spl/validate", json={"selected": []}
    )
    assert response.status_code == 404
