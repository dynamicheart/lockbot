"""AC-8: a non-Infoflow adapter must be able to complete a whole webhook callback.

Codex's review of round 0 found that making the *dispatcher* platform-neutral was not
enough: `handle_webhook` still reached into `msg_data["message"]["header"]["toid"]`, so a
bot on a registered non-Infoflow adapter executed its command — committing a lock — and
then raised `KeyError` while retargeting the reply, returning HTTP 500 with no reply sent.
A retry could then execute the mutation a second time.

These tests pin the fixed behaviour: the full path (verify -> decrypt -> dispatch ->
retarget -> send) completes with no Infoflow payload and no Infoflow field name.
"""

from types import SimpleNamespace

import pytest
from fake_adapter import VALID_SIGNATURE, FakeAdapter, encode_message
from lockbot.backend.app.bots.webhook_handler import handle_webhook
from lockbot.core.bot_instance import BotInstance
from lockbot.core.platforms.registry import register_adapter, unregister_adapter

INFOFLOW_FIELDS = ("message", "header", "body", "fromuserid", "toid", "atuserids")


@pytest.fixture
def fake_platform():
    register_adapter(FakeAdapter)
    yield FakeAdapter.platform_name
    unregister_adapter(FakeAdapter.platform_name)


@pytest.fixture
def fake_bot(fake_platform, tmp_path):
    """A real NodeBot driven by FakeAdapter, as selected from its stored platform.

    DATA_DIR points at a per-test directory so one test's committed lock cannot be loaded
    back as the next test's starting state.
    """
    instance = BotInstance(
        "NODE",
        {
            "BOT_NAME": "neutral",
            "BOT_ID": 4242,
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": {"n1": "m1"},
            "PLATFORM": fake_platform,
        },
    )
    assert isinstance(instance.bot.adapter, FakeAdapter)
    return instance.bot


def test_read_only_command_completes_and_replies(fake_bot):
    body, status, meta = handle_webhook(
        fake_bot,
        raw_form={},
        raw_args={"signature": VALID_SIGNATURE},
        raw_body=encode_message("query"),
    )

    assert status == 200
    assert body == "command succeed"
    assert meta["user_id"] == "fake-actor-1"
    assert meta["command"] == "query"
    assert len(fake_bot.adapter.sent) == 1


def test_webhook_uses_one_parse_for_scope_dispatch_and_reply(fake_bot, monkeypatch):
    from lockbot.core.command_context import command_source

    parsed = []
    observed = []
    original_lock = fake_bot.lock

    def extract_once(message):
        assert not parsed
        result = FakeAdapter.extract_command(fake_bot.adapter, message)
        parsed.append(result)
        return result

    def record_lock(*args, **kwargs):
        observed.append(command_source.get())
        return original_lock(*args, **kwargs)

    group = "authorized-conversation"
    fake_bot.config.set_val("_AUTHORIZED_GROUP_IDS", [group])
    monkeypatch.setattr(fake_bot.adapter, "extract_command", extract_once)
    monkeypatch.setattr(fake_bot, "lock", record_lock)

    _, status, meta = handle_webhook(
        fake_bot,
        raw_form={},
        raw_args={"signature": VALID_SIGNATURE},
        raw_body=encode_message("lock n1 1h", actor="owner", conversation=group),
    )

    assert status == 200
    assert parsed == [("owner", group, "lock n1 1h")]
    assert observed == ["chat"]
    assert meta == {"user_id": "owner", "group_id": group, "command": "lock n1 1h"}
    assert fake_bot.adapter.sent[0]["envelope"]["conversation"] == group


def test_webhook_refuses_an_unlisted_group_before_dispatch(fake_bot):
    fake_bot.config.set_val("_AUTHORIZED_GROUP_IDS", ["allowed-group"])

    result = handle_webhook(
        fake_bot,
        raw_form={},
        raw_args={"signature": VALID_SIGNATURE},
        raw_body=encode_message("lock n1", conversation="other-group"),
    )

    assert result == ("conversation not authorized", 403, {"event": "scope_refused"})
    assert fake_bot.state.bot_state["n1"]["status"] == "idle"
    assert fake_bot.adapter.sent == []


