"""Actual private loopback service/consumer acceptance, not a live deployment test."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "posix", reason="The isolated service inherits a pre-bound private socket")
def test_actual_http_frozen_consumer_and_restart_unknown(tmp_path):
    script = Path(__file__).parent / "integration/v2_consumer_http_probe.py"
    output = tmp_path / "sanitized-proof.json"
    result = subprocess.run(
        [sys.executable, str(script), "--output", str(output)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-3000:]
    proof = json.loads(output.read_text())
    for field in (
        "real_loopback_http",
        "no_feishu_application_configured",
        "platform_delivery_stubbed",
        "frozen_as_of_checkpoint_preserved",
        "late_append_excluded",
        "grant_renew_book_release_present",
        "initial_report_verified",
        "public_projection",
        "real_scheduler_expiry",
        "independently_checked_commit_interval",
        "waiting_actor_has_no_grant_time",
        "real_cli_verified_report",
        "restart_identity_preserved",
        "restart_report_unknown",
        "real_cli_restart_unknown",
        "private_fixture_removed",
    ):
        assert proof[field] is True, field
    assert proof["frozen_pages"] >= 2 and proof["synthetic_resource_commands"] == 5
    assert proof["production_requests"] == proof["sdk_receivers_started"] == 0
    assert proof["external_connection_attempts"] == proof["consumer_external_connections"] == 0
    assert proof["surviving_child_processes"] == 0


@pytest.mark.parametrize("built_frontend", [False, True])
def test_identity_seam_is_independent_of_frontend_artifacts(tmp_path, monkeypatch, built_frontend):
    import importlib.util

    from fastapi.testclient import TestClient
    from lockbot.backend.app.main import create_app
    from lockbot.backend.app.resource_auth import service
    from starlette.routing import Mount

    source = Path(__file__).parent / "integration/v2_consumer_http_probe.py"
    spec = importlib.util.spec_from_file_location("v2_probe_static_regression", source)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    configured = tmp_path / "frontend-dist"
    if built_frontend:
        configured.mkdir()
        (configured / "index.html").write_text("<html>Built frontend fixture</html>")
    monkeypatch.setenv("FRONTEND_DIST", str(configured))
    # Establish that the same create_app inputs really reproduce a root SPA mount.
    control = create_app()
    assert any(isinstance(route, Mount) and route.path == "" for route in control.routes) == built_frontend
    calls = []
    monkeypatch.setattr(service, "handle_chat_binding", lambda **kwargs: calls.append(kwargs))
    app = probe.fixture_app(tmp_path / "explicitly-disabled-frontend")
    assert not any(isinstance(route, Mount) and route.path == "" for route in app.routes)
    # The temporary override must not mutate the caller's frontend configuration.
    assert os.environ["FRONTEND_DIST"] == str(configured)
    response = TestClient(app).post("/_fixture/bind", json={"bot_id": 7, "actor": "synthetic-user", "code": "CODE"})
    assert response.status_code == 200 and response.json() == {"bound": True}
    assert calls == [
        {
            "platform": "infoflow",
            "external_user_id": "synthetic-user",
            "bot_id": 7,
            "action": "bind",
            "argument": "CODE",
        }
    ]
