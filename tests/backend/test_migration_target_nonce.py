# ruff: noqa: F811
"""Private nonce creation and existing verified ingress, no management execution API."""

import copy
import json
import time
from datetime import datetime, timedelta

import pytest
from lockbot.backend.app.bot_migrations import target_proof
from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked
from lockbot.backend.app.bot_migrations.models import BotMigration, MigrationTargetProof
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupMembership, FeishuGroupRoute
from lockbot.backend.app.feishu.service import accept_sdk_event as accept_event
from lockbot.core.platforms.feishu_events import InvalidFeishuEvent

from backend.test_durable_platform_cutover import subject  # noqa: F401


@pytest.fixture
def challenge(subject, db_session, monkeypatch, request):
    monkeypatch.setattr(target_proof, "_budget", target_proof.IngressBudget(app_limit=60, actor_limit=6))
    service, manager, bot_id, app_id, _ = subject
    app = db_session.get(FeishuApplication, app_id)
    if getattr(request, "param", None) == "http":
        app.transport = "http"
        app.verification_token = encryption.encrypt("fixture-proof")
        app.encrypt_key = encryption.encrypt("fixture-encrypt")
    db_session.add(
        FeishuGroupMembership(
            application_id=app_id,
            chat_id="oc_target",
            app_id=app.app_id,
            tenant_key=app.tenant_key,
            status="present",
            event_time_ms=1,
            event_id="fixture-added",
        )
    )
    db_session.commit()
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1, token_version=0)["operation_id"]
    row = db_session.get(BotMigration, operation)
    clock = [time.time()]
    monkeypatch.setattr(target_proof, "_now", lambda: datetime.utcfromtimestamp(clock[0]))
    result = target_proof.create_target_challenge(
        db_session,
        operation,
        actor_id=1,
        token_version=0,
        reviewed_target_digest=row.target_digest,
        publication_evidence_ref="e" * 32,
        publication_valid_until=datetime.utcfromtimestamp(clock[0]) + timedelta(minutes=10),
    )
    clock[0] += 1
    message = {
        "schema": "2.0",
        "header": {
            "event_id": "fixture-event",
            "event_type": "im.message.receive_v1",
            "app_id": app.app_id,
            "tenant_key": app.tenant_key,
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_person"}, "tenant_key": app.tenant_key},
            "message": {
                "message_id": "fixture-message",
                "chat_type": "group",
                "chat_id": "oc_target",
                "message_type": "text",
                "create_time": str(int(clock[0] * 1000)),
                "content": json.dumps({"text": "@_user_1 " + result["command"]}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": app.bot_open_id}}],
            },
        },
    }
    return service, manager, bot_id, app_id, result, message, clock


def test_signed_group_nonce_is_one_use_and_never_binds_or_executes(db_session, challenge, monkeypatch):
    _, manager, bot_id, app_id, result, message, _ = challenge
    app = db_session.get(FeishuApplication, app_id)
    original = copy.deepcopy(manager.get_instance(bot_id).bot.state.bot_state)
    monkeypatch.setattr(
        "lockbot.backend.app.feishu.router._executor.submit", lambda *a, **k: pytest.fail("no allocator")
    )
    assert accept_event(db_session, app, message) == {"accepted": True, "migration_proof": True}
    assert accept_event(db_session, app, message) == {"accepted": False, "migration_proof": True}
    report = target_proof.inspect_target_receipt(db_session, result["proof_id"], actor_id=1, token_version=0)
    assert report["delivery_verified"] and not report["account_ownership_verified"] and not report["can_commit"]
    assert db_session.query(FeishuGroupRoute).count() == 0
    assert manager.get_instance(bot_id).bot.state.bot_state == original
    row = db_session.get(MigrationTargetProof, result["proof_id"])
    assert row.status == "consumed" and row.nonce_digest not in result["command"]
    assert result["command"].split()[1] not in repr(row.__dict__)


@pytest.mark.parametrize(
    "change", ["expired", "source", "application", "reviewer", "session", "password", "membership", "instance"]
)
def test_stale_or_revoked_scope_never_consumes(db_session, challenge, change):
    from lockbot.backend.app.auth.models import User
    from lockbot.backend.app.settings.models import SiteSetting

    _, _, bot_id, app_id, result, message, clock = challenge
    if change == "expired":
        clock[0] += 121
    elif change == "source":
        db_session.get(Bot, bot_id).cluster_configs = '["other"]'
    elif change == "application":
        db_session.get(FeishuApplication, app_id).name = "Changed application"
        db_session.get(FeishuApplication, app_id).tenant_key = "changed-tenant"
    elif change == "reviewer":
        db_session.get(User, 1).role = "admin"
    elif change == "session":
        db_session.get(User, 1).token_version = 1
    elif change == "password":
        db_session.get(User, 1).must_change_password = True
    elif change == "membership":
        db_session.get(FeishuGroupMembership, (app_id, "oc_target")).status = "removed"
    else:
        db_session.get(SiteSetting, "resource_api_instance_id").value = "different-instance"
    db_session.commit()
    app = db_session.get(FeishuApplication, app_id)
    if change == "application":
        # Production normalization rejects a tenant mismatch before the proof processor.
        with pytest.raises(InvalidFeishuEvent):
            accept_event(db_session, app, message)
    else:
        assert accept_event(db_session, app, message)["accepted"] is False
    db_session.expire_all()
    assert db_session.get(MigrationTargetProof, result["proof_id"]).status != "consumed"
    assert db_session.query(FeishuGroupRoute).count() == 0


@pytest.mark.parametrize("change", ["old_event", "wrong_group", "missing_at", "other_bot_at", "dm", "malformed"])
def test_event_must_be_fresh_exact_group_actual_own_mention(db_session, challenge, change):
    _, _, _, app_id, result, message, clock = challenge
    event_message = message["event"]["message"]
    if change == "old_event":
        event_message["create_time"] = str(int((clock[0] - 10) * 1000))
    elif change == "wrong_group":
        event_message["chat_id"] = "oc_other"
    elif change == "missing_at":
        event_message["mentions"] = []
    elif change == "other_bot_at":
        event_message["mentions"][0]["id"]["open_id"] = "ou_another_bot"
    elif change == "dm":
        event_message["chat_type"] = "p2p"
    else:
        event_message["content"] = json.dumps({"text": "@_user_1 migration-check bad"})
    assert accept_event(db_session, db_session.get(FeishuApplication, app_id), message)["accepted"] is False
    db_session.expire_all()
    assert db_session.get(MigrationTargetProof, result["proof_id"]).status == "pending"


def test_no_management_creation_or_execution_routes_exist(client, admin_header, challenge):
    _, _, bot_id, _, _, _, _ = challenge
    for action in ("migration-target-challenge", "migration-confirm", "migration-commit"):
        assert client.post(f"/api/bots/{bot_id}/{action}", headers=admin_header, json={}).status_code == 404


def test_explicit_matching_publication_review_is_required(db_session, challenge):
    _, _, _, _, result, _, clock = challenge
    operation = db_session.get(MigrationTargetProof, result["proof_id"]).operation_id
    with pytest.raises(MigrationBlocked, match="publication"):
        target_proof.create_target_challenge(
            db_session,
            operation,
            actor_id=1,
            token_version=0,
            reviewed_target_digest="wrong",
            publication_evidence_ref="e" * 32,
            publication_valid_until=datetime.utcfromtimestamp(clock[0]) + timedelta(minutes=10),
        )


def test_unverified_internal_transport_cannot_consume_proof(db_session, challenge):
    from lockbot.backend.app.feishu.router import accept_event as unverified

    _, _, _, app_id, result, message, _ = challenge
    assert unverified(db_session, db_session.get(FeishuApplication, app_id), message)["accepted"] is False
    assert db_session.get(MigrationTargetProof, result["proof_id"]).status == "pending"


@pytest.mark.parametrize("challenge", ["http"], indirect=True)
def test_existing_http_signature_gate_runs_before_nonce_consumption(client, db_session, challenge):
    import base64
    import hashlib

    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    _, _, _, app_id, result, message, _ = challenge
    message["header"]["token"] = "fixture-proof"
    iv = b"0" * 16
    encrypted = AES.new(hashlib.sha256(b"fixture-encrypt").digest(), AES.MODE_CBC, iv).encrypt(
        pad(json.dumps(message).encode(), 16)
    )
    body = json.dumps({"encrypt": base64.b64encode(iv + encrypted).decode()}).encode()
    timestamp, nonce = str(int(time.time())), "fixture-nonce"
    signature = hashlib.sha256((timestamp + nonce + "fixture-encrypt").encode() + body).hexdigest()
    headers = {"x-lark-request-timestamp": timestamp, "x-lark-request-nonce": nonce, "x-lark-signature": "bad"}
    path = f"/api/feishu/events/{app_id}"
    assert client.post(path, content=body, headers=headers).status_code == 401
    db_session.expire_all()
    assert db_session.get(MigrationTargetProof, result["proof_id"]).status == "pending"
    headers["x-lark-signature"] = signature
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 200 and response.json()["accepted"] is True
    db_session.expire_all()
    assert db_session.get(MigrationTargetProof, result["proof_id"]).status == "consumed"
    assert db_session.query(FeishuGroupRoute).count() == 0


def test_nonce_probe_budget_rejects_before_database_work(db_session, challenge, monkeypatch):
    _, _, _, app_id, result, message, _ = challenge
    monkeypatch.setattr(target_proof._budget, "claim", lambda *args, **kwargs: False)
    response = accept_event(db_session, db_session.get(FeishuApplication, app_id), message)
    assert response == {"accepted": False, "migration_proof": True, "reason": "rate_limited"}
    assert db_session.get(MigrationTargetProof, result["proof_id"]).status == "pending"


def test_similarly_named_ordinary_resource_command_is_not_reserved(db_session, challenge):
    _, _, _, app_id, _, message, _ = challenge
    normalized = {"command": "migration-check-node", "user_id": "ou_person", "group_id": "oc_target"}
    assert (
        target_proof.consume_verified_group_event(db_session, app_id, message, normalized, verified_ingress=True)
        is None
    )


def test_default_off_unbound_target_uses_fresh_signed_delivery_without_membership(db_session, challenge, monkeypatch):
    _, _, _, app_id, result, message, _ = challenge
    db_session.query(FeishuGroupMembership).delete()
    db_session.commit()
    monkeypatch.setattr("lockbot.backend.app.feishu.unbound.queue_hint", lambda *a, **k: pytest.fail("no greeting"))
    assert accept_event(db_session, db_session.get(FeishuApplication, app_id), message)["accepted"]
    assert target_proof.inspect_target_receipt(db_session, result["proof_id"], actor_id=1, token_version=0)[
        "delivery_verified"
    ]
    assert db_session.query(FeishuGroupRoute).count() == 0


def test_default_off_target_records_removal_without_greeting_and_revokes_receipt(db_session, challenge, monkeypatch):
    from lockbot.backend.app.feishu import membership

    from backend.test_feishu_membership import event

    _, _, _, app_id, result, message, _ = challenge
    app = db_session.get(FeishuApplication, app_id)
    db_session.query(FeishuGroupMembership).delete()
    db_session.commit()
    assert accept_event(db_session, app, message)["accepted"]
    monkeypatch.setattr("lockbot.backend.app.feishu.unbound.queue_hint", lambda *a, **k: pytest.fail("no greeting"))
    removed = event(kind="deleted", event_id="migration-removal")
    removed["header"].update(app_id=app.app_id, tenant_key=app.tenant_key)
    removed["event"]["chat_id"] = "oc_target"
    removed["event"]["operator_tenant_key"] = app.tenant_key
    assert membership.accept_membership(db_session, app, removed) == {"accepted": True}
    assert db_session.get(FeishuGroupMembership, (app_id, "oc_target")).status == "removed"
    with pytest.raises(MigrationBlocked, match="verified_bot_added"):
        target_proof.inspect_target_receipt(db_session, result["proof_id"], actor_id=1, token_version=0)
    # Unrelated groups remain entirely ignored with the ordinary default-off policy.
    other = copy.deepcopy(removed)
    other["header"]["event_id"] = "unrelated"
    other["event"]["chat_id"] = "oc_unrelated"
    assert membership.accept_membership(db_session, app, other) == {"accepted": False}
    assert db_session.get(FeishuGroupMembership, (app_id, "oc_unrelated")) is None
    assert db_session.query(FeishuGroupRoute).count() == 0
