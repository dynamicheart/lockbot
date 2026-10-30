"""Actual manager + private state files + SQLite; fake target validation, no real Feishu IO."""

import json
import time
import uuid

import pytest
from lockbot.backend.app.auth.models import User
from lockbot.backend.app.bot_migrations.coordinator import CutoverCoordinator, MigrationBlocked
from lockbot.backend.app.bot_migrations.models import BotMigration
from lockbot.backend.app.bot_migrations.recovery import restore_fences
from lockbot.backend.app.bots import encryption
from lockbot.backend.app.bots.manager import BotManager, InstanceRetiring
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.bots.router import _build_config_dict
from lockbot.backend.app.feishu.models import FeishuApplication, FeishuGroupRoute
from lockbot.backend.app.managed_paths import get_managed_paths


@pytest.fixture
def subject(admin_header, db_session, session_factory):
    user = db_session.query(User).filter_by(username="adminuser").one()
    user.role = "super_admin"
    db_session.commit()
    bot = Bot(
        uuid="11111111-1111-4111-8111-111111111111",
        user_id=user.id,
        name="Fixture",
        bot_type="QUEUE",
        platform="Infoflow",
        identity_revision=0,
        group_id="1234",
        webhook_url=encryption.encrypt("https://example.invalid/webhook"),
        aes_key=encryption.encrypt("FAKE_AES"),
        token=encryption.encrypt("FAKE_TOKEN"),
        cluster_configs='["node1"]',
        config_overrides="{}",
        api_key="FAKE_OP",
        utilization_config=json.dumps(
            {
                "accessMode": "short_lived",
                "endpoint": "https://metrics.example.invalid",
                "profile": "lockbot-v1",
                "nodeMapping": {"node1": "host-a"},
            }
        ),
        status="running",
    )
    app = FeishuApplication(
        app_id="cli_fixture_" + uuid.uuid4().hex,
        name="Fixture App",
        tenant_key="fixture_tenant",
        bot_open_id="ou_fixture",
        app_secret=encryption.encrypt("FAKE_APP_SECRET"),
        verification_token=encryption.encrypt(""),
        encrypt_key=encryption.encrypt(""),
        transport="websocket",
    )
    db_session.add_all([bot, app])
    db_session.commit()
    manager = BotManager()
    manager.start_bot(bot.id, _build_config_dict(bot, db_session), activate=False)
    manager.activate_bot(bot.id)
    now = [time.time()]
    coordinator = CutoverCoordinator(
        session_factory,
        manager,
        clock=lambda: now[0],
        target_validator=lambda *args: True,
        metrics_validator=lambda *args: True,
        maximum_clock_offset=0,
    )
    yield coordinator, manager, bot.id, app.id, now
    manager.shutdown_all()


def test_empty_cutover_keeps_same_identity_inventory_and_uses_nonblocking_grant_wait(subject, db_session):
    service, manager, bot_id, app_id, now = subject
    original = db_session.get(Bot, bot_id)
    identity, inventory = original.uuid, original.cluster_configs
    planned = service.plan(bot_id, app_id, "oc_target", actor_id=1)
    assert planned["phase"] == "planned" and not manager.migration_fenced(bot_id)
    result = service.resume(planned["operation_id"], actor_id=1)
    assert result["phase"] == "checkpointed" and manager.migration_fenced(bot_id)
    assert not manager.is_running(bot_id)
    with pytest.raises(InstanceRetiring), manager.transition(bot_id):
        pass
    assert service.resume(planned["operation_id"], actor_id=1)["phase"] == "checkpointed"
    now[0] += 316
    result = service.resume(planned["operation_id"], actor_id=1)
    assert result["phase"] == "completed" and not manager.migration_fenced(bot_id)
    db_session.expire_all()
    current = db_session.get(Bot, bot_id)
    assert (current.uuid, current.cluster_configs, current.platform, current.group_id, current.identity_revision) == (
        identity,
        inventory,
        "Feishu",
        "oc_target",
        1,
    )
    assert current.api_key is None
    assert db_session.query(FeishuGroupRoute).one().bot_id == bot_id
    assert service.resume(planned["operation_id"], actor_id=1) == result
    state = json.loads(get_managed_paths().state_file(bot_id).read_text())
    assert state["lifecycle_outbox"]["identity_revision"] == 1
    assert state["bot_state"]["node1"]["current_users"] == []


