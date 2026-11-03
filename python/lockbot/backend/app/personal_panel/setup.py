"""Enable the dashboard only when the operator supplies a reviewed local config."""

import importlib.util
import json
import os
from pathlib import Path


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
        path = Path(item["file"]).resolve(strict=True)
        spec = importlib.util.spec_from_file_location(f"lockbot_reporter_{index}", path)
        if spec is None or spec.loader is None:
            raise ValueError("Invalid personal panel reporter module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reporters.append(module.create_reporter(item["config"]))
    app.mount("/me", create_panel(config["origin"], config["state_file"], provider, reporters))
