"""Enable the dashboard only when the operator supplies a reviewed local config."""

import importlib.util
import json
import os
from pathlib import Path


def _load_plugin(item, name, factory):
    path = Path(item["file"]).resolve(strict=True)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Invalid personal panel plugin module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, factory)(item["config"])


def mount_if_configured(app):
    config_file = os.environ.get("LOCKBOT_PERSONAL_PANEL_CONFIG")
    if not config_file:
        return
    from .native import NativeProvider
    from .router import create_panel

    config = json.loads(Path(config_file).read_text())
    provider = NativeProvider(config["provider"])
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
    app.mount(
        "/me",
        create_panel(
            config["origin"],
            config["state_file"],
            provider,
            reporters,
            identity_resolver=resolver,
            identity_directory=directory,
        ),
    )
