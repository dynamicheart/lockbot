"""Permanent regression tests for the 2026-10-24 plan-completion review (R1–R5).

Each test reproduces a concrete issue the review found and pins the fix. See
``docs/plan-completion-review-2026-10-24.md`` (the review) and
``docs/review-fixes-2026-10-24.md`` (the fix record).
"""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name

import os
from datetime import datetime, timezone

import pytest
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.resource_auth.models import ResourceBinding

from backend.test_backup_restore import _build_backup_zip
from backend.test_resource_api_resources import api  # noqa: F401

NODE_BOT = {
    "name": "review-bot",
    "bot_type": "NODE",
    "webhook_url": "https://example.com/old-hook",
    "aes_key": "oldaes",
    "token": "oldtok",
    "cluster_configs": ["node1"],
}


def _make_bot(client, admin_header, **overrides):
    body = {**NODE_BOT, **overrides}
    resp = client.post("/api/bots", json=body, headers=admin_header)
    assert resp.status_code == 201, resp.text
    return resp.json()


# =====================================================================================
# R1 — a restore whose state-file install fails must NOT leave a startable new-config /
# old-state mismatch: the bot is fenced into a persisted ``recovery_required`` status that
# every start entry refuses.
# =====================================================================================
def test_r1_restore_state_install_failure_fences_bot(client, admin_header, db_session, monkeypatch):
    from lockbot.backend.app.backup.service import restore_apply

    created = _make_bot(client, admin_header, name="r1-bot", cluster_configs=["node1"])
    bot_id, bot_uuid = created["id"], created["uuid"]
    admin_id = db_session.get(Bot, bot_id).user_id

    # Archive carries a DIFFERENT config (node2) plus a state file, so a surviving old state
    # would be loaded under the new config and silently drop occupancy.
    zip_bytes = _build_backup_zip(
        bot_uuid=bot_uuid,
        bot_name="r1-restored",
        bot_id=bot_id,
        state_data={"bot_state": {"node1": {"status": "idle", "current_users": [], "booking_list": []}}},
    )

    real_replace = os.replace

    def _boom(src, dst, *a, **k):
        if str(dst).endswith("bot_state.json"):
            raise OSError("simulated disk failure installing state")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _boom)

    result = restore_apply(zip_bytes, None, None, [bot_uuid], admin_id, db_session)

    assert result["restored"] == 0
    assert result["errors"], "a failed state install must be reported, not silently counted as success"

    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert row.status == "recovery_required"

    # Every start entry must refuse the fenced bot (the status is persisted, survives restart).
    monkeypatch.setattr(os, "replace", real_replace)
    start = client.post(f"/api/bots/{bot_id}/start", headers=admin_header)
    assert start.status_code == 409
    restart = client.post(f"/api/bots/{bot_id}/restart", headers=admin_header)
    assert restart.status_code == 409


def test_r1_recovery_required_bot_is_not_auto_recovered(client, admin_header, db_session, monkeypatch, session_factory):
    """A fenced bot must not be swept back into service by startup auto-recovery."""
    import lockbot.backend.app.main as main_mod

    created = _make_bot(client, admin_header, name="r1-norecover")
    bot_id = created["id"]
    row = db_session.get(Bot, bot_id)
    row.status = "recovery_required"
    db_session.commit()

    # Point auto-recovery at the test database so it sees the fenced row.
    monkeypatch.setattr(main_mod, "SessionLocal", session_factory)
    main_mod._reset_running_bots()  # selects only running/error candidates

    db_session.expire_all()
    assert db_session.get(Bot, bot_id).status == "recovery_required"


