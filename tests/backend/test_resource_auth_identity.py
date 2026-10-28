"""B1 / AC-2: a binding's authorization scope is the bot's authoritative identity.

Two independent weaknesses let a caller choose which bot a confirmed binding is written
against, which is the identity the resource API later trusts:

- ``_build_config_dict`` merged ``config_overrides`` *after* the authoritative BOT_ID /
  BOT_TYPE / PLATFORM, so an override row could silently reassign the running bot's identity.
- the injected binding hook took its ``bot_id`` and ``platform`` from that mutable config
  (via the dispatcher), so whatever the config said became the authorization scope.

These tests pin the fix: identity comes from the DB row id and the platform resolved at
start, the override can never reassign it, and the hook refuses a mismatched claim.
"""

from unittest.mock import MagicMock, patch

import pytest
from fake_adapter import FakeAdapter
from lockbot.backend.app.bots.manager import BotManager
from lockbot.backend.app.bots.router import _build_config_dict
from lockbot.core.platforms.registry import register_adapter, unregister_adapter


def _make_bot(client, auth_header, name, *, enabled=False):
    resp = client.post(
        "/api/bots",
        headers=auth_header,
        json={
            "name": name,
            "bot_type": "NODE",
            "webhook_url": "https://example.invalid/hook",
            "cluster_configs": {"n1": "m1"},
        },
    )
    assert resp.status_code == 201, resp.text
    bot_id = resp.json()["id"]
    if enabled:
        got = client.put(f"/api/bots/{bot_id}/resource-auth", headers=auth_header, json={"enabled": True})
        assert got.status_code == 200, got.text
    return bot_id


class TestOverridesCannotReassignIdentity:
    @pytest.mark.parametrize(
        "key, poison",
        [("BOT_ID", 999999), ("BOT_TYPE", "QUEUE"), ("PLATFORM", "fakeim")],
    )
    def test_override_cannot_replace_authoritative_identity(self, client, auth_header, db_session, key, poison):
        from lockbot.backend.app.bots.models import Bot

        bot_id = _make_bot(client, auth_header, "victim")
        baseline = _build_config_dict(db_session.get(Bot, bot_id))[key]

        client.put(f"/api/bots/{bot_id}", headers=auth_header, json={"config_overrides": {key: poison}})
        db_session.expire_all()

        config = _build_config_dict(db_session.get(Bot, bot_id))
        assert config[key] == baseline != poison

    def test_harmless_overrides_still_apply(self, client, auth_header, db_session):
        from lockbot.backend.app.bots.models import Bot

        bot_id = _make_bot(client, auth_header, "tweaked")
        client.put(f"/api/bots/{bot_id}", headers=auth_header, json={"config_overrides": {"MAX_LOCK_DURATION": 1800}})
        db_session.expire_all()

        config = _build_config_dict(db_session.get(Bot, bot_id))
        assert config["MAX_LOCK_DURATION"] == 1800

    def test_one_bot_cannot_borrow_anothers_id_via_override(self, client, auth_header, db_session):
        """Cross-bot: a disabled bot whose override claims an enabled bot's id stays itself."""
        from lockbot.backend.app.bots.models import Bot

        target = _make_bot(client, auth_header, "target", enabled=True)
        attacker = _make_bot(client, auth_header, "attacker", enabled=False)
        client.put(f"/api/bots/{attacker}", headers=auth_header, json={"config_overrides": {"BOT_ID": target}})
        db_session.expire_all()

        config = _build_config_dict(db_session.get(Bot, attacker))
        assert config["BOT_ID"] == attacker, "the override must not let the attacker bot claim the target's id"

    def test_a_bot_cannot_borrow_a_different_owners_bot_id(self, client, auth_header, db_session):
        """Cross-ownership: the two bots are owned by different users, as the contract requires."""
        from lockbot.backend.app.bots.models import Bot

        target = _make_bot(client, auth_header, "target", enabled=True)  # owned by the default user

        client.post("/api/auth/register", json={"username": "mallory", "email": "m@test.com", "password": "mallorypw1"})
        other = client.post("/api/auth/login", json={"username": "mallory", "password": "mallorypw1"}).json()
        other_header = {"Authorization": f"Bearer {other['access_token']}"}

        attacker = _make_bot(client, other_header, "attacker", enabled=False)  # owned by mallory
        client.put(f"/api/bots/{attacker}", headers=other_header, json={"config_overrides": {"BOT_ID": target}})
        db_session.expire_all()

        config = _build_config_dict(db_session.get(Bot, attacker))
        assert config["BOT_ID"] == attacker, "one owner's bot must not assume another owner's bot id"
        assert config["BOT_ID"] != target


@pytest.fixture
def fakeim():
    register_adapter(FakeAdapter)
    yield FakeAdapter.platform_name
    unregister_adapter(FakeAdapter.platform_name)


class TestInjectedHookBindsToAuthoritativeIdentity:
    """The hook the manager injects derives its scope from the manager, not from config."""

    def _hook_for(self, bot_id, platform):
        manager = BotManager()
        with patch("lockbot.backend.app.bots.manager.BotInstance", return_value=MagicMock()):
            manager.start_bot(bot_id=bot_id, config_dict={"BOT_TYPE": "NODE", "PLATFORM": platform})
        return manager.get_instance(bot_id).bot.binding_hook

    def test_matching_claim_binds_against_the_managers_own_id(self):
        hook = self._hook_for(100, "infoflow")
        with patch("lockbot.backend.app.bots.manager.handle_chat_binding") as impl:
            impl.return_value = "ok"
            hook(platform="infoflow", external_user_id="alice", bot_id=100, action="bind", argument="CODE")
        impl.assert_called_once()
        assert impl.call_args.kwargs["bot_id"] == 100
        assert impl.call_args.kwargs["platform"] == "infoflow"

    def test_a_mismatched_bot_id_claim_is_refused(self):
        hook = self._hook_for(100, "infoflow")
        with patch("lockbot.backend.app.bots.manager.handle_chat_binding") as impl, pytest.raises(RuntimeError):
            hook(platform="infoflow", external_user_id="alice", bot_id=999, action="bind", argument="CODE")
        impl.assert_not_called()

    def test_a_mismatched_platform_claim_is_refused(self, fakeim):
        hook = self._hook_for(100, "infoflow")
        with patch("lockbot.backend.app.bots.manager.handle_chat_binding") as impl, pytest.raises(RuntimeError):
            hook(platform=fakeim, external_user_id="alice", bot_id=100, action="bind", argument="CODE")
        impl.assert_not_called()
