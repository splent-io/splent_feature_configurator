"""
Unit tests for splent_feature_configurator.

Unit tests verify individual classes and functions in isolation.
External dependencies (DB, HTTP, other services) MUST be mocked.
These tests should be fast and have zero side effects.
"""

import json

import pytest

from splent_io.splent_feature_configurator.services import ConfiguratorService


def _feature(package, presence="optional", group=None, group_kind=None):
    return {
        "org": "splent-io",
        "package": package,
        "presence": presence,
        "group": group,
        "group_kind": group_kind,
    }


# Fixture SPL model: one mandatory base, one required alternative group
# (skin_a | skin_b) and a transitive constraint chain comments => blog => media.
MODEL = {
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
        "comments": _feature("splent_feature_comments"),
        "media": _feature("splent_feature_media"),
    },
    "alternative_groups": [
        {"owner": "skin", "kind": "alternative", "members": ["skin_a", "skin_b"]},
    ],
    "constraints": [["comments", "blog"], ["blog", "media"]],
}

INDEX = {
    "schema": 1,
    "spls": [{"name": "demo_spl", "description": "Demo", "model": MODEL}],
}


@pytest.fixture()
def service(monkeypatch):
    svc = ConfiguratorService()
    monkeypatch.setattr(svc, "get_index", lambda: INDEX)
    return svc


# ── validate_selection ────────────────────────────────────────────────


def test_mandatory_feature_is_auto_included():
    result = ConfiguratorService().validate_selection(MODEL, ["skin_a"])
    assert result["selection_valid"] is True
    assert "base" in result["auto_added"]
    assert "base" in result["selection"]


def test_constraint_closure_is_transitive():
    result = ConfiguratorService().validate_selection(MODEL, ["skin_a", "comments"])
    assert result["selection_valid"] is True
    # comments => blog and blog => media, both auto-added
    assert "blog" in result["auto_added"]
    assert "media" in result["auto_added"]
    assert set(result["selection"]) == {"base", "skin_a", "comments", "blog", "media"}


def test_alternative_group_with_two_members_is_violation():
    result = ConfiguratorService().validate_selection(MODEL, ["skin_a", "skin_b"])
    assert result["selection_valid"] is False
    codes = [v["code"] for v in result["violations"]]
    assert "group_conflict" in codes
    conflict = next(v for v in result["violations"] if v["code"] == "group_conflict")
    assert conflict["group"] == "skin"
    assert set(conflict["features"]) == {"skin_a", "skin_b"}


def test_required_alternative_group_without_choice_is_violation():
    result = ConfiguratorService().validate_selection(MODEL, [])
    assert result["selection_valid"] is False
    codes = [v["code"] for v in result["violations"]]
    assert "group_required" in codes


def test_unknown_feature_is_violation():
    result = ConfiguratorService().validate_selection(MODEL, ["skin_a", "nope"])
    assert result["selection_valid"] is False
    codes = [v["code"] for v in result["violations"]]
    assert "unknown_feature" in codes


# ── validate + command generation ─────────────────────────────────────


def test_commands_are_correct_and_ordered(service):
    result = service.validate("demo_spl", ["skin_a", "comments"], "My Shop")
    assert result["selection_valid"] is True
    assert result["commands"] == [
        "splent product:create my_shop --spl demo_spl",
        "splent product:select my_shop",
        # feature:install (clone + register + env) follows the model
        # declaration order; feature:add would abort for repos that are not
        # cloned in the workspace yet.
        "splent feature:install splent-io/splent_feature_base",
        "splent feature:install splent-io/splent_feature_skin_a",
        "splent feature:install splent-io/splent_feature_blog",
        "splent feature:install splent-io/splent_feature_comments",
        "splent feature:install splent-io/splent_feature_media",
        "splent product:resolve",
        "splent product:derive --dev",
    ]


def test_invalid_selection_generates_no_commands(service):
    result = service.validate("demo_spl", ["skin_a", "skin_b"], "shop")
    assert result["selection_valid"] is False
    assert result["commands"] == []


def test_validate_unknown_spl_returns_none(service):
    assert service.validate("ghost_spl", ["skin_a"], "shop") is None


def test_product_name_is_normalized_and_defaulted():
    normalize = ConfiguratorService.normalize_product_name
    assert normalize("My Shop 2!") == "my_shop_2"
    assert normalize("") == "my_app"
    assert normalize("   ") == "my_app"


# ── feature_tree ──────────────────────────────────────────────────────


def test_feature_tree_buckets_and_group_required():
    tree = ConfiguratorService().feature_tree(MODEL)
    assert [f["short"] for f in tree["mandatory"]] == ["base"]
    assert {f["short"] for f in tree["optional"]} == {"blog", "comments", "media"}
    assert len(tree["groups"]) == 1
    group = tree["groups"][0]
    assert group["owner"] == "skin"
    assert group["required"] is True
    assert [m["short"] for m in group["members"]] == ["skin_a", "skin_b"]


def test_feature_tree_marks_features_absent_from_index_as_external():
    # The model may reference features that are not published in the index
    # (e.g. only on GitHub/PyPI): they are annotated, never dropped.
    known = {"base", "skin_a", "skin_b", "blog", "comments"}
    tree = ConfiguratorService().feature_tree(MODEL, known_shorts=known)
    by_short = {
        f["short"]: f
        for f in tree["mandatory"] + tree["optional"] + tree["groups"][0]["members"]
    }
    assert by_short["media"]["external"] is True
    assert all(
        by_short[short]["external"] is False for short in known
    )


def test_index_feature_shorts_reads_index_features(monkeypatch):
    svc = ConfiguratorService()
    monkeypatch.setattr(
        svc,
        "get_index",
        lambda: {
            "schema": 1,
            "features": [{"short": "base"}, {"short": "blog"}, {"nope": True}],
        },
    )
    assert svc.index_feature_shorts() == {"base", "blog"}


# ── index degradation (soft marketplace dependency) ───────────────────


def test_get_index_falls_back_to_local_cache(tmp_path, monkeypatch):
    svc = ConfiguratorService()
    # marketplace feature not installed → soft dependency degrades
    monkeypatch.setattr(svc, "_index_from_marketplace", lambda: None)
    cache = tmp_path / ".splent_cache" / "marketplace" / "index.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps(INDEX))
    monkeypatch.setattr(svc, "_cache_path", lambda: cache)
    monkeypatch.delenv("SPLENT_INDEX_URL", raising=False)

    index = svc.get_index()
    assert index["spls"][0]["name"] == "demo_spl"


def test_get_index_returns_none_when_no_source_available(tmp_path, monkeypatch):
    svc = ConfiguratorService()
    monkeypatch.setattr(svc, "_index_from_marketplace", lambda: None)
    monkeypatch.setattr(svc, "_cache_path", lambda: tmp_path / "missing.json")
    monkeypatch.delenv("SPLENT_INDEX_URL", raising=False)

    assert svc.get_index() is None
    assert svc.list_spls() == []