@pytest.mark.parametrize(
    "boundary",
    [
        "freeze_committed",
        "checkpoint_published",
        "checkpoint_committed",
        "identity_switched",
        "target_staged",
        "target_activated",
        "completion_committed",
    ],
)
def test_crash_recovery_never_autostarts_source_or_replays_commands(subject, session_factory, db_session, boundary):
    service, manager, bot_id, app_id, now = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    fired = []

    def crash(phase):
        if phase == boundary and not fired:
            fired.append(phase)
            raise RuntimeError("simulated process failure")

    service.fault = crash
    if boundary in {"identity_switched", "target_staged", "target_activated", "completion_committed"}:
        assert service.resume(operation, actor_id=1)["phase"] == "checkpointed"
        now[0] += 316
    with pytest.raises(RuntimeError, match="simulated"):
        service.resume(operation, actor_id=1)
    manager.shutdown_all()
    recovered = BotManager()
    try:
        restore_fences(session_factory, recovered)
        db_session.expire_all()
        row = db_session.get(BotMigration, operation)
        if row.phase != "completed":
            assert recovered.migration_fenced(bot_id)
            with pytest.raises(InstanceRetiring), recovered.transition(bot_id):
                pass
        service.manager = recovered
        service.fault = lambda phase: None
        if row.phase in {"freezing", "checkpointed"}:
            service.resume(operation, actor_id=1)
            now[0] += 316
        assert service.resume(operation, actor_id=1)["phase"] == "completed"
        assert db_session.query(FeishuGroupRoute).count() == 1
    finally:
        recovered.shutdown_all()


def test_no_target_validator_or_clock_evidence_does_not_freeze_source(subject):
    service, manager, bot_id, app_id, _ = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    service.target_validator = None
    with pytest.raises(MigrationBlocked, match="target_live_validation_unavailable"):
        service.resume(operation, actor_id=1)
    assert manager.is_running(bot_id) and not manager.migration_fenced(bot_id)
    service.target_validator = lambda *args: True
    service.maximum_clock_offset = None
    with pytest.raises(MigrationBlocked, match="clock_bound_unverified"):
        service.resume(operation, actor_id=1)
    assert manager.is_running(bot_id) and not manager.migration_fenced(bot_id)


def test_live_holders_are_never_deleted_to_make_cutover_pass(subject):
    service, manager, bot_id, app_id, _ = subject
    instance = manager.get_instance(bot_id)
    state = instance.bot.state.bot_state["node1"]
    state["current_users"] = [{"user_id": "fixture", "start_time": time.time(), "duration": 600}]
    state["status"] = "exclusive"
    with pytest.raises(MigrationBlocked, match="source_has_outstanding_allocations"):
        service.plan(bot_id, app_id, "oc_target", actor_id=1)
    assert state["current_users"][0]["user_id"] == "fixture"
    assert not manager.migration_fenced(bot_id)


def test_fence_cannot_be_bypassed_by_migration_scope_for_resource_writes(subject):
    _, manager, bot_id, _, _ = subject
    manager.install_migration_fence(bot_id, "a" * 32)
    with manager.migration_operation(bot_id, "a" * 32):
        with manager.transition(bot_id):
            pass
        with pytest.raises(InstanceRetiring), manager.admit(bot_id, enforce_cap=False):
            pass
        with manager.admit(bot_id, for_read=True):
            pass
    with pytest.raises(InstanceRetiring), manager.migration_operation(bot_id, "b" * 32):
        pass


def test_frozen_bot_cannot_issue_metrics_links_bindings_or_audit_grants(
    subject, monkeypatch, session_factory, db_session
):
    from fastapi import HTTPException
    from lockbot.backend.app.audit.delegation import _bot_scope
    from lockbot.backend.app.bots import manager as manager_module
    from lockbot.backend.app.bots.metrics_access import issue_metrics_access
    from lockbot.backend.app.resource_api.view_links import BotViewWindow, view_link
    from lockbot.backend.app.resource_auth import service as binding_service
    from lockbot.backend.app.resource_auth.models import ResourceBinding

    service, manager, bot_id, app_id, _ = subject
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    monkeypatch.setattr("lockbot.backend.app.database.SessionLocal", session_factory)
    pending = binding_service.create_binding()
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    assert service.resume(operation, actor_id=1)["phase"] == "checkpointed"
    row = db_session.get(Bot, bot_id)
    assert issue_metrics_access(db_session, row, {"accessMode": "short_lived"}) is None
    assert view_link(db_session, row, "https://example.invalid") == {}
    assert db_session.query(BotViewWindow).count() == 0
    binding_service.handle_chat_binding(
        platform="infoflow",
        external_user_id="fixture",
        bot_id=bot_id,
        action="bind",
        argument=pending["bind_code"],
        expected_revision=0,
    )
    assert db_session.query(ResourceBinding).count() == 0
    assert binding_service.binding_status(db_session, pending["binding_id"]) == "pending"
    with pytest.raises(HTTPException) as denial:
        _bot_scope(db_session, [bot_id])
    assert denial.value.status_code == 409


