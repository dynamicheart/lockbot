import json
import subprocess
import sys
import time

import pytest
from lockbot.backend.app.audit.models import AuditLog
from lockbot.backend.app.auth.dependencies import create_access_token
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.feishu import registration, router
from lockbot.backend.app.feishu.models import FeishuApplication
from lockbot.core.platforms.feishu import FeishuAPIError, FeishuClient

PATH = "/api/feishu/application-registrations"
SECRET = "synthetic-app-secret"


@pytest.fixture
def super_admin(auth_header, db_session):
    user = db_session.query(User).filter_by(username="testuser").one()
    user.role = "super_admin"
    db_session.commit()
    return auth_header


@pytest.fixture
def worker(monkeypatch):
    messages = [
        {"status": "waiting", "url": "https://accounts.feishu.cn/verify?code=test", "expires_in": 60},
        {"status": "confirmed", "app_id": "cli_fixture", "app_secret": SECRET},
    ]
    processes = []
    popen = subprocess.Popen

    def start(args, **options):
        assert args[-1] == "lockbot.backend.app.feishu.registration_process"
        script = "import sys,time; sys.stdin.readline(); "
        for message in messages:
            script += f"print({json.dumps(message)!r}, flush=True); "
        script += "time.sleep(30)"
        process = popen([sys.executable, "-c", script], **options)
        processes.append(process)
        return process

    monkeypatch.setattr(registration.subprocess, "Popen", start)
    monkeypatch.setattr(router, "find_spec", lambda _: True)
    yield messages, processes
    registration.registrations.close()
    for process in processes:
        process.wait(timeout=3)


def status(client, path, headers, phase):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get(path, headers=headers).raise_for_status()
        if result.json()["status"] == phase:
            return result
        time.sleep(0.01)
    pytest.fail(f"Registration did not reach {phase}")


def test_registration_routes_require_super_administrator(client, auth_header, db_session):
    for role in ("user", "admin"):
        user = db_session.query(User).filter_by(username="testuser").one()
        user.role = role
        db_session.commit()
        for headers, expected in (({}, 401), (auth_header, 403)):
            assert client.post(PATH, headers=headers, json={"name": "Fixture"}).status_code == expected
            assert client.get(f"{PATH}/unknown", headers=headers).status_code == expected
            assert client.delete(f"{PATH}/unknown", headers=headers).status_code == expected
            assert client.post(f"{PATH}/unknown/import", headers=headers).status_code == expected


def test_import_is_encrypted_rechecked_and_idempotent(client, super_admin, db_session, worker, monkeypatch):
    monkeypatch.setattr(FeishuClient, "bot_identity", lambda self: "ou_fixture_bot")
    started = client.post(PATH, headers=super_admin, json={"name": "Fixture"})
    assert started.status_code == 202 and started.headers["cache-control"] == "no-store"
    path = f"{PATH}/{started.json()['id']}"
    confirmed = status(client, path, super_admin, "confirmed")
    assert confirmed.json()["app_id"] == "cli_fixture" and SECRET not in confirmed.text
    assert "url" not in confirmed.json()
    assert db_session.query(FeishuApplication).count() == 0

    imported = client.post(path + "/import", headers=super_admin)
    assert imported.status_code == 201 and SECRET not in imported.text
    assert imported.json()["transport"] == "websocket"
    assert client.post(path + "/import", headers=super_admin).json() == imported.json()
    row = db_session.query(FeishuApplication).one()
    assert row.app_secret != SECRET and encryption.decrypt(row.app_secret) == SECRET
    assert len(worker[1]) == 1
    assert db_session.query(AuditLog).filter_by(action="feishu.create_application").count() == 1


def test_rechecks_authority_after_external_verification(client, super_admin, db_session, worker, monkeypatch):
    def lose_authority(_self):
        user = db_session.query(User).filter_by(username="testuser").one()
        user.role = "admin"
        db_session.commit()
        return "ou_fixture_bot"

    monkeypatch.setattr(FeishuClient, "bot_identity", lose_authority)
    started = client.post(PATH, headers=super_admin, json={"name": "Fixture"}).raise_for_status()
    path = f"{PATH}/{started.json()['id']}"
    status(client, path, super_admin, "confirmed")
    assert client.post(path + "/import", headers=super_admin).status_code == 403
    assert db_session.query(FeishuApplication).count() == 0


def test_registration_is_private_to_the_starting_administrator_and_cancellable(client, super_admin, db_session, worker):
    messages, processes = worker
    messages.pop()
    second = User(username="second", email="second@example.test", password_hash="unused", role="super_admin")
    db_session.add(second)
    db_session.commit()
    other = {"Authorization": f"Bearer {create_access_token(second.id, second.effective_token_version)}"}
    started = client.post(PATH, headers=super_admin, json={"name": "Fixture"}).raise_for_status()
    path = f"{PATH}/{started.json()['id']}"
    continuing = client.post(PATH, headers=super_admin, json={"name": "Duplicate"}).raise_for_status()
    assert continuing.json()["id"] == started.json()["id"]
    waiting = status(client, path, super_admin, "waiting")
    assert waiting.json()["url"].startswith("https://accounts.feishu.cn/")
    assert client.get(path, headers=other).status_code == 404
    assert client.post(path + "/import", headers=other).status_code == 404
    assert client.delete(path, headers=other).status_code == 404
    assert client.post(path + "/import", headers=super_admin).status_code == 409
    assert client.delete(path, headers=super_admin).status_code == 204
    assert client.get(path, headers=super_admin).status_code == 404
    assert len(processes) == 1 and processes[0].wait(timeout=3) != 0


def test_import_retries_failed_verification_with_same_registration(
    client, super_admin, db_session, worker, monkeypatch
):
    def offline(_self):
        raise FeishuAPIError("Synthetic verification failure")

    monkeypatch.setattr(FeishuClient, "bot_identity", offline)
    started = client.post(PATH, headers=super_admin, json={"name": "Fixture"}).raise_for_status()
    path = f"{PATH}/{started.json()['id']}"
    status(client, path, super_admin, "confirmed")
    assert client.post(path + "/import", headers=super_admin).status_code == 502
    assert db_session.query(FeishuApplication).count() == 0
    monkeypatch.setattr(FeishuClient, "bot_identity", lambda self: "ou_fixture_bot")
    assert client.post(path + "/import", headers=super_admin).status_code == 201
    assert len(worker[1]) == 1
