from flask import url_for
from flask_babel import gettext as _

from splent_framework.hooks.template_hooks import register_template_hook


def configurator_sidebar_link():
    return (
        '<li class="sidebar-item">'
        f'<a class="sidebar-link" href="{url_for("configurator.index")}">'
        '<i class="align-middle" data-feather="sliders"></i> '
        f'<span class="align-middle">{_("Configurator")}</span>'
        "</a>"
        "</li>"
    )


register_template_hook("layout.sidebar.top", configurator_sidebar_link)