def test_operation_target_is_idempotent_and_other_destinations_conflict(subject):
    service, _, bot_id, app_id, _ = subject
    operation = "a" * 32
    first = service.plan(bot_id, app_id, "oc_target", actor_id=1, operation_id=operation)
    assert service.plan(bot_id, app_id, "oc_target", actor_id=1, operation_id=operation) == first
    with pytest.raises(MigrationBlocked, match="operation_target_conflict"):
        service.plan(bot_id, app_id, "oc_other", actor_id=1, operation_id=operation)


def test_source_reconfiguration_invalidates_plan_before_freeze(subject, db_session):
    service, manager, bot_id, app_id, _ = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    row = db_session.get(Bot, bot_id)
    row.config_overrides = '{"FORBID_RELOCK":true}'
    db_session.commit()
    with pytest.raises(MigrationBlocked, match="source_configuration_changed"):
        service.resume(operation, actor_id=1)
    assert manager.is_running(bot_id) and not manager.migration_fenced(bot_id)


def test_newly_claimed_target_is_not_overwritten(subject, db_session):
    service, manager, bot_id, app_id, _ = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    db_session.add(FeishuGroupRoute(application_id=app_id, chat_id="oc_target", bot_id=bot_id))
    db_session.commit()
    with pytest.raises(MigrationBlocked, match="source_already_has_feishu_route|target_group_already_bound"):
        service.resume(operation, actor_id=1)
    assert not manager.migration_fenced(bot_id)
    assert db_session.query(FeishuGroupRoute).count() == 1


def test_source_file_edit_after_checkpoint_never_switches_identity(subject, db_session):
    service, manager, bot_id, app_id, now = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    service.resume(operation, actor_id=1)
    path = get_managed_paths().state_file(bot_id)
    data = json.loads(path.read_text())
    data["operator_edit"] = True
    path.write_text(json.dumps(data))
    now[0] += 316
    with pytest.raises(MigrationBlocked, match="source_state_changed_after_checkpoint"):
        service.resume(operation, actor_id=1)
    db_session.expire_all()
    assert db_session.get(Bot, bot_id).platform == "Infoflow"
    assert manager.migration_fenced(bot_id)
    assert db_session.query(FeishuGroupRoute).count() == 0


def test_current_super_administrator_role_is_required_on_each_resume(subject, db_session):
    service, manager, bot_id, app_id, _ = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    db_session.get(User, 1).role = "admin"
    db_session.commit()
    with pytest.raises(MigrationBlocked, match="current_super_administrator_required"):
        service.resume(operation, actor_id=1)
    assert not manager.migration_fenced(bot_id)


def test_previously_admitted_mutation_is_drained_and_blocks_an_empty_cutover(subject, db_session):
    import threading

    from lockbot.core.io import save_bot_state_to_file

    service, manager, bot_id, app_id, _ = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    admitted, release, completed = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            with manager.admit(bot_id) as lease:
                admitted.set()
                assert release.wait(5)
                with lease.bot._lock:
                    entry = lease.bot.state.bot_state["node1"]
                    entry["status"] = "exclusive"
                    entry["current_users"] = [
                        {"user_id": "fixture", "holding_id": "fixture-hold", "start_time": time.time(), "duration": 600}
                    ]
                    save_bot_state_to_file(lease.bot.state.bot_state, lease.bot.config)
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    thread = threading.Thread(target=writer)
    thread.start()
    assert admitted.wait(5)
    service.fault = lambda phase: release.set() if phase == "freeze_committed" else None
    try:
        with pytest.raises(MigrationBlocked, match="source_has_outstanding_allocations"):
            service.resume(operation, actor_id=1)
    finally:
        release.set()
        thread.join(5)
    assert completed.is_set() and not errors
    stored = json.loads(get_managed_paths().state_file(bot_id).read_text())["bot_state"]
    assert stored["node1"]["current_users"][0]["holding_id"] == "fixture-hold"
    assert manager.migration_fenced(bot_id)
    db_session.expire_all()
    assert db_session.get(Bot, bot_id).identity_revision == 0
    assert db_session.query(FeishuGroupRoute).count() == 0


