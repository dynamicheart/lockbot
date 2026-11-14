"""Report discovery and one-extension reads are limited to the current binding."""

# ruff: noqa: F811
from datetime import datetime

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.personal_panel.agent_reports import PREFIX, Extension, mount_agent_reports
from lockbot.backend.app.personal_panel.native import NativeProvider
from lockbot.backend.app.resource_api.discovery import ensure_instance_id
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_resource_api_resources import api  # noqa: F401


class Reporter:
    def __init__(self):
        self.calls = 0
        self.result = [
            {
                "group_id": "local-internal-identifier",
                "label": "Test group",
                "status": "ok",
                "private_detail": "discard this",
                "allocations": [{"kind": "hold", "node": "node1", "purpose": "discard this"}],
            }
        ]

    def report(self, grant):
        assert grant["subject"] == "alice"
        self.calls += 1
        return self.result


@pytest.fixture
def extension(client, api, db_session, session_factory):
    origin = str(client.base_url).rstrip("/")
    ensure_instance_id(db_session)
    bot = db_session.get(Bot, api["bot_id"])
    bot.status = "running"
    db_session.commit()
    provider = NativeProvider({"issuer": origin, "session_factory": session_factory})
    grant = provider.authenticate(api["headers"]["Authorization"][7:])
    reporter = Reporter()
    item = Extension.configured(
        {"id": "team-report", "revision": "1", "label": "Test report", "grants": [grant]},
        reporter,
    )
    mount_agent_reports(client.app, origin, provider, [item])
    try:
        yield reporter
    finally:
        client.app.state.agent_personal_reports.close()
        del client.app.state.agent_personal_reports
        del client.app.state.agent_personal_report_origin
        client.app.router.routes.pop()


def test_only_current_binding_discovers_and_reads_closed_projection(client, api, extension):
    field = "personal_report_extensions"
    assert field not in client.get("/api/agent-skill").json()
    own = client.get("/api/agent/context", headers=api["headers"])
    assert own.status_code == 200 and own.headers["cache-control"] == "no-store"
    metadata = own.json()[field]
    assert metadata["items"][0]["mode"] == "read_only"
    path = metadata["items"][0]["read_path"]
    assert path == PREFIX + "team-report"
    foreign = client.get("/api/agent/context", headers=api["other_headers"])
    assert field not in foreign.json()
    assert client.get(path, headers=api["other_headers"]).status_code == 404
    assert client.get(path, headers=api["auth_header"]).status_code == 401
    assert client.get(path + "?subject=alice", headers=api["headers"]).status_code == 404
    assert client.post(path, headers=api["headers"]).status_code == 405
    assert extension.calls == 0
    read = client.get(path, headers=api["headers"])
    assert read.status_code == 200 and read.headers["cache-control"] == "no-store"
    assert read.json()["groups"][0]["allocations"] == [
        {"kind": "hold", "node": "node1", "device": None, "expires_at": None},
    ]
    assert "local-internal-identifier" not in read.text
    assert "discard this" not in read.text
    assert extension.calls == 1


def test_reports_distinguish_unknown_from_verified_empty(client, api, extension):
    path = PREFIX + "team-report"
    extension.result[0]["source_at"] = "2000-01-01T00:00:00Z"
    stale = client.get(path, headers=api["headers"]).json()
    assert stale["status"] == "unknown" and stale["groups"][0]["allocations"] == []
    extension.result = []
    assert client.get(path, headers=api["headers"]).json()["status"] == "ok"
    extension.result = [{}]
    result = client.get(path, headers=api["headers"]).json()
    assert result["status"] == "unknown" and result["groups"] == []


def test_stopped_and_revoked_binding_cannot_read_extension(client, api, extension, db_session):
    path = PREFIX + "team-report"
    bot = db_session.get(Bot, api["bot_id"])
    bot.status = "stopped"
    db_session.commit()
    assert client.get(path, headers=api["headers"]).status_code == 404
    assert "personal_report_extensions" not in client.get("/api/agent/context", headers=api["headers"]).json()
    row = db_session.query(ResourceBinding).filter_by(external_user_id="alice").one()
    row.revoked_at = datetime.utcnow()
    db_session.commit()
    assert client.get(path, headers=api["headers"]).status_code == 401
    assert extension.calls == 0


def test_revocation_during_report_discards_inflight_result(client, api, extension, db_session, monkeypatch):
    reports = client.app.state.agent_personal_reports
    provider = reports.provider
    original = provider.authenticate
    changed = False

    def authenticate(key):
        nonlocal changed
        if changed:
            db_session.query(ResourceBinding).filter_by(external_user_id="alice").update(
                {ResourceBinding.revoked_at: datetime.utcnow()}
            )
            db_session.commit()
        changed = True
        return original(key)

    monkeypatch.setattr(provider, "authenticate", authenticate)
    response = client.get(PREFIX + "team-report", headers=api["headers"])
    assert extension.calls == 1
    assert response.status_code == 401
    assert "node1" not in response.text


def test_mount_loads_only_explicit_adapters(api, db_session, session_factory, tmp_path, monkeypatch):
    import json

    from fastapi import FastAPI
    from lockbot.backend.app.personal_panel.setup import mount_if_configured

    origin = "https://fixture.invalid"
    ensure_instance_id(db_session)
    provider = NativeProvider({"issuer": origin, "session_factory": session_factory})
    grant = provider.authenticate(api["headers"]["Authorization"][7:])
    module = tmp_path / "reporter.py"
    module.write_text(
        "class Reporter:\n"
        "    def report(self, grant):\n"
        "        return []\n\n"
        "def create_reporter(config):\n"
        "    return Reporter()\n"
    )
    config = tmp_path / "panel-config.json"
    config.write_text(
        json.dumps(
            {
                "origin": origin,
                "state_file": str(tmp_path / "panel.sqlite3"),
                "provider": {"issuer": origin},
                "agent_report_extensions": [
                    {
                        "id": "team-report",
                        "label": "Fixture",
                        "revision": "1",
                        "grants": [grant],
                        "file": str(module),
                        "config": {},
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("LOCKBOT_PERSONAL_PANEL_CONFIG", str(config))
    monkeypatch.setattr("lockbot.backend.app.personal_panel.setup._home", "")
    app = FastAPI()
    mount_if_configured(app)
    try:
        assert app.state.agent_personal_reports.discover(grant)["items"][0]["read_path"] == PREFIX + "team-report"
    finally:
        app.state.agent_personal_reports.close()
