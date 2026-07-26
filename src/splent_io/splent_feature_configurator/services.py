"""ConfiguratorService — the UVL configurator behind the marketplace.

Given an SPL from the marketplace index, it lets a caller pick a set of
features and validates the selection against the SPL variability model
(mandatory features, requires-constraints, alternative groups) without any
external solver. For a valid selection it produces the exact ``splent``
commands that build the product.

The marketplace index is obtained through a SOFT dependency on the
marketplace feature (``MarketplaceService`` via the service locator). When
that feature is absent the service degrades gracefully and reads the index
itself: local cache first, then ``SPLENT_INDEX_URL``.
"""

import json
import os
import re
from pathlib import Path

INDEX_SCHEMA = 1
INDEX_URL_ENV = "SPLENT_INDEX_URL"
WORKSPACE_ENV = "WORKING_DIR"
DEFAULT_WORKSPACE = "/workspace"
DEFAULT_ORG = "splent-io"
DEFAULT_PRODUCT_NAME = "my_app"


class ConfiguratorService:
    """Plain service (no DB): index access + variability validation."""

    # ── Marketplace index (soft dependency) ───────────────────────────

    def get_index(self) -> dict | None:
        """Best marketplace index available, or ``None``.

        Order: marketplace feature → local cache → ``SPLENT_INDEX_URL``.
        """
        for source in (
            self._index_from_marketplace,
            self._index_from_cache,
            self._index_from_url,
        ):
            index = source()
            if index is not None:
                return index
        return None

    def _index_from_marketplace(self) -> dict | None:
        """Ask the marketplace feature for the index, if it is installed.

        SOFT dependency: the marketplace feature may not be part of the
        product (or may still be booting), so every failure — service not
        registered, no request context, missing method — degrades to the
        local fallbacks instead of breaking the configurator.
        """
        try:
            from splent_framework.services.service_locator import service_proxy

            marketplace_service = service_proxy("MarketplaceService")
            for method_name in ("get_index", "index"):
                method = getattr(marketplace_service, method_name, None)
                if callable(method):
                    index = method()
                    if self._is_index(index):
                        return index
        except Exception:
            return None
        return None

    def _cache_path(self) -> Path:
        workspace = os.getenv(WORKSPACE_ENV, DEFAULT_WORKSPACE)
        return Path(workspace) / ".splent_cache" / "marketplace" / "index.json"

    def _index_from_cache(self) -> dict | None:
        try:
            data = json.loads(self._cache_path().read_text())
        except (OSError, ValueError):
            return None
        return data if self._is_index(data) else None

    def _index_from_url(self) -> dict | None:
        url = os.getenv(INDEX_URL_ENV)
        if not url:
            return None
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url, headers={"User-Agent": "splent-feature-configurator"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, ValueError):
            return None
        if not self._is_index(data):
            return None
        try:  # best-effort: next request is instant and works offline
            path = self._cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return data

    @staticmethod
    def _is_index(data) -> bool:
        return isinstance(data, dict) and data.get("schema") == INDEX_SCHEMA

    # ── SPL queries ───────────────────────────────────────────────────

    def list_spls(self) -> list[dict]:
        """SPL summaries for the chooser page."""
        index = self.get_index() or {}
        summaries = []
        for spl in index.get("spls", []):
            model = spl.get("model") or {}
            summaries.append(
                {
                    "name": spl.get("name"),
                    "description": spl.get("description", ""),
                    "has_model": bool(model.get("features")),
                    "feature_count": len(model.get("features") or {}),
                }
            )
        return summaries

    def get_spl(self, name: str) -> dict | None:
        index = self.get_index() or {}
        for spl in index.get("spls", []):
            if spl.get("name") == name:
                return spl
        return None

    def index_feature_shorts(self) -> set[str]:
        """Short names of the features published in the marketplace index.

        An SPL model may reference features that are not published in the
        index (e.g. only available on GitHub/PyPI); callers use this set to
        mark them as *external* instead of linking to a missing detail page.
        """
        index = self.get_index() or {}
        return {
            feature.get("short")
            for feature in index.get("features") or []
            if isinstance(feature, dict) and feature.get("short")
        }

    def feature_tree(self, model: dict, known_shorts: set[str] | None = None) -> dict:
        """Shape the flat model into what the configurator page renders.

        Returns ``{"mandatory": [...], "optional": [...], "groups": [...]}``
        where each feature dict carries ``short`` plus its index attributes,
        and each group carries ``owner``, ``kind``, ``members`` and
        ``required`` (True when its members sit under ``mandatory``).

        When ``known_shorts`` is given, every feature also carries
        ``external``: True for model features absent from the index.
        """
        features = model.get("features") or {}

        def entry(short: str) -> dict:
            info = {"short": short, **features[short]}
            if known_shorts is not None:
                info["external"] = short not in known_shorts
            return info

        groups = []
        grouped: set[str] = set()
        for group in model.get("alternative_groups") or []:
            members = [m for m in group.get("members", []) if m in features]
            grouped.update(members)
            groups.append(
                {
                    "owner": group.get("owner"),
                    "kind": group.get("kind", "alternative"),
                    "members": [entry(m) for m in members],
                    "required": any(
                        features[m].get("presence") == "mandatory" for m in members
                    ),
                }
            )
        mandatory, optional = [], []
        for short, info in features.items():
            if short in grouped:
                continue
            bucket = mandatory if info.get("presence") == "mandatory" else optional
            bucket.append(entry(short))
        return {"mandatory": mandatory, "optional": optional, "groups": groups}

    # ── Variability validation (own logic, no solver, no network) ─────

    def validate_selection(self, model: dict, selected: list[str]) -> dict:
        """Validate ``selected`` against the SPL model.

        Applies, in order:

        1. Mandatory features outside alternative groups are always part of
           the product (auto-added when missing).
        2. Transitive closure of the requires-constraints ``a => b``
           (auto-adds every implied feature).
        3. Alternative groups: an ``alternative`` group admits at most one
           member; a required group (members under ``mandatory``) admits no
           empty choice.

        Returns ``{"selection_valid", "selection", "auto_added",
        "violations"}`` where violations are structured dicts
        (``code`` + context) so callers can translate them.
        """
        features = model.get("features") or {}
        violations: list[dict] = []
        auto_added: list[str] = []
        chosen: set[str] = set()

        for short in selected or []:
            if short in features:
                chosen.add(short)
            else:
                violations.append({"code": "unknown_feature", "feature": short})

        grouped = {
            member
            for group in model.get("alternative_groups") or []
            for member in group.get("members", [])
        }

        # 1. mandatory features (outside groups) are always included
        for short, info in features.items():
            if (
                info.get("presence") == "mandatory"
                and short not in grouped
                and short not in chosen
            ):
                chosen.add(short)
                auto_added.append(short)

        # 2. transitive closure of a => b constraints
        constraints = [
            pair for pair in (model.get("constraints") or []) if len(pair) == 2
        ]
        for a, b in constraints:
            if b not in features:
                violations.append(
                    {"code": "unknown_constraint_target", "feature": b, "required_by": a}
                )
        applicable = [(a, b) for a, b in constraints if b in features]
        changed = True
        while changed:
            changed = False
            for a, b in applicable:
                if a in chosen and b not in chosen:
                    chosen.add(b)
                    auto_added.append(b)
                    changed = True

        # 3. alternative groups
        for group in model.get("alternative_groups") or []:
            members = [m for m in group.get("members", []) if m in features]
            chosen_members = [m for m in members if m in chosen]
            required = any(
                features[m].get("presence") == "mandatory" for m in members
            )
            if group.get("kind", "alternative") == "alternative" and len(chosen_members) > 1:
                violations.append(
                    {
                        "code": "group_conflict",
                        "group": group.get("owner"),
                        "features": chosen_members,
                    }
                )
            if required and not chosen_members:
                violations.append(
                    {
                        "code": "group_required",
                        "group": group.get("owner"),
                        "features": members,
                    }
                )

        return {
            "selection_valid": not violations,
            # final selection in model declaration order (mandatory first)
            "selection": [s for s in features if s in chosen],
            "auto_added": auto_added,
            "violations": violations,
        }

    # ── Command generation ────────────────────────────────────────────

    def commands_for(
        self, spl_name: str, model: dict, selection: list[str], product_name: str
    ) -> list[str]:
        """The exact splent commands that build the configured product."""
        features = model.get("features") or {}
        name = self.normalize_product_name(product_name)
        commands = [
            f"splent product:create {name} --spl {spl_name}",
            f"splent product:select {name}",
        ]
        for short in selection:
            info = features.get(short) or {}
            package = info.get("package")
            if not package:
                continue
            org = info.get("org") or DEFAULT_ORG
            # feature:install clones the repo when it is not in the workspace
            # yet (feature:add only registers already-cloned editable repos),
            # so the recipe works for marketplace users starting from scratch.
            commands.append(f"splent feature:install {org}/{package}")
        commands.append("splent product:resolve")
        commands.append("splent product:derive --dev")
        return commands

    @staticmethod
    def normalize_product_name(name: str) -> str:
        slug = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
        return slug or DEFAULT_PRODUCT_NAME

    # ── Entry point used by the routes ────────────────────────────────

    def validate(
        self, spl_name: str, selected: list[str], product_name: str = ""
    ) -> dict | None:
        """Full validation for one SPL. ``None`` when the SPL is unknown."""
        spl = self.get_spl(spl_name)
        model = (spl or {}).get("model") or {}
        if not model.get("features"):
            return None
        result = self.validate_selection(model, selected)
        result["commands"] = (
            self.commands_for(spl_name, model, result["selection"], product_name)
            if result["selection_valid"]
            else []
        )
        return result