def test_abort_before_switch_preserves_current_source_deadlines_and_keys(subject, db_session):
    from lockbot.core.io import save_bot_state_to_file

    service, manager, bot_id, app_id, _ = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    instance = manager.get_instance(bot_id)
    with instance.bot._lock:
        entry = instance.bot.state.bot_state["node1"]
        entry["status"] = "exclusive"
        entry["current_users"] = [
            {
                "user_id": "fixture",
                "holding_id": "fixture-hold",
                "start_time": time.time() - 5,
                "duration": 600,
                "is_notified": False,
            }
        ]
        original_holder = dict(entry["current_users"][0])
        save_bot_state_to_file(instance.bot.state.bot_state, instance.config)
    # A plan can become stale because an admitted caller acquires resources before confirmation.
    with pytest.raises(MigrationBlocked, match="source_has_outstanding_allocations"):
        service.resume(operation, actor_id=1)
    # Force the active pre-switch recovery phase, as if confirmation had frozen before the late write drained.
    row = db_session.get(BotMigration, operation)
    row.phase = "freezing"
    db_session.commit()
    manager.install_migration_fence(bot_id, operation)
    result = service.abort(operation, actor_id=1)
    assert result["phase"] == "aborted" and not manager.migration_fenced(bot_id)
    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert (row.platform, row.identity_revision, row.api_key) == ("Infoflow", 0, "FAKE_OP")
    assert manager.get_instance(bot_id).bot.state.bot_state["node1"]["current_users"] == [original_holder]
    assert service.abort(operation, actor_id=1) == result


def test_abort_after_identity_switch_cannot_restore_the_old_source_snapshot(subject):
    service, manager, bot_id, app_id, now = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    service.resume(operation, actor_id=1)
    now[0] += 316
    service.fault = lambda phase: (
        (_ for _ in ()).throw(RuntimeError("fixture crash")) if phase == "identity_switched" else None
    )
    with pytest.raises(RuntimeError, match="fixture crash"):
        service.resume(operation, actor_id=1)
    with pytest.raises(MigrationBlocked, match="reverse_migration_requires_separate_confirmation"):
        service.abort(operation, actor_id=1)
    assert manager.migration_fenced(bot_id)


def test_late_source_orm_object_cannot_mint_or_downgrade_target_links(subject, db_session, session_factory):
    from lockbot.backend.app.bots.metrics_access import issue_metrics_access
    from lockbot.backend.app.resource_api.view_links import BotViewWindow, view_link

    service, _, bot_id, app_id, now = subject
    stale_session = session_factory()
    try:
        stale = stale_session.get(Bot, bot_id)
        assert stale.identity_revision == 0
        operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
        service.resume(operation, actor_id=1)
        now[0] += 316
        service.resume(operation, actor_id=1)
        db_session.expire_all()
        current = db_session.get(Bot, bot_id)
        new_link = view_link(db_session, current, "https://example.invalid")
        assert new_link.get("view_url")
        assert view_link(stale_session, stale, "https://example.invalid") == {}
        assert issue_metrics_access(stale_session, stale, {"accessMode": "short_lived"}) is None
        db_session.expire_all()
        assert db_session.get(BotViewWindow, bot_id).identity_revision == 1
        assert view_link(db_session, current, "https://example.invalid") == new_link
    finally:
        stale_session.close()


def test_repeated_plan_request_returns_frozen_operation_without_reserving_again(subject):
    service, _, bot_id, app_id, _ = subject
    operation = "b" * 32
    service.plan(bot_id, app_id, "oc_target", actor_id=1, operation_id=operation)
    current = service.resume(operation, actor_id=1)
    assert current["phase"] == "checkpointed"
    assert service.plan(bot_id, app_id, "oc_target", actor_id=1, operation_id=operation) == current