# =====================================================================================
# R2 — the bot uuid is unique across the WHOLE table (tombstones included); a duplicate
# (direct, concurrent, or a recreate over a soft-deleted row) is impossible.
# =====================================================================================
def test_r2_uuid_unique_index_rejects_duplicate(client, admin_header, db_session):
    from sqlalchemy.exc import IntegrityError

    created = _make_bot(client, admin_header, name="r2-orig")
    orig = db_session.query(Bot).filter(Bot.uuid == created["uuid"]).first()

    dup = Bot(
        uuid=created["uuid"],  # same uuid as an existing row
        user_id=orig.user_id,
        name="r2-dup",
        bot_type="NODE",
        platform="Infoflow",
        webhook_url=orig.webhook_url,
        aes_key="",
        token="",
        cluster_configs="[]",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_r2_restore_create_rejects_tombstoned_uuid(client, admin_header, db_session):
    """A soft-deleted row owning the uuid is a conflict, not a free slot: no silent recreate."""
    from lockbot.backend.app.backup.service import restore_apply

    created = _make_bot(client, admin_header, name="r2-tomb")
    bot_id, bot_uuid = created["id"], created["uuid"]
    row = db_session.get(Bot, bot_id)
    admin_id = row.user_id
    row.is_deleted = True
    row.deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    zip_bytes = _build_backup_zip(bot_uuid=bot_uuid, bot_name="r2-revived", bot_id=bot_id)
    result = restore_apply(zip_bytes, None, None, [bot_uuid], admin_id, db_session)

    assert result["created"] == 0
    assert result["overwritten"] == 0
    assert any("already exists" in e for e in result["errors"])
    # No new live row sprang up next to the tombstone.
    assert db_session.query(Bot).filter(Bot.uuid == bot_uuid, Bot.is_deleted.is_(False)).first() is None


# =====================================================================================
# R3 — a webhook's post-lease metadata tail-writes (discovered group_id / last_user_id)
# are fenced on the dispatch generation still being current, so a stale generation cannot
# resurrect an old identity across a restore/cutover.
# =====================================================================================
def _webhook_setup(monkeypatch, *, current):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import lockbot.backend.app.bots.router as router_mod

    @contextmanager
    def _admit(bot_id):
        yield SimpleNamespace(bot=object(), generation=1, instance=None, bot_id=bot_id)

    monkeypatch.setattr(router_mod.bot_manager, "admit", _admit)
    monkeypatch.setattr(
        router_mod,
        "handle_webhook",
        lambda bot, **kw: ("ok", 200, {"user_id": "ghost-user", "group_id": "ghost-group"}),
    )
    monkeypatch.setattr(router_mod.bot_manager, "lease_is_current", lambda lease: current)


def test_r3_stale_generation_tailwrites_are_dropped(client, admin_header, db_session, monkeypatch):
    created = _make_bot(client, admin_header, name="r3-stale", group_id="authoritative-group")
    bot_id = created["id"]

    _webhook_setup(monkeypatch, current=False)  # the generation that dispatched has been retired
    resp = client.post(f"/api/bots/webhook/{bot_id}", data={})
    assert resp.status_code == 200

    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert row.group_id == "authoritative-group"  # not polluted by the stale observation
    assert row.last_user_id is None


def test_r3_current_generation_tailwrites_apply(client, admin_header, db_session, monkeypatch):
    created = _make_bot(client, admin_header, name="r3-current", group_id="authoritative-group")
    bot_id = created["id"]

    _webhook_setup(monkeypatch, current=True)  # positive control: a live generation still records
    resp = client.post(f"/api/bots/webhook/{bot_id}", data={})
    assert resp.status_code == 200

    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert row.group_id == "authoritative-group"
    assert "ghost-group" in (row.observed_group_ids or "")
    assert row.last_user_id == "ghost-user"


# =====================================================================================
# R4 — lifecycle / identity-cutover boundaries (closable sub-items).
# =====================================================================================
def _add_binding(db_session, bot, public_id):
    binding = ResourceBinding(
        public_id=public_id,
        platform="infoflow",
        external_user_id="agent-1",
        bound_bot_id=bot["id"],
        bound_bot_uuid=bot["uuid"],
        credential_digest="d" * 64,
        label="agent",
    )
    db_session.add(binding)
    db_session.commit()
    return public_id


def test_r4a_credential_change_revokes_bindings(client, admin_header, db_session):
    """Changing the signing credentials is an identity cutover: old agent bindings are revoked."""
    created = _make_bot(client, admin_header, name="r4a-bot")
    _add_binding(db_session, created, "r4a-pid")

    resp = client.put(
        f"/api/bots/{created['id']}",
        json={"webhook_url": "https://example.com/new-hook"},
        headers=admin_header,
    )
    assert resp.status_code == 200

    db_session.expire_all()
    binding = db_session.query(ResourceBinding).filter_by(public_id="r4a-pid").first()
    assert binding.revoked_at is not None


def test_r4a_noncredential_change_keeps_bindings(client, admin_header, db_session):
    """A plain config edit that leaves webhook/aes/token untouched keeps durable credentials."""
    created = _make_bot(client, admin_header, name="r4a-keep")
    _add_binding(db_session, created, "r4a-keep-pid")

    resp = client.put(f"/api/bots/{created['id']}", json={"name": "new-display-name"}, headers=admin_header)
    assert resp.status_code == 200

    db_session.expire_all()
    binding = db_session.query(ResourceBinding).filter_by(public_id="r4a-keep-pid").first()
    assert binding.revoked_at is None


def test_r4b_reset_row_does_not_claim_stopped_when_cleanup_unconfirmed(client, admin_header, db_session, monkeypatch):
    """If the drain cannot confirm cleanup (MayHaveExecuted), the row must NOT be reported stopped."""
    import lockbot.backend.app.bots.router as router_mod
    from lockbot.backend.app.bots.manager import MayHaveExecuted

    created = _make_bot(client, admin_header, name="r4b-bot")
    bot_id = created["id"]
    row = db_session.get(Bot, bot_id)
    row.status = "running"
    row.pid = 4242
    db_session.commit()

    def _raise_may_have_executed(*a, **k):
        raise MayHaveExecuted(bot_id)

    monkeypatch.setattr(router_mod.bot_manager, "stop_bot", _raise_may_have_executed)
    router_mod._reset_row_to_stopped(db_session, bot_id)

    db_session.expire_all()
    row = db_session.get(Bot, bot_id)
    assert row.status == "running"  # never a false "stopped" while a worker may still persist
    assert row.pid == 4242


# =====================================================================================
# R5 — the per-identity holdings cap is judged on PROJECTED holdings (current ∪ targets),
# so a multi-target single request cannot bypass it and a renewal/claim is not falsely blocked.
# =====================================================================================
def _set_cap(monkeypatch, n):
    import lockbot.backend.app.resource_api.commands as commands_mod

    monkeypatch.setattr(commands_mod, "RESOURCE_API_MAX_HELD_PER_IDENTITY", n)


def _cmd(client, api, command):
    return client.post(f"/api/bots/{api['bot_id']}/commands", headers=api["headers"], json={"command": command})


def test_r5_multi_target_single_request_cannot_bypass_cap(client, api, monkeypatch):
    _set_cap(monkeypatch, 1)
    # One request targeting two nodes would take the caller to 2 held, past the cap of 1.
    resp = _cmd(client, api, "lock n1,n2 1h")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "identity_cap_reached"


def test_r5_renewal_of_held_resource_is_not_blocked(client, api, monkeypatch):
    _set_cap(monkeypatch, 1)
    assert _cmd(client, api, "lock n1 1h").status_code == 200  # now holds 1 (== cap)
    # Renewing the SAME node adds no new resource, so it must not be rejected by the cap.
    assert _cmd(client, api, "lock n1 1h").status_code == 200


def test_r5_additional_distinct_resource_past_cap_is_blocked(client, api, monkeypatch):
    _set_cap(monkeypatch, 1)
    assert _cmd(client, api, "lock n1 1h").status_code == 200
    # A DIFFERENT node would grow holdings to 2, past the cap.
    resp = _cmd(client, api, "lock n2 1h")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "identity_cap_reached"
