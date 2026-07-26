from splent_framework.blueprints.base_blueprint import create_blueprint
from splent_framework.nav.nav_registry import register_nav_item
from splent_framework.services.service_locator import register_service

from splent_io.splent_feature_configurator.services import ConfiguratorService

configurator_bp = create_blueprint(__name__)


def init_feature(app):
    register_service(app, "ConfiguratorService", ConfiguratorService)
    # Public storefront navigation entry (composed per-derivation by the theme).
    register_nav_item(
        key="configurator", label="Configurator", href="/configurator", order=30
    )


def inject_context_vars(app):
    return {}
