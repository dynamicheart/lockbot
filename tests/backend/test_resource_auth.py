"""AC-2 / AC-2.1 / AC-3 / AC-4 / AC-10: binding issuance, confirmation and credentials.

The shape of the design under test:

- ``POST /api/resource-auth/bindings`` is unauthenticated, writes **nothing** durable, and
  returns an inert credential plus a public bind code.
- The credential becomes active only when a signature-verified chat message confirms the
  code, which is also the only moment a trustworthy identity exists.
- The credential never expires by default, so revocation is the only invalidation path.
"""

from datetime import datetime, timedelta, timezone

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_auth import credentials, service
from lockbot.backend.app.resource_auth.models import ResourceBinding
from lockbot.backend.app.resource_auth.pending import pending_store

PLATFORM = "infoflow"
ACTOR = "alice"
OTHER_ACTOR = "bob"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def chat_hook(monkeypatch, session_factory):
    """The injected chat hook, pointed at the test database."""
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)

    def _call(action, argument="", *, bot_id, platform=PLATFORM, actor=ACTOR):
        return service.handle_chat_binding(
            platform=platform,
            external_user_id=actor,
            bot_id=bot_id,
            action=action,
            argument=argument,
        )

    return _call


@pytest.fixture
def bot_id(client, auth_header, db_session):
    """A bot owned by the authenticated test user, with enrollment enabled."""
    resp = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": "agentbot",
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"n1": "m1"},
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["id"]

    enabled = client.put(f"/api/bots/{created}/resource-auth", headers=auth_header, json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    db_session.expire_all()
    return created


@pytest.fixture
def issued(client):
    """An inert credential plus its bind code."""
    resp = client.post("/api/resource-auth/bindings", json={"label": "ci-runner"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _confirm(chat_hook, issued, bot_id, **kwargs):
    return chat_hook("bind", issued["bind_code"], bot_id=bot_id, **kwargs)


def _auth(credential):
    return {"Authorization": f"Bearer {credential}"}


# ── AC-2: two keys up front, durable write only on confirmation ───────────────


class TestIssuance:
    def test_one_call_returns_both_keys_and_a_deadline(self, issued):
        assert issued["binding_id"]
        assert issued["credential"].startswith(credentials.CREDENTIAL_PREFIX)
        assert issued["bind_code"]
        assert issued["bind_expires_at"]

    def test_creation_writes_no_database_row(self, issued, db_session):
        assert db_session.query(ResourceBinding).count() == 0

    def test_creation_records_only_digests_in_the_pending_store(self, issued):
        record = pending_store.get(issued["binding_id"])
        assert record is not None
        assert record.code_digest == credentials.digest_bind_code(issued["bind_code"])
        assert record.credential_digest == credentials.digest_credential(issued["credential"])
        assert issued["bind_code"] not in repr(record)
        assert issued["credential"] not in repr(record)

    def test_default_bind_window_is_ten_minutes(self, issued):
        # Python 3.10's fromisoformat does not accept a trailing "Z".
        deadline = datetime.fromisoformat(issued["bind_expires_at"].replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        remaining = deadline - datetime.now(timezone.utc)
        assert timedelta(minutes=9) < remaining <= timedelta(minutes=10)

    @pytest.mark.parametrize("field", ["external_user_id", "platform", "actor", "bot_id"])
    def test_identity_fields_in_the_request_body_are_rejected(self, client, field):
        """AC-2: the caller can never declare who it acts as."""
        resp = client.post("/api/resource-auth/bindings", json={field: "attacker"})
        assert resp.status_code == 422

    def test_body_is_optional(self, client):
        assert client.post("/api/resource-auth/bindings", json={}).status_code == 201


class TestConfirmation:
    def test_confirmation_writes_the_durable_row_from_trusted_identity(self, issued, bot_id, chat_hook, db_session):
        reply = _confirm(chat_hook, issued, bot_id)
        assert "【Agent授权成功】" in reply

        row = db_session.query(ResourceBinding).one()
        assert row.platform == PLATFORM
        assert row.external_user_id == ACTOR
        assert row.bound_bot_id == bot_id
        assert row.label == "ci-runner"
        assert row.credential_digest == credentials.digest_credential(issued["credential"])
        assert row.revoked_at is None

    def test_credential_is_inert_before_confirmation_and_active_after(self, issued, bot_id, chat_hook, db_session):
        """AC-2: the credential the agent already holds activates at confirmation time."""
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=issued["credential"]) is None, (
            "an unconfirmed credential must be inert"
        )

        _confirm(chat_hook, issued, bot_id)
        db_session.expire_all()

        row = service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=issued["credential"])
        assert row is not None
        assert row.external_user_id == ACTOR

    def test_the_claim_is_single_use(self, issued, bot_id, chat_hook, db_session):
        _confirm(chat_hook, issued, bot_id)
        second = _confirm(chat_hook, issued, bot_id)
        assert "✅" not in second
        assert db_session.query(ResourceBinding).count() == 1

    def test_pending_record_is_removed_by_confirmation(self, issued, bot_id, chat_hook):
        _confirm(chat_hook, issued, bot_id)
        assert pending_store.get(issued["binding_id"]) is None

    def test_unknown_code_confirms_nothing(self, bot_id, chat_hook, db_session):
        reply = chat_hook("bind", "ZZZZ9999", bot_id=bot_id)
        assert "✅" not in reply
        assert db_session.query(ResourceBinding).count() == 0

    def test_bind_without_a_code_explains_usage(self, bot_id, chat_hook):
        assert "bind" in chat_hook("bind", "", bot_id=bot_id).lower()

    def test_repeated_failures_are_throttled(self, bot_id, chat_hook):
        replies = [chat_hook("bind", "ZZZZ9999", bot_id=bot_id) for _ in range(7)]
        assert replies[0] != replies[-1], "a throttled reply must differ from a plain rejection"

    def test_a_different_sender_is_not_throttled_by_someone_elses_failures(self, bot_id, chat_hook):
        for _ in range(7):
            chat_hook("bind", "ZZZZ9999", bot_id=bot_id, actor=ACTOR)
        fresh = chat_hook("bind", "YYYY8888", bot_id=bot_id, actor=OTHER_ACTOR)
        assert fresh == chat_hook("bind", "XXXX7777", bot_id=bot_id, actor="carol")


class TestStatusRoute:
    def test_pending_then_confirmed(self, client, issued, bot_id, chat_hook):
        before = client.get(f"/api/resource-auth/bindings/{issued['binding_id']}")
        assert before.json()["status"] == "pending"

        _confirm(chat_hook, issued, bot_id)
        after = client.get(f"/api/resource-auth/bindings/{issued['binding_id']}")
        assert after.json()["status"] == "confirmed"

    def test_unknown_id_is_expired_never_pending(self, client):
        resp = client.get("/api/resource-auth/bindings/does-not-exist")
        assert resp.json()["status"] == "expired"

    def test_status_never_discloses_the_code_or_the_credential(self, client, issued):
        body = client.get(f"/api/resource-auth/bindings/{issued['binding_id']}").text
        assert issued["bind_code"] not in body
        assert issued["credential"] not in body

    def test_revoked_is_reported_after_chat_revocation(self, client, issued, bot_id, chat_hook):
        _confirm(chat_hook, issued, bot_id)
        chat_hook("unbind", credentials.short_id(issued["binding_id"]), bot_id=bot_id)
        resp = client.get(f"/api/resource-auth/bindings/{issued['binding_id']}")
        assert resp.json()["status"] == "revoked"


class TestRestartInvalidatesPending:
    """AC-2.1: a restart before confirmation invalidates the issued credential, by design."""

    def test_pending_record_is_lost_and_cannot_be_confirmed(self, issued, bot_id, chat_hook, db_session):
        pending_store.purge_all()  # what a process restart does to an in-process store
        reply = _confirm(chat_hook, issued, bot_id)
        assert "✅" not in reply
        assert db_session.query(ResourceBinding).count() == 0

    def test_creating_a_new_binding_still_works_after_a_restart(self, client, issued):
        pending_store.purge_all()
        assert client.post("/api/resource-auth/bindings", json={}).status_code == 201


# ── AC-3: enrollment requires the owner to enable it ──────────────────────────


class TestEnrollmentGate:
    @pytest.fixture
    def disabled_bot_id(self, client, auth_header):
        resp = client.post(
            "/api/bots",
            headers=auth_header,
            json={
                "name": "closedbot",
                "resource_auth_enabled": False,
                "bot_type": "NODE",
                "webhook_url": "https://example.invalid/hook",
                "cluster_configs": {"n1": "m1"},
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def test_explicitly_disabled_enrollment_is_preserved(self, client, auth_header, disabled_bot_id):
        bots = client.get("/api/bots", headers=auth_header).json()
        target = next(b for b in bots if b["id"] == disabled_bot_id)
        assert target["resource_auth_enabled"] is False

    def test_bind_is_refused_while_enrollment_is_disabled(self, issued, disabled_bot_id, chat_hook, db_session):
        reply = _confirm(chat_hook, issued, disabled_bot_id)
        assert "✅" not in reply
        assert db_session.query(ResourceBinding).count() == 0

    def test_the_pending_record_is_left_unclaimed_when_refused(self, issued, disabled_bot_id, chat_hook):
        _confirm(chat_hook, issued, disabled_bot_id)
        assert pending_store.get(issued["binding_id"]) is not None, "the code must still be usable later"

    def test_owner_can_enable_and_disable(self, client, auth_header, disabled_bot_id):
        on = client.put(f"/api/bots/{disabled_bot_id}/resource-auth", headers=auth_header, json={"enabled": True})
        assert on.json()["resource_auth_enabled"] is True
        off = client.put(f"/api/bots/{disabled_bot_id}/resource-auth", headers=auth_header, json={"enabled": False})
        assert off.json()["resource_auth_enabled"] is False

    def test_super_admin_can_enable_someone_elses_bot(self, client, disabled_bot_id, db_session):
        from lockbot.backend.app.auth.models import User
        from lockbot.backend.app.auth.router import _hash_password

        db_session.add(
            User(
                username="root",
                email="root@test.com",
                password_hash=_hash_password("rootpass123"),
                role="super_admin",
            )
        )
        db_session.commit()
        token = client.post("/api/auth/login", json={"username": "root", "password": "rootpass123"}).json()[
            "access_token"
        ]
        resp = client.put(
            f"/api/bots/{disabled_bot_id}/resource-auth",
            headers={"Authorization": f"Bearer {token}"},
            json={"enabled": True},
        )
        assert resp.status_code == 200

    def test_a_non_owner_cannot_enable_someone_elses_bot(self, client, disabled_bot_id):
        client.post(
            "/api/auth/register",
            json={"username": "stranger", "email": "s@test.com", "password": "strangerpass1"},
        )
        token = client.post("/api/auth/login", json={"username": "stranger", "password": "strangerpass1"}).json()[
            "access_token"
        ]
        resp = client.put(
            f"/api/bots/{disabled_bot_id}/resource-auth",
            headers={"Authorization": f"Bearer {token}"},
            json={"enabled": True},
        )
        assert resp.status_code in (403, 404)

    def test_unauthenticated_cannot_enable(self, client, disabled_bot_id):
        assert client.put(f"/api/bots/{disabled_bot_id}/resource-auth", json={"enabled": True}).status_code == 401

    def test_disabling_does_not_revoke_already_active_credentials(
        self, client, auth_header, issued, bot_id, chat_hook, db_session
    ):
        """Disabling suspends access but preserves the key for an explicit re-enable."""
        _confirm(chat_hook, issued, bot_id)
        client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": False})
        db_session.expire_all()

        row = service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=issued["credential"])
        assert row is not None, "the stored credential is preserved, while HTTP access is suspended"
        assert row.revoked_at is None

    def test_disabling_does_not_confirm_anything_pending(self, client, auth_header, issued, bot_id, db_session):
        client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": False})
        assert db_session.query(ResourceBinding).count() == 0


# ── AC-4: the credential is opaque, digest-stored, and strictly separated ──────


@pytest.fixture
def active(issued, bot_id, chat_hook, db_session):
    """A confirmed, active credential plus its durable row."""
    _confirm(chat_hook, issued, bot_id)
    db_session.expire_all()
    row = db_session.query(ResourceBinding).one()
    return {"credential": issued["credential"], "row": row, "binding_id": issued["binding_id"]}


class TestCredentialPredicate:
    def test_only_a_digest_is_persisted(self, active, db_session):
        row = active["row"]
        assert row.credential_digest != active["credential"]
        assert active["credential"] not in row.credential_digest
        columns = [getattr(row, c.name) for c in row.__table__.columns]
        assert all(active["credential"] != value for value in columns)

    def test_credential_does_not_expire_by_default(self, active):
        assert active["row"].credential_expires_at is None

    def test_a_configured_past_expiry_is_enforced(self, active, db_session, bot_id):
        active["row"].credential_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db_session.commit()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is None

    def test_a_configured_future_expiry_still_authenticates(self, active, db_session, bot_id):
        active["row"].credential_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        db_session.commit()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is not None

    def test_a_revoked_credential_is_rejected_immediately(self, active, db_session, bot_id):
        active["row"].revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db_session.commit()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is None

    def test_a_credential_scoped_to_one_bot_is_rejected_for_another(self, active, db_session, bot_id):
        assert service.authenticate(db_session, bot_id=bot_id + 999, bot_uuid=None, token=active["credential"]) is None

    def test_row_id_reuse_cannot_transfer_the_credential(self, active, db_session, bot_id):
        """AC-9: the bot uuid snapshot is checked, so a recycled row id is not enough."""
        bot = db_session.get(Bot, bot_id)
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=bot.uuid, token=active["credential"])
        assert (
            service.authenticate(db_session, bot_id=bot_id, bot_uuid="a-different-uuid", token=active["credential"])
            is None
        )

    def test_a_wrong_credential_is_rejected(self, active, db_session, bot_id):
        other = credentials.generate_credential()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=other) is None

    def test_a_management_jwt_is_rejected_by_the_predicate(self, active, db_session, bot_id, auth_header):
        jwt_token = auth_header["Authorization"].removeprefix("Bearer ")
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=jwt_token) is None

    def test_a_legacy_service_key_is_rejected_by_the_predicate(self, active, db_session, bot_id):
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token="lbk_" + "a" * 48) is None

    def test_using_a_credential_records_last_used(self, active, db_session, bot_id):
        assert active["row"].last_used_at is None
        row = service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"])
        service.touch_last_used(db_session, row)
        db_session.commit()
        assert row.last_used_at is not None