@pytest.mark.parametrize("restricted", [False, True])
def test_webhook_rejects_unparseable_input_without_retry(fake_bot, monkeypatch, restricted):
    if restricted:
        fake_bot.config.set_val("_AUTHORIZED_GROUP_IDS", ["allowed-group"])

    calls = []

    def malformed(message):
        calls.append(message)
        raise ValueError("malformed command")

    monkeypatch.setattr(fake_bot.adapter, "extract_command", malformed)

    def callback():
        return handle_webhook(
            fake_bot,
            raw_form={},
            raw_args={"signature": VALID_SIGNATURE},
            raw_body=encode_message("lock n1"),
        )

    if restricted:
        assert callback() == ("conversation not authorized", 403, {"event": "scope_refused"})
    else:
        with pytest.raises(ValueError, match="malformed command"):
            callback()

    assert len(calls) == 1
    assert fake_bot.state.bot_state["n1"]["status"] == "idle"
    assert fake_bot.adapter.sent == []


def test_mutating_command_commits_and_still_replies(fake_bot):
    """The reported failure mode: the lock committed but the reply blew up afterwards."""
    body, status, _ = handle_webhook(
        fake_bot,
        raw_form={},
        raw_args={"signature": VALID_SIGNATURE},
        raw_body=encode_message("lock n1 1h", actor="fake-actor-9"),
    )

    assert status == 200
    assert body == "command succeed"
    holders = [u["user_id"] for u in fake_bot.state.bot_state["n1"]["current_users"]]
    assert holders == ["fake-actor-9"], "the lock must still be committed"
    assert len(fake_bot.adapter.sent) == 1, "and the reply must have been sent, not dropped"


def test_reply_is_retargeted_through_the_adapter(fake_bot):
    handle_webhook(
        fake_bot,
        raw_form={},
        raw_args={"signature": VALID_SIGNATURE},
        raw_body=encode_message("query", conversation="fake-conversation-77"),
    )

    sent = fake_bot.adapter.sent[0]
    assert sent["envelope"]["conversation"] == "fake-conversation-77"


def test_no_infoflow_field_name_appears_anywhere(fake_bot):
    handle_webhook(
        fake_bot,
        raw_form={},
        raw_args={"signature": VALID_SIGNATURE},
        raw_body=encode_message("query"),
    )

    sent = fake_bot.adapter.sent[0]
    assert set(sent) == {"envelope"}
    for field in INFOFLOW_FIELDS:
        assert field not in sent


def test_url_verification_uses_the_adapter(fake_bot):
    body, status, meta = handle_webhook(
        fake_bot,
        raw_form={"echostr": "neutral-echo", "signature": VALID_SIGNATURE, "rn": "1", "timestamp": "2"},
        raw_args={},
        raw_body=None,
    )
    assert (body, status) == ("neutral-echo", 200)
    assert meta == {"event": "url_verification"}


def test_bad_signature_is_refused_before_dispatch(fake_bot):
    body, status, _ = handle_webhook(
        fake_bot,
        raw_form={},
        raw_args={"signature": "wrong"},
        raw_body=encode_message("lock n1 1h"),
    )
    assert status == 401
    assert body == "check signature fail"
    assert fake_bot.state.bot_state["n1"]["status"] == "idle"
    assert fake_bot.adapter.sent == []


def test_undecryptable_payload_is_refused_before_dispatch(fake_bot):
    body, status, _ = handle_webhook(
        fake_bot,
        raw_form={},
        raw_args={"signature": VALID_SIGNATURE},
        raw_body=b"not-json",
    )
    assert status == 400
    assert body == "decrypt failed"
    assert fake_bot.adapter.sent == []


def test_bot_without_a_real_adapter_falls_back_to_the_default_platform(tmp_path):
    """Pre-existing behaviour: a bot exposing no MessageAdapter still serves callbacks."""
    stub = BotInstance("NODE", {"BOT_NAME": "x", "DATA_DIR": str(tmp_path)}).bot
    bot = SimpleNamespace(adapter=None, config=stub.config)
    body, status, _ = handle_webhook(bot, raw_form={}, raw_args={"signature": "nope"}, raw_body=None)
    assert status == 401
    assert body == "check signature fail"
