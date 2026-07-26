"""Functional tests of the REAL configurator→marketplace chain.

Unlike test_routes.py (which replaces ConfiguratorService with a fake), these
tests keep the real ConfiguratorService AND the real MarketplaceService
registered in the product, stubbing only the marketplace's index source
(``_load_index``). They pin the production contract:

    service_proxy("MarketplaceService") → index() → _is_index (schema == 1)

so renaming ``MarketplaceService.index()`` or changing the index shape can no
longer degrade the configurator silently while every suite stays green.
"""

import copy
import json

import pytest

from splent_io.splent_feature_configurator.services import ConfiguratorService

marketplace_services = pytest.importorskip(
    "splent_io.splent_feature_marketplace.services",
    reason="the chain tests need the marketplace feature installed",
)

CHAIN_INDEX = {
    "schema": 1,
    "generated_at": "2026-07-26T00:00:00+00:00",
    "features": [{"short": "base"}, {"short": "session_a"}, {"short": "session_b"}],
    "spls": [
        {
            "name": "chain_spl",
            "description": "Chain fixture SPL",
            "model": {
                "features": {
                    "base": {
                        "org": "splent-io",
                        "package": "splent_feature_base",
                        "presence": "mandatory",
                        "group": None,
                        "group_kind": None,
                    },
                    "session_a": {
                        "org": "splent-io",
                        "package": "splent_feature_session_a",
                        "presence": "mandatory",
                        "group": "session",
                        "group_kind": "alternative",
                    },
                    "session_b": {
                        "org": "splent-io",
                        "package": "splent_feature_session_b",
                        "presence": "mandatory",
                        "group": "session",
                        "group_kind": "alternative",
                    },
                    "cache": {
                        "org": "splent-io",
                        "package": "splent_feature_cache",
                        "presence": "optional",
                        "group": None,
                        "group_kind": None,
                    },
                },
                "alternative_groups": [
                    {
                        "owner": "session",
                        "kind": "alternative",
                        "members": ["session_a", "session_b"],
                    }
                ],
                "constraints": [["session_b", "cache"]],
            },
        }
    ],
    "collisions": [],
}


@pytest.fixture()
def marketplace_index(monkeypatch):
    """Serve CHAIN_INDEX through the real MarketplaceService."""
    monkeypatch.setattr(
        marketplace_services.MarketplaceService,
        "_load_index",
        lambda self, force=False: copy.deepcopy(CHAIN_INDEX),
    )


def test_configure_page_renders_through_real_marketplace_service(
    test_client, marketplace_index
):
    response = test_client.get("/configurator/chain_spl")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    for short in ("base", "session_a", "session_b", "cache"):
        assert short in html


def test_validate_through_real_marketplace_service(test_client, marketplace_index):
    response = test_client.post(
        "/configurator/chain_spl/validate",
        json={"selected": ["session_b"], "product_name": "Chain App"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["selection_valid"] is True
    # base is mandatory and cache is pulled in by session_b => cache.
    assert "base" in data["auto_added"]
    assert "cache" in data["auto_added"]
    assert "splent feature:install splent-io/splent_feature_cache" in data["commands"]
    assert data["commands"][0] == "splent product:create chain_app --spl chain_spl"


def test_empty_marketplace_index_falls_back_to_local_cache(
    test_client, tmp_path, monkeypatch
):
    """EMPTY_INDEX has no ``schema`` key: _is_index rejects it and get_index
    must fall back to the configurator's own local cache."""
    monkeypatch.setattr(
        marketplace_services.MarketplaceService,
        "_load_index",
        lambda self, force=False: dict(marketplace_services.EMPTY_INDEX),
    )
    cache = tmp_path / "index.json"
    cache.write_text(json.dumps(CHAIN_INDEX))
    monkeypatch.setattr(ConfiguratorService, "_cache_path", lambda self: cache)

    response = test_client.get("/configurator/chain_spl")
    assert response.status_code == 200
    assert b"session_a" in response.data