class TestPrincipalSeparation:
    """AC-4: a personal credential must not reach management or legacy routes."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/bots",
            "/api/auth/me",
            "/api/audit/logs",
        ],
    )
    def test_credential_is_rejected_on_management_routes(self, client, active, path):
        resp = client.get(path, headers=_auth(active["credential"]))
        assert resp.status_code in (401, 403, 404), f"{path} accepted a personal credential"

    def test_credential_is_rejected_on_the_legacy_locked_users_route(self, client, active, bot_id):
        resp = client.get(f"/api/bots/{bot_id}/locked-users", headers=_auth(active["credential"]))
        assert resp.status_code == 401

    def test_credential_is_rejected_on_the_legacy_opkick_route(self, client, active, bot_id):
        resp = client.post(
            f"/api/bots/{bot_id}/opkick",
            headers=_auth(active["credential"]),
            json={"target_user": "someone"},
        )
        assert resp.status_code in (401, 422)

    def test_credential_cannot_toggle_its_own_enrollment(self, client, active, bot_id):
        resp = client.put(
            f"/api/bots/{bot_id}/resource-auth",
            headers=_auth(active["credential"]),
            json={"enabled": True},
        )
        assert resp.status_code == 401


# ── AC-10: revocation is self-service in chat and leaks nothing ────────────────


class TestChatRevocation:
    def test_bindings_lists_only_the_senders_own(self, issued, bot_id, chat_hook, client):
        _confirm(chat_hook, issued, bot_id)
        other = client.post("/api/resource-auth/bindings", json={"label": "bobs-agent"}).json()
        chat_hook("bind", other["bind_code"], bot_id=bot_id, actor=OTHER_ACTOR)

        mine = chat_hook("bindings", bot_id=bot_id)
        assert "ci-runner" in mine
        assert credentials.short_id(issued["binding_id"]) not in mine
        assert credentials.short_id(other["binding_id"]) not in mine
        assert "bobs-agent" not in mine

    def test_bindings_is_empty_for_a_sender_with_none(self, bot_id, chat_hook):
        assert credentials.short_id("x" * 20) not in chat_hook("bindings", bot_id=bot_id)

    def test_listing_shows_label_and_bot(self, active, bot_id, chat_hook):
        listed = chat_hook("bindings", bot_id=bot_id)
        assert "ci-runner" in listed
        assert f"机器人 ID：{bot_id}" in listed
        assert "agentbot" not in listed

    def test_listing_never_contains_a_secret(self, active, issued, bot_id, chat_hook):
        listed = chat_hook("bindings", bot_id=bot_id)
        assert issued["credential"] not in listed
        assert issued["bind_code"] not in listed
        assert active["row"].credential_digest not in listed

    def test_unbind_revokes_one_and_takes_effect_on_the_next_request(self, active, bot_id, chat_hook, db_session):
        short = credentials.short_id(active["binding_id"])
        reply = chat_hook("unbind", short, bot_id=bot_id)
        assert short in reply

        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is None

    def test_unbind_without_an_argument_revokes_current_authorization(self, active, bot_id, chat_hook, db_session):
        reply = chat_hook("unbind", bot_id=bot_id)
        assert f"机器人 ID：{bot_id}" in reply
        assert "agentbot" not in reply
        assert "已撤销" in reply
        assert "份" not in reply
        again = chat_hook("unbind", bot_id=bot_id)
        assert "没有有效授权，无需解绑" in again
        assert "Agent" not in again
        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is None

    def test_unbind_all_revokes_every_binding_of_the_sender(self, client, bot_id, chat_hook, db_session):
        first = client.post("/api/resource-auth/bindings", json={}).json()
        second = client.post("/api/resource-auth/bindings", json={}).json()
        chat_hook("bind", first["bind_code"], bot_id=bot_id)
        chat_hook("bind", second["bind_code"], bot_id=bot_id)

        chat_hook("unbind", "all", bot_id=bot_id)
        db_session.expire_all()
        for token in (first["credential"], second["credential"]):
            assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=token) is None

    def test_unbind_one_does_not_affect_the_senders_others(self, client, bot_id, chat_hook, db_session):
        keep = client.post("/api/resource-auth/bindings", json={}).json()
        drop = client.post("/api/resource-auth/bindings", json={}).json()
        chat_hook("bind", keep["bind_code"], bot_id=bot_id, actor=OTHER_ACTOR)
        chat_hook("bind", drop["bind_code"], bot_id=bot_id)

        chat_hook("unbind", credentials.short_id(drop["binding_id"]), bot_id=bot_id)
        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=keep["credential"]) is not None
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=drop["credential"]) is None

    def test_a_sender_cannot_revoke_another_identitys_binding(self, active, bot_id, chat_hook, db_session):
        short = credentials.short_id(active["binding_id"])
        reply = chat_hook("unbind", short, bot_id=bot_id, actor=OTHER_ACTOR)
        assert short in reply  # the reply names the id the sender asked about

        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is not None

    def test_unbind_all_only_touches_the_senders_own(self, active, bot_id, chat_hook, db_session):
        chat_hook("unbind", "all", bot_id=bot_id, actor=OTHER_ACTOR)
        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is not None

    def test_unknown_id_is_reported_as_not_found(self, active, bot_id, chat_hook):
        assert "zzzzzzzz" in chat_hook("unbind", "zzzzzzzz", bot_id=bot_id)

    def test_an_unknown_action_does_not_crash(self, bot_id, chat_hook):
        assert chat_hook("rebind", "x", bot_id=bot_id)


class TestOwnerRevokeAllRoute:
    def test_owner_can_revoke_every_credential_for_their_bot(self, client, auth_header, active, bot_id, db_session):
        resp = client.delete(f"/api/bots/{bot_id}/resource-auth/bindings", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["revoked"] == 1

        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is None

    def test_revocation_is_audited(self, client, auth_header, active, bot_id, db_session):
        from lockbot.backend.app.audit.models import AuditLog

        client.delete(f"/api/bots/{bot_id}/resource-auth/bindings", headers=auth_header)
        actions = [row.action for row in db_session.query(AuditLog).all()]
        assert "bot.resource_auth_revoke_all" in actions

    def test_audit_record_contains_no_secret(self, client, auth_header, active, issued, bot_id, db_session):
        from lockbot.backend.app.audit.models import AuditLog

        client.delete(f"/api/bots/{bot_id}/resource-auth/bindings", headers=auth_header)
        for row in db_session.query(AuditLog).all():
            blob = f"{row.action}{row.detail}{row.target_name}"
            assert issued["credential"] not in blob
            assert issued["bind_code"] not in blob

    def test_enabling_enrollment_is_audited(self, client, auth_header, bot_id, db_session):
        from lockbot.backend.app.audit.models import AuditLog

        client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": False})
        actions = [row.action for row in db_session.query(AuditLog).all()]
        assert "bot.update" in actions and "bot.resource_auth_disabled" not in actions

    def test_a_non_owner_cannot_revoke(self, client, active, bot_id):
        client.post(
            "/api/auth/register",
            json={"username": "nosy", "email": "n@test.com", "password": "nosypass1234"},
        )
        token = client.post("/api/auth/login", json={"username": "nosy", "password": "nosypass1234"}).json()[
            "access_token"
        ]
        resp = client.delete(
            f"/api/bots/{bot_id}/resource-auth/bindings",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code in (403, 404)

    def test_revoking_twice_reports_zero_the_second_time(self, client, auth_header, active, bot_id):
        assert client.delete(f"/api/bots/{bot_id}/resource-auth/bindings", headers=auth_header).json()["revoked"] == 1
        assert client.delete(f"/api/bots/{bot_id}/resource-auth/bindings", headers=auth_header).json()["revoked"] == 0


class TestOneSharedCredentialPerUserBot:
    def test_second_confirmation_keeps_original_key_and_consumes_new_code(
        self, active, bot_id, chat_hook, client, db_session
    ):
        another = client.post("/api/resource-auth/bindings", json={"label": "second-agent"}).json()
        reply = _confirm(chat_hook, another, bot_id)
        assert "未激活" in reply
        assert active["credential"] not in reply
        assert another["credential"] not in reply
        assert pending_store.get(another["binding_id"]) is None
        db_session.expire_all()
        assert db_session.query(ResourceBinding).filter_by(revoked_at=None).count() == 1
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"])
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=another["credential"]) is None
        assert "✅" not in _confirm(chat_hook, another, bot_id)

    @pytest.mark.parametrize("argument", ["", "all"])
    def test_unbind_and_list_are_scoped_to_sender_and_current_bot(
        self, active, bot_id, chat_hook, client, db_session, auth_header, argument
    ):
        other_bot = client.post(
            "/api/bots",
            headers=auth_header,
            json={
                "name": "otherbot",
                "bot_type": "NODE",
                "webhook_url": "https://example.invalid/hook",
                "cluster_configs": {"n2": "m2"},
            },
        ).json()["id"]
        client.put(f"/api/bots/{other_bot}/resource-auth", headers=auth_header, json={"enabled": True})
        other = client.post("/api/resource-auth/bindings", json={}).json()
        bob = client.post("/api/resource-auth/bindings", json={}).json()
        _confirm(chat_hook, other, other_bot)
        _confirm(chat_hook, bob, bot_id, actor=OTHER_ACTOR)
        assert credentials.short_id(other["binding_id"]) not in chat_hook("bindings", bot_id=bot_id)
        chat_hook("unbind", credentials.short_id(other["binding_id"]), bot_id=bot_id)
        chat_hook("unbind", argument, bot_id=bot_id)
        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is None
        assert service.authenticate(db_session, bot_id=other_bot, bot_uuid=None, token=other["credential"])
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=bob["credential"])
        assert "没有有效授权，无需解绑" in chat_hook("unbind", bot_id=bot_id)
        # Explicit replacement after unbind creates exactly one new effective key.
        replacement = client.post("/api/resource-auth/bindings", json={}).json()
        _confirm(chat_hook, replacement, bot_id)
        db_session.expire_all()
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=replacement["credential"])
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=active["credential"]) is None

    @pytest.mark.parametrize("invalidity", ["expiry", "revision", "uuid"])
    def test_inactive_old_row_is_retired_before_rebinding(
        self, active, bot_id, chat_hook, client, db_session, invalidity
    ):
        row = active["row"]
        if invalidity == "expiry":
            row.credential_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        elif invalidity == "revision":
            row.bound_identity_revision -= 1
        else:
            row.bound_bot_uuid = "old-bot-uuid"
        db_session.commit()
        another = client.post("/api/resource-auth/bindings", json={}).json()
        _confirm(chat_hook, another, bot_id)
        db_session.expire_all()
        assert db_session.get(ResourceBinding, row.id).revoked_at is not None
        assert service.authenticate(db_session, bot_id=bot_id, bot_uuid=None, token=another["credential"])
        assert db_session.query(ResourceBinding).filter_by(revoked_at=None).count() == 1

    def test_database_rejects_a_duplicate_even_if_service_is_bypassed(self, active, db_session):
        from sqlalchemy.exc import IntegrityError

        row = active["row"]
        db_session.add(
            ResourceBinding(
                public_id="duplicate",
                platform=row.platform,
                external_user_id=row.external_user_id,
                bound_bot_id=row.bound_bot_id,
                bound_bot_uuid=row.bound_bot_uuid,
                credential_digest="f" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestChatAuthorizationAudit:
    def test_binding_changes_are_audited_without_secrets_or_duplicate_events(
        self, client, chat_hook, issued, bot_id, db_session
    ):
        from lockbot.backend.app.bot_audit.models import BotAuditLog as AuditLog

        _confirm(chat_hook, issued, bot_id)
        another = client.post("/api/resource-auth/bindings", json={}).json()
        _confirm(chat_hook, another, bot_id)
        rows = db_session.query(AuditLog).filter_by(action="resource_auth.bind").all()
        assert len(rows) == 1
        assert rows[0].operator_username == f"{PLATFORM}:{ACTOR}"
        assert rows[0].operator_role == "agent" and rows[0].target_id == bot_id
        for value in [issued["credential"], issued["bind_code"], another["credential"], another["bind_code"]]:
            assert value not in (rows[0].detail or "")

    def test_only_real_own_unbind_changes_enter_audit(self, chat_hook, issued, bot_id, db_session):
        from lockbot.backend.app.bot_audit.models import BotAuditLog as AuditLog

        _confirm(chat_hook, issued, bot_id)
        chat_hook("unbind", bot_id=bot_id, actor=OTHER_ACTOR)
        assert db_session.query(AuditLog).filter_by(action="resource_auth.unbind").count() == 0
        chat_hook("unbind", bot_id=bot_id)
        chat_hook("unbind", bot_id=bot_id)
        rows = db_session.query(AuditLog).filter_by(action="resource_auth.unbind").all()
        assert len(rows) == 1
        assert rows[0].operator_username == f"{PLATFORM}:{ACTOR}" and rows[0].target_id == bot_id

    def test_rolled_back_confirmation_leaves_no_authorization_audit(
        self, monkeypatch, session_factory, chat_hook, issued, bot_id, db_session
    ):
        from lockbot.backend.app.bot_audit.models import BotAuditLog as AuditLog

        class FailingSession(session_factory.class_):
            def commit(self):
                raise RuntimeError("test commit failed")

        from sqlalchemy.orm import sessionmaker

        monkeypatch.setattr(
            "lockbot.backend.app.database.SessionLocal", sessionmaker(bind=db_session.get_bind(), class_=FailingSession)
        )
        with pytest.raises(RuntimeError, match="test commit failed"):
            _confirm(chat_hook, issued, bot_id)
        assert db_session.query(AuditLog).filter_by(action="resource_auth.bind").count() == 0
        assert db_session.query(ResourceBinding).filter_by(public_id=issued["binding_id"]).count() == 0