def test_source_personal_and_detailed_audit_credentials_cannot_cross_revision(
    subject, db_session, client, admin_header, monkeypatch
):
    from lockbot.backend.app.bots import manager as manager_module
    from lockbot.backend.app.resource_auth import credentials
    from lockbot.backend.app.resource_auth import service as auth_service
    from lockbot.backend.app.resource_auth.models import ResourceBinding

    service, manager, bot_id, app_id, now = subject
    monkeypatch.setattr(manager_module, "bot_manager", manager)
    key = credentials.generate_credential()
    row = ResourceBinding(
        public_id=credentials.generate_public_id(),
        platform="infoflow",
        external_user_id="fixture",
        bound_bot_id=bot_id,
        bound_bot_uuid=db_session.get(Bot, bot_id).uuid,
        bound_identity_revision=0,
        credential_digest=credentials.digest_credential(key),
    )
    db_session.add(row)
    db_session.commit()
    grant = client.post(
        "/api/admin/audit-credentials",
        headers=admin_header,
        json={"name": "Fixture audit", "bot_ids": [bot_id], "expires_in_seconds": 600},
    )
    assert grant.status_code == 201
    audit_key = grant.json()["credential"]
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    service.resume(operation, actor_id=1)
    now[0] += 316
    service.resume(operation, actor_id=1)
    db_session.expire_all()
    assert db_session.get(ResourceBinding, row.id).revoked_at is not None
    assert auth_service.authenticate(db_session, bot_id=bot_id, bot_uuid=row.bound_bot_uuid, token=key) is None
    response = client.get(f"/api/audit/bots/{bot_id}/task-notes", headers={"Authorization": "Bearer " + audit_key})
    assert response.status_code == 403


def test_frozen_source_rejects_management_mutation_routes(subject, client, admin_header, monkeypatch):
    from lockbot.backend.app.bots import router as bot_router

    service, manager, bot_id, app_id, _ = subject
    monkeypatch.setattr(bot_router, "bot_manager", manager)
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    service.resume(operation, actor_id=1)
    for action in ("start", "stop", "restart", "api-key"):
        response = client.post(f"/api/bots/{bot_id}/{action}", headers=admin_header)
        assert response.status_code == 503, (action, response.text)
    assert client.put(f"/api/bots/{bot_id}", headers=admin_header, json={"name": "Changed"}).status_code == 503
    assert (
        client.put(f"/api/bots/{bot_id}/resource-auth", headers=admin_header, json={"enabled": False}).status_code
        == 503
    )
    assert client.post(f"/api/bots/{bot_id}/migration-commit", headers=admin_header, json={}).status_code == 404


def test_application_cannot_be_deleted_during_a_plan_but_terminal_journal_does_not_block_removal(
    subject, client, admin_header, db_session
):
    service, _, bot_id, app_id, _ = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    path = f"/api/feishu/applications/{app_id}"
    response = client.delete(path, headers=admin_header)
    assert response.status_code == 409 and "migration" in response.text
    assert db_session.get(FeishuApplication, app_id) is not None
    service.abort(operation, actor_id=1)
    response = client.delete(path, headers=admin_header)
    assert response.status_code == 204, response.text
    db_session.expire_all()
    assert db_session.get(BotMigration, operation).phase == "aborted"


def test_selected_restore_never_copies_an_incomplete_migration_as_an_unfrozen_bot(subject, db_session, monkeypatch):
    import io
    import zipfile

    from lockbot.backend.app.backup import service as backup

    _, _, bot_id, _, _ = subject
    bot = db_session.get(Bot, bot_id)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w"):
        pass
    monkeypatch.setattr(
        backup, "_read_backup_bots", lambda zf: [{"uuid": bot.uuid, "name": "Fixture", "_migration_incomplete": True}]
    )
    result = backup.restore_apply(archive.getvalue(), None, None, [bot.uuid], 1, db_session)
    assert result["restored"] == 0 and result["errors"]
    db_session.expire_all()
    assert (db_session.get(Bot, bot_id).platform, db_session.get(Bot, bot_id).identity_revision) == ("Infoflow", 0)


@pytest.mark.parametrize("phase", ["planned", "completed"])
def test_unknown_journal_version_stays_fenced_and_cannot_resume_or_abort(subject, session_factory, db_session, phase):
    service, manager, bot_id, app_id, _ = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    row = db_session.get(BotMigration, operation)
    row.schema_version, row.phase = 99, phase
    db_session.commit()
    manager.shutdown_all()
    recovered = BotManager()
    try:
        restore_fences(session_factory, recovered)
        assert recovered.migration_fenced(bot_id)
        for action in (service.resume, service.abort):
            with pytest.raises(MigrationBlocked, match="journal_version_unsupported"):
                action(operation, actor_id=1)
        with pytest.raises(MigrationBlocked, match="journal_version_unsupported"):
            service.plan(bot_id, app_id, "oc_target", actor_id=1, operation_id=operation)
    finally:
        recovered.shutdown_all()


