"""Enable the dashboard only when the operator supplies a reviewed local config."""

import importlib.util
import json
import os
from pathlib import Path

from fastapi.responses import FileResponse


def _load_plugin(item, name, factory):
    path = Path(item["file"]).resolve(strict=True)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Invalid personal panel plugin module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, factory)(item["config"])


_home = ""


def panel_home():
    return _home if os.environ.get("LOCKBOT_PERSONAL_PANEL_CONFIG") else ""


def mount_if_configured(app):
    global _home
    _home = ""
    config_file = os.environ.get("LOCKBOT_PERSONAL_PANEL_CONFIG")
    if not config_file:
        return
    from .native import NativeProvider
    from .router import create_panel

    config = json.loads(Path(config_file).read_text())
    item = config["provider"]
    provider = (
        _load_plugin(item, "lockbot_identity_provider", "create_provider") if "file" in item else NativeProvider(item)
    )
    if not all(callable(getattr(provider, method, None)) for method in ("authenticate", "validate", "report")):
        raise ValueError("Invalid identity provider")
    reporters = []
    for index, item in enumerate(config.get("reporters", [])):
        reporters.append(_load_plugin(item, f"lockbot_reporter_{index}", "create_reporter"))
    resolver, directory = None, None
    item = config.get("identity_resolver")
    if item is not None:
        directory = item["config"].get("directory")
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("Identity resolver requires a pinned directory")
        resolver = _load_plugin(item, "lockbot_identity_resolver", "create_resolver")
        if not callable(getattr(resolver, "resolve", None)):
            raise ValueError("Invalid identity resolver")
    metadata_adapter, metadata_pins = None, None
    metadata = config.get("connection_metadata")
    if metadata is not None:
        from .metadata import pinned_services

        metadata_pins = pinned_services(
            metadata.get("allowed_service_origins"),
            metadata.get("instance_pins"),
            config.get("allow_private_http", False),
        )
        metadata_adapter = _load_plugin(metadata["adapter"], "lockbot_connection_metadata", "create_metadata_adapter")
        if not callable(getattr(metadata_adapter, "describe_connection", None)):
            raise ValueError("Invalid connection metadata adapter")
    app.mount(
        "/api/personal-panel",
        create_panel(
            config["origin"],
            config["state_file"],
            provider,
            reporters,
            identity_resolver=resolver,
            identity_directory=directory,
            allow_private_http=config.get("allow_private_http", False),
            metadata_adapter=metadata_adapter,
            metadata_pins=metadata_pins,
        ),
    )

    _home = config["origin"] + "/#/me"

    @app.get("/me/", include_in_schema=False)
    def legacy_home():
        return FileResponse(Path(__file__).parent / "assets/me.html")

    @app.get("/me/me.js", include_in_schema=False)
    def legacy_script():
        return FileResponse(Path(__file__).parent / "assets/me.js", media_type="application/javascript")
