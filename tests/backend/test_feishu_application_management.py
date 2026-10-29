"""Application management uses fake credentials and never contacts a real tenant."""

import threading
from types import SimpleNamespace

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.database import get_db
from lockbot.backend.app.feishu.connectors import ConnectorManager, connectors
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute, FeishuReceivedMessage
from lockbot.backend.app.feishu.service import application_client
from lockbot.core.platforms.feishu import FeishuAPIError, FeishuClient
from sqlalchemy.exc import OperationalError


@pytest.fixture
def application(client, auth_header, db_session, monkeypatch):
    monkeypatch.setattr(FeishuClient, "bot_identity", lambda self: "ou_fixture_bot")
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "super_admin"
    db_session.commit()
    response = client.post(
        "/api/feishu/applications",
        headers=auth_header,
        json={
            "name": "Fixture",
            "app_id": "cli_fixture",
            "app_secret": "FAKE_OLD_SECRET",
            "transport": "http",
            "tenant_key": "fixture_tenant",
            "verification_token": "FAKE_VERIFY",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_reveal_is_explicit_audited_and_never_in_list(client, auth_header, db_session, application):
    path = f"/api/feishu/applications/{application}"
    assert "FAKE_OLD_SECRET" not in client.get("/api/feishu/applications", headers=auth_header).text
    assert client.get(path + "/reveal", headers=auth_header).status_code in (404, 405)
    result = client.post(path + "/reveal", headers=auth_header)
    assert result.status_code == 200
    assert result.json() == {"app_secret": "FAKE_OLD_SECRET"}
    assert result.headers["cache-control"] == "no-store"
    audit = db_session.query(AuditLog).filter_by(action="feishu.reveal_secret").one()
    assert audit.target_id == application and audit.detail is None
    assert "FAKE_OLD_SECRET" not in str(audit.__dict__)


def test_update_keeps_identity_encrypted_and_refreshes_retained_clients(client, auth_header, db_session, application):
    row = db_session.get(FeishuApplication, application)
    retained = application_client(row)
    from lockbot.backend.app.feishu import service

    # Eviction cannot leave a running bot's retained client using the old credential.
    with service._clients_lock:
        for key in list(service._clients):
            if key[0] == row.app_id:
                service._clients.pop(key)
    retained._token, retained._expires = "fake-old-token", 9999999999
    original = (row.app_id, row.tenant_key, row.bot_open_id, row.transport, row.verification_token)
    path = f"/api/feishu/applications/{application}"
    assert client.patch(path, headers=auth_header, json={"name": "Renamed", "app_id": "cli_other"}).status_code == 422
    result = client.patch(path, headers=auth_header, json={"name": "Renamed", "app_secret": "FAKE_NEW_SECRET"})
    assert result.status_code == 200 and "SECRET" not in result.text
    db_session.refresh(row)
    assert row.name == "Renamed" and row.app_secret != "FAKE_NEW_SECRET"
    assert encryption.decrypt(row.app_secret) == "FAKE_NEW_SECRET"
    assert original == (row.app_id, row.tenant_key, row.bot_open_id, row.transport, row.verification_token)
    assert retained._secret == "FAKE_NEW_SECRET" and retained._token == "" and retained._expires == 0
    stale = SimpleNamespace(app_id=row.app_id, app_secret=encryption.encrypt("FAKE_OLD_SECRET"))
    with pytest.raises(FeishuAPIError, match="credentials changed"):
        application_client(stale)


def test_all_management_routes_reject_admin_and_anonymous(client, auth_header, db_session, application):
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "admin"
    db_session.commit()
    path = f"/api/feishu/applications/{application}"
    for headers in ({}, auth_header):
        expected = 401 if not headers else 403
        assert client.patch(path, headers=headers, json={"name": "Denied"}).status_code == expected
        assert client.post(path + "/reveal", headers=headers).status_code == expected
        assert client.delete(path, headers=headers).status_code == expected


def test_delete_refuses_routes_and_pending_claims_then_removes_settled_claims(
    client, auth_header, db_session, application
):
    path = f"/api/feishu/applications/{application}"
    route = FeishuGroupRoute(application_id=application, chat_id="oc_fixture", bot_id=999999)
    db_session.add(route)
    db_session.commit()
    assert client.delete(path, headers=auth_header).status_code == 409
    db_session.delete(route)
    db_session.add(
        FeishuReceivedMessage(
            application_id=application, message_id="fixture_claim", bot_id=999999, identity_revision=0, state="claimed"
        )
    )
    db_session.commit()
    assert client.delete(path, headers=auth_header).status_code == 409
    claim = db_session.get(FeishuReceivedMessage, (application, "fixture_claim"))
    claim.state = "completed"
    db_session.commit()
    result = client.delete(path, headers=auth_header)
    assert result.status_code == 204 and result.headers["cache-control"] == "no-store"
    db_session.expire_all()
    assert db_session.get(FeishuApplication, application) is None
    assert db_session.get(FeishuReceivedMessage, (application, "fixture_claim")) is None


def test_application_change_stops_only_target_and_fails_closed():
    manager = ConnectorManager()
    stopped = []
    manager._connectors = {
        1: SimpleNamespace(stop=lambda: stopped.append(1), thread=SimpleNamespace(is_alive=lambda: False)),
        2: SimpleNamespace(stop=lambda: stopped.append(2), thread=SimpleNamespace(is_alive=lambda: True)),
    }
    with manager.application_change(1):
        assert 2 in manager._connectors
    assert stopped == [1] and 1 not in manager._connectors
    with pytest.raises(RuntimeError), manager.application_change(2):
        pytest.fail("An unstopped receiver must not permit mutation")
    assert 2 in manager._connectors


def test_rotation_rejects_changed_bot_identity_and_empty_secret(
    client, auth_header, db_session, application, monkeypatch
):
    path = f"/api/feishu/applications/{application}"
    assert client.patch(path, headers=auth_header, json={"name": "Invalid", "app_secret": " "}).status_code == 422
    monkeypatch.setattr(FeishuClient, "bot_identity", lambda self: "ou_foreign")
    assert (
        client.patch(path, headers=auth_header, json={"name": "Invalid", "app_secret": "FAKE_FOREIGN"}).status_code
        == 409
    )
    db_session.expire_all()
    assert encryption.decrypt(db_session.get(FeishuApplication, application).app_secret) == "FAKE_OLD_SECRET"


def test_concurrent_credential_change_is_refused_before_receiver_stop(
    client, auth_header, db_session, application, monkeypatch
):
    calls = []
    fake = SimpleNamespace(stop=lambda: calls.append("stop"), thread=SimpleNamespace(is_alive=lambda: False))
    monkeypatch.setattr(connectors, "_connectors", {application: fake})

    def verify(self):
        db_session.get(FeishuApplication, application).app_secret = encryption.encrypt("FAKE_CONCURRENT")
        db_session.commit()
        return "ou_fixture_bot"

    monkeypatch.setattr(FeishuClient, "bot_identity", verify)
    result = client.patch(
        f"/api/feishu/applications/{application}",
        headers=auth_header,
        json={"name": "Stale", "app_secret": "FAKE_REPLACEMENT"},
    )
    assert result.status_code == 409 and calls == []
    db_session.expire_all()
    assert encryption.decrypt(db_session.get(FeishuApplication, application).app_secret) == "FAKE_CONCURRENT"


def test_unstopped_receiver_prevents_delete_and_secret_change(
    client, auth_header, db_session, application, monkeypatch
):
    fake = SimpleNamespace(stop=lambda: None, thread=SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(connectors, "_connectors", {application: fake})
    path = f"/api/feishu/applications/{application}"
    assert client.delete(path, headers=auth_header).status_code == 503
    assert (
        client.patch(path, headers=auth_header, json={"name": "Unchanged", "app_secret": "FAKE_NEW"}).status_code == 503
    )
    db_session.expire_all()
    row = db_session.get(FeishuApplication, application)
    assert row is not None and row.name == "Fixture"
    assert encryption.decrypt(row.app_secret) == "FAKE_OLD_SECRET"


def test_reveal_fails_closed_when_audit_cannot_be_written(client, auth_header, application, monkeypatch):
    from lockbot.backend.app.feishu import router

    monkeypatch.setattr(
        router, "_application_audit", lambda *args: (_ for _ in ()).throw(RuntimeError("audit unavailable"))
    )
    # The test client re-raises server errors: no successful response with a Secret is returned.
    with pytest.raises(RuntimeError, match="audit unavailable"):
        client.post(f"/api/feishu/applications/{application}/reveal", headers=auth_header)


def test_rotation_stops_old_receiver_before_commit_and_starts_only_target(
    client, auth_header, db_session, application, monkeypatch
):
    row = db_session.get(FeishuApplication, application)
    row.transport = "websocket"
    db_session.commit()
    calls = []
    fake = SimpleNamespace(stop=lambda: calls.append("stop"), thread=SimpleNamespace(is_alive=lambda: False))
    monkeypatch.setattr(connectors, "_connectors", {application: fake})

    def connect(identifier, **kwargs):
        assert identifier == application and calls == ["stop"]
        db_session.expire_all()
        assert encryption.decrypt(db_session.get(FeishuApplication, identifier).app_secret) == "FAKE_NEW_SECRET"
        calls.append("connect")

    monkeypatch.setattr(connectors, "connect", connect)
    result = client.patch(
        f"/api/feishu/applications/{application}",
        headers=auth_header,
        json={"name": "Rotated", "app_secret": "FAKE_NEW_SECRET"},
    )
    assert result.status_code == 200 and calls == ["stop", "connect"]


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_commit_failure_restarts_only_original_persisted_receiver_without_holding_read_transaction(
    client, auth_header, db_session, application, monkeypatch, method
):
    row = db_session.get(FeishuApplication, application)
    row.transport = "websocket"
    db_session.commit()
    current = []
    original_dependency = client.app.dependency_overrides[get_db]

    def tracked_db():
        for session in original_dependency():
            current.append(session)
            monkeypatch.setattr(session, "commit", lambda: (_ for _ in ()).throw(OperationalError("fixture", {}, None)))
            yield session

    client.app.dependency_overrides[get_db] = tracked_db
    calls = []

    def stop():
        assert not current[-1].in_transaction()
        calls.append("stop")

    monkeypatch.setattr(
        connectors,
        "_connectors",
        {
            application: SimpleNamespace(stop=stop, thread=SimpleNamespace(is_alive=lambda: False)),
        },
    )

    def connect(identifier, **kwargs):
        assert identifier == application
        with kwargs["factory"]() as fresh:
            persisted = fresh.get(FeishuApplication, identifier)
            assert encryption.decrypt(persisted.app_secret) == "FAKE_OLD_SECRET"
            assert persisted.name == "Fixture"
        calls.append("recover")

    monkeypatch.setattr(connectors, "connect", connect)
    with pytest.raises(OperationalError):
        if method == "patch":
            client.patch(
                f"/api/feishu/applications/{application}",
                headers=auth_header,
                json={"name": "Not committed", "app_secret": "FAKE_UNCOMMITTED"},
            )
        else:
            client.delete(f"/api/feishu/applications/{application}", headers=auth_header)
    assert calls == ["stop", "recover"]


def test_restore_holds_same_application_guard_through_commit(monkeypatch):
    from lockbot.backend.app.backup import service as backup

    acquired = threading.Event()
    blocked = []
    threads = []

    def attempt():
        with connectors.application_guard():
            acquired.set()

    def restore(*args):
        thread = threading.Thread(target=attempt)
        threads.append(thread)
        thread.start()
        blocked.append(not acquired.wait(0.05))
        return {"restored": 1}

    monkeypatch.setattr(backup, "_restore_apply", restore)
    assert backup.restore_apply(b"fixture", None, None, [], 1, SimpleNamespace(rollback=lambda: None)) == {
        "restored": 1
    }
    for thread in threads:
        thread.join(timeout=1)
    assert blocked == [True] and acquired.is_set()
