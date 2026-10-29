"""Persisted site policy is private, scoped, and shared by both Feishu transports."""

from types import SimpleNamespace

import pytest
from lockbot.backend.app.bots.encryption import encrypt
from lockbot.backend.app.feishu.proxy_settings import restore_proxy
from lockbot.core.platforms.feishu import FeishuClient
from lockbot.core.platforms.feishu_proxy import configure, http_options, proxy_state, sdk_proxy_override


@pytest.fixture(autouse=True)
def reset_policy():
    configure()
    yield
    configure()


def test_existing_http_client_observes_policy_and_direct_bypasses_ambient(monkeypatch):
    monkeypatch.setenv("LOCKBOT_FEISHU_PROXY_URL", "http://deployment.example:8188")
    monkeypatch.setenv("https_proxy", "http://other.example:9999")
    client = FeishuClient("cli_fixture", "fictional")
    assert client._http_options["proxies"]["https"] == "http://deployment.example:8188"
    configure("custom", "http://site.example:3128/")
    assert client._http_options["proxies"]["https"] == "http://site.example:3128"
    assert sdk_proxy_override() == "http://site.example:3128"
    configure("direct")
    assert client._http_options == {"proxies": {"http": "", "https": ""}}
    assert sdk_proxy_override() == ""
    import os

    assert os.environ["https_proxy"] == "http://other.example:9999"


def test_site_policy_authorization_atomic_validation_restore_and_effective_restart(
    client, admin_header, auth_header, db_session, monkeypatch
):
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.feishu.connectors import connectors

    monkeypatch.setenv("LOCKBOT_FEISHU_PROXY_URL", "http://deployment.example:8188")
    restarts = []
    monkeypatch.setattr(connectors, "reconfigure", lambda **kwargs: restarts.append(kwargs))
    body = {"settings": {"feishu_proxy_mode": "custom", "feishu_proxy_url": "http://site.example:3128"}}
    endpoint = "/api/admin/settings"
    assert client.put(endpoint, json=body).status_code == 401
    assert client.put(endpoint, headers=auth_header, json=body).status_code == 403
    assert client.put(endpoint, headers=admin_header, json=body).status_code == 403
    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.commit()
    for path in ("/api/admin/feishu/proxy", "/api/admin/feishu/proxy/test"):
        request = client.post if path.endswith("test") else client.get
        assert request(path, headers=auth_header).status_code == 403
    result = client.put(endpoint, headers=admin_header, json=body)
    assert result.status_code == 200 and result.json()["feishu_proxy_status"] == "connection_pending"
    assert len(restarts) == 1
    assert client.put(endpoint, headers=admin_header, json=body).json()["feishu_proxy_status"] == "unchanged"
    assert len(restarts) == 1
    public = client.get("/api/settings").json()
    assert not any("proxy" in item["key"] for item in public)
    assert "site.example" not in str(public)
    assert client.get("/api/admin/feishu/proxy", headers=admin_header).json()["source"] == "site"
    for mode, url in [("unknown", ""), ("custom", ""), ("custom", "http://user:secret@proxy")]:
        rejected = client.put(
            endpoint, headers=admin_header, json={"settings": {"feishu_proxy_mode": mode, "feishu_proxy_url": url}}
        )
        assert rejected.status_code == 422 and "secret" not in rejected.text
    configure("direct")
    restore_proxy(db_session)
    assert proxy_state()["effective_url"] == "http://site.example:3128"
    assert http_options()["proxies"]["https"] == sdk_proxy_override()
    result = client.put(endpoint, headers=admin_header, json={"settings": {"feishu_proxy_mode": "deployment"}})
    assert result.status_code == 200
    assert proxy_state()["effective_url"] == "http://deployment.example:8188"
    assert client.post("/api/admin/feishu/proxy/test", headers=admin_header).json() == {
        "http": "no_application",
        "websocket": "not_tested",
    }


def test_reconfigure_stops_every_old_receiver_before_any_new_start(monkeypatch):
    from lockbot.backend.app.feishu.connectors import ConnectorManager

    order = []
    manager = ConnectorManager()
    manager._connectors = {
        1: SimpleNamespace(stop=lambda: order.append("stop1"), thread=SimpleNamespace(is_alive=lambda: False)),
        2: SimpleNamespace(stop=lambda: order.append("stop2"), thread=SimpleNamespace(is_alive=lambda: False)),
    }
    monkeypatch.setattr(manager, "start", lambda **kw: order.append("start"))
    manager.reconfigure()
    assert order == ["stop1", "stop2", "start"]
    manager._connectors = {1: SimpleNamespace(stop=lambda: None, thread=SimpleNamespace(is_alive=lambda: True))}
    with pytest.raises(RuntimeError, match="not stopped"):
        manager.reconfigure()
    assert order == ["stop1", "stop2", "start"]


def test_direct_sdk_disables_both_environment_discovery_paths():
    import asyncio

    from lockbot.backend.app.feishu.ws_proxy import configure_sdk_proxy

    calls = []

    async def connect(uri, *, proxy="environment", **kwargs):
        calls.append(proxy)
        return object()

    sdk = SimpleNamespace(
        websockets=SimpleNamespace(connect=connect), requests=SimpleNamespace(post=lambda *a, **kw: calls.append(kw))
    )
    configure_sdk_proxy(sdk, proxy_override="")
    asyncio.run(sdk.websockets.connect("wss://example.test/events"))
    sdk.requests.post("https://open.feishu.cn/callback/ws/endpoint")
    assert calls[0] is None
    assert calls[1]["proxies"] == {"http": "", "https": ""}


def test_http_probe_uses_fixed_client_after_closing_database_transaction(client, admin_header, db_session, monkeypatch):
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.feishu.models import FeishuApplication

    admin = db_session.query(User).filter_by(username="adminuser").one()
    admin.role = "super_admin"
    db_session.add(
        FeishuApplication(
            app_id="cli_fixture",
            name="fixture",
            tenant_key="tenant",
            bot_open_id="ou_bot",
            app_secret=encrypt("not-read"),
            verification_token=encrypt("not-read"),
        )
    )
    db_session.commit()
    probes = []
    from sqlalchemy.orm import object_session

    def selected_client(app):
        app_id, session = app.app_id, object_session(app)

        def probe():
            assert not session.in_transaction()
            probes.append(app_id)

        return SimpleNamespace(bot_identity=probe)

    monkeypatch.setattr("lockbot.backend.app.feishu.service.application_client", selected_client)
    result = client.post(
        "/api/admin/feishu/proxy/test", headers=admin_header, json={"url": "http://ignored.example/private"}
    )
    assert result.json() == {"http": "ok", "websocket": "not_tested"}
    assert probes == ["cli_fixture"]
