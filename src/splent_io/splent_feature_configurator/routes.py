from flask import abort, jsonify, render_template, request
from flask_babel import gettext as _

from splent_io.splent_feature_configurator import configurator_bp
from splent_framework.services.service_locator import service_proxy

configurator_service = service_proxy("ConfiguratorService")


def _violation_message(violation: dict) -> str:
    """Translate a structured violation into a human-readable message."""
    code = violation.get("code")
    features = ", ".join(violation.get("features", []))
    if code == "unknown_feature":
        return _(
            "Feature '%(feature)s' does not exist in this SPL",
            feature=violation.get("feature"),
        )
    if code == "unknown_constraint_target":
        return _(
            "'%(required_by)s' requires '%(feature)s', which is not in this SPL model",
            required_by=violation.get("required_by"),
            feature=violation.get("feature"),
        )
    if code == "group_conflict":
        return _(
            "Group '%(group)s' is exclusive: choose only one of %(features)s",
            group=violation.get("group"),
            features=features,
        )
    if code == "group_required":
        return _(
            "Group '%(group)s' needs a choice: select one of %(features)s",
            group=violation.get("group"),
            features=features,
        )
    return str(code)


@configurator_bp.route("/configurator", methods=["GET"])
def index():
    spls = configurator_service.list_spls()
    return render_template("configurator/index.html", spls=spls)


@configurator_bp.route("/configurator/<spl_name>", methods=["GET"])
def configure(spl_name):
    spl = configurator_service.get_spl(spl_name)
    model = (spl or {}).get("model") or {}
    if not model.get("features"):
        abort(404)
    tree = configurator_service.feature_tree(model)
    return render_template("configurator/configure.html", spl=spl, tree=tree)


@configurator_bp.route("/configurator/<spl_name>/validate", methods=["POST"])
def validate_selection(spl_name):
    payload = request.get_json(silent=True) or {}
    selected = payload.get("selected") or []
    product_name = payload.get("product_name") or ""
    result = configurator_service.validate(spl_name, selected, product_name)
    if result is None:
        abort(404)
    for violation in result["violations"]:
        violation["message"] = _violation_message(violation)
    return jsonify(result)