def test_post_switch_configuration_change_cannot_activate_target(subject, db_session):
    service, manager, bot_id, app_id, now = subject
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    service.resume(operation, actor_id=1)
    now[0] += 316

    def crash(boundary):
        if boundary == "identity_switched":
            raise RuntimeError("injected")

    service.fault = crash
    with pytest.raises(RuntimeError, match="injected"):
        service.resume(operation, actor_id=1)
    db_session.expire_all()
    db_session.get(Bot, bot_id).config_overrides = '{"DISABLE_RENEWAL":true}'
    db_session.commit()
    service.fault = lambda boundary: None
    with pytest.raises(MigrationBlocked, match="target_configuration_changed"):
        service.resume(operation, actor_id=1)
    assert manager.migration_fenced(bot_id)
    assert not manager.is_running(bot_id)
    db_session.expire_all()
    assert db_session.get(BotMigration, operation).phase == "switched"


@pytest.mark.parametrize("version", [1, 99, None])
def test_archive_unknown_journal_schema_cannot_escape_fence(tmp_path, version):
    import io
    import sqlite3
    import zipfile

    from lockbot.backend.app.backup.service import _read_backup_bots

    path = tmp_path / "archive.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE bots (id INTEGER, name TEXT, bot_type TEXT, platform TEXT, "
            "group_id TEXT, webhook_url TEXT, aes_key TEXT, token TEXT, "
            "cluster_configs TEXT, config_overrides TEXT, is_deleted INTEGER)"
        )
        connection.execute("INSERT INTO bots VALUES (1, 'Fixture', 'NODE', 'Infoflow', '1', '', '', '', '[]', '{}', 0)")
        connection.execute(
            "CREATE TABLE bot_migrations (bot_id INTEGER, phase TEXT"
            + (", schema_version INTEGER)" if version is not None else ")")
        )
        if version is None:
            connection.execute("INSERT INTO bot_migrations VALUES (1, 'completed')")
        else:
            connection.execute("INSERT INTO bot_migrations VALUES (1, 'completed', ?)", (version,))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("lockbot.db", path.read_bytes())
    with zipfile.ZipFile(archive) as target:
        assert _read_backup_bots(target)[0]["_migration_incomplete"] is (version != 1)


def test_absent_optional_metrics_does_not_require_clock_proof_or_wait(subject, db_session):
    service, manager, bot_id, app_id, _ = subject
    db_session.get(Bot, bot_id).utilization_config = None
    db_session.commit()
    service.maximum_clock_offset = None
    service.metrics_validator = None
    operation = service.plan(bot_id, app_id, "oc_target", actor_id=1)["operation_id"]
    assert service.resume(operation, actor_id=1)["phase"] == "completed"
    assert not manager.migration_fenced(bot_id)


@pytest.mark.parametrize(
    "version,phase,fenced",
    [
        (None, "completed", True),
        (1, None, True),
        (None, None, True),
        (1, "planned", False),
        (1, "completed", False),
        (1, "future_phase", True),
    ],
)
def test_malformed_restored_sqlite_null_journal_fields_stay_fenced(tmp_path, version, phase, fenced):
    from lockbot.backend.app.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///" + str(tmp_path / "malformed.sqlite"))
    Base.metadata.create_all(engine)
    # Simulate a malformed/older restored database, not rows permitted by today's NOT NULL schema.
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE bot_migrations RENAME TO strict_original")
        connection.exec_driver_sql("CREATE TABLE bot_migrations AS SELECT * FROM strict_original WHERE 0")
        connection.exec_driver_sql(
            "INSERT INTO bot_migrations (id, bot_id, schema_version, phase) VALUES (?, ?, ?, ?)",
            ("f" * 32, 42, version, phase),
        )
    manager = BotManager()
    try:
        restore_fences(sessionmaker(bind=engine), manager)
        assert manager.migration_fenced(42) is fenced
        assert not manager.is_running(42)
    finally:
        manager.shutdown_all()
        engine.dispose()
