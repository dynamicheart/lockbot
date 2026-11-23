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
    v2_credentials = config.get("v2_credentials", False)
    if type(v2_credentials) is not bool:
        raise ValueError("v2_credentials must be a boolean")
    native_v2 = None
    if v2_credentials:
        from .native_v2 import NativeV2Provider

        native_v2 = NativeV2Provider(config["origin"])
    if "file" in item:
        provider = _load_plugin(item, "lockbot_identity_provider", "create_provider")
    elif native_v2 is not None:
        if item.get("issuer") != config["origin"]:
            raise ValueError("V2 credentials require the same-origin issuer")
        provider = native_v2
    else:
        provider = NativeProvider(item)
    if "resource_guidance" in config:
        from .guidance import Guidance, mount_agent_guidance

        if type(provider) is not NativeProvider or provider.config["issuer"] != config["origin"]:
            raise ValueError("Resource guidance requires the same-origin native identity provider")
        provider.guidance = Guidance.configured(config["resource_guidance"])
        mount_agent_guidance(app, provider)
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
    extensions = config.get("agent_report_extensions")
    if extensions is not None:
        from .agent_reports import Extension, mount_agent_reports

        if type(provider) is not NativeProvider or provider.config["issuer"] != config["origin"]:
            raise ValueError("Agent report extensions require the same-origin native identity provider")
        if not isinstance(extensions, list) or not 1 <= len(extensions) <= 8:
            raise ValueError("Configure one to eight Agent report extensions")
        mount_agent_reports(
            app,
            config["origin"],
            provider,
            [
                Extension.configured(item, _load_plugin(item, f"lockbot_agent_report_{index}", "create_reporter"))
                for index, item in enumerate(extensions)
            ],
        )
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
    native_handoff = None
    local_key_handoff = config.get("local_key_handoff", False)
    if type(local_key_handoff) is not bool:
        raise ValueError("local_key_handoff must be a boolean")
    if local_key_handoff and native_v2 is not None:
        raise ValueError("Select v2_credentials or local_key_handoff")
    if local_key_handoff:
        native_handoff = NativeProvider({"issuer": config["origin"], "local_key_handoff": True})
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
            native_handoff=native_handoff,
            native_v2=native_v2,
        ),
    )

    _home = config["origin"] + "/#/me"

    @app.get("/me/", include_in_schema=False)
    def legacy_home():
        return FileResponse(Path(__file__).parent / "assets/me.html")

    @app.get("/me/me.js", include_in_schema=False)
    def legacy_script():
        return FileResponse(Path(__file__).parent / "assets/me.js", media_type="application/javascript")
