"""Optional reactions must never infer allocation success or alter legacy bodies."""

import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests
from lockbot.core.command_result import CommandResult, capture_result, completed_reply
from lockbot.core.config import Config
from lockbot.core.handler import execute_text_command
from lockbot.core.node_bot import NodeBot
from lockbot.core.platforms.feishu import FeishuAdapter, FeishuAPIError, FeishuClient
from lockbot.core.platforms.infoflow import InfoflowAdapter
from lockbot.core.queue_bot import QueueBot


def configured(tmp_path, kind=NodeBot, **extra):
    return kind(
        config_dict={"BOT_NAME": "reaction-fixture", "CLUSTER_CONFIGS": ["node_a"], "DATA_DIR": str(tmp_path), **extra}
    )


def test_capture_is_nested_request_local_and_reset_after_exceptions():
    barrier = threading.Barrier(2)

    def run(operation):
        with capture_result() as outer:
            completed_reply({}, operation)
            barrier.wait()
            with pytest.raises(RuntimeError), capture_result() as inner:
                completed_reply({}, "other")
                raise RuntimeError()
            assert inner.operation == "other"
            assert outer.operation == operation
            return outer

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(run, ["lock", "unlock"]))
    assert [item.operation for item in values] == ["lock", "unlock"]
    completed_reply({}, "alias")  # no active capture: cannot mutate an old result
    assert [item.operation for item in values] == ["lock", "unlock"]
    with capture_result() as clean:
        assert not clean.committed


def test_success_requires_completed_persistence_and_rejections_stay_unknown(tmp_path, monkeypatch):
    bot = configured(tmp_path)
    with capture_result() as success:
        execute_text_command("alice", "lock node_a 2m", bot)
    assert success == CommandResult("lock", True, True)
    with capture_result() as rejected:
        execute_text_command("bob", "lock node_a 2m", bot)
    assert not rejected.committed
    monkeypatch.setattr("lockbot.core.io._save_bot_state_atomically", Mock(side_effect=OSError("fixture")))
    with capture_result() as uncertain, pytest.raises(OSError):
        execute_text_command("alice", "unlock node_a", bot)
    assert not uncertain.committed


def test_queue_extension_warning_keeps_original_body(tmp_path):
    bot = configured(tmp_path, QueueBot)
    execute_text_command("alice", "lock node_a 2m", bot)
    execute_text_command("bob", "book node_a 2m", bot)
    with capture_result() as result:
        reply = execute_text_command("alice", "lock node_a 2m", bot)
    assert result.committed and not result.reaction_safe
    assert "bob" in str(reply)
    with capture_result() as queued:
        execute_text_command("charlie", "book node_a 2m", bot)
    assert not queued.reaction_safe


@pytest.mark.parametrize("platform", ["infoflow", "feishu"])
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_capture_keeps_existing_reply_payloads_byte_for_byte(tmp_path, monkeypatch, platform, lang):
    monkeypatch.setattr("time.time", lambda: 1800000000)
    monkeypatch.setattr("lockbot.core.platforms.feishu.uuid.uuid4", lambda: uuid.UUID(int=1))
    adapter_type = InfoflowAdapter if platform == "infoflow" else FeishuAdapter
    plain = configured(tmp_path / "plain", LANGUAGE=lang)
    captured = configured(tmp_path / "captured", LANGUAGE=lang)
    plain.adapter = adapter_type(plain.config)
    captured.adapter = adapter_type(captured.config)
    for command in ["help", "query", "lock node_a 2m", "book node_a 2m", "unlock node_a", "unknown"]:
        expected = copy.deepcopy(execute_text_command("ou_alice", command, plain))
        with capture_result():
            actual = execute_text_command("ou_alice", command, captured)
        assert actual == expected


@pytest.mark.parametrize(
    "enabled,committed,safe,operation,group",
    [
        (False, True, True, "lock", "oc_demo"),
        (True, False, True, "lock", "oc_demo"),
        (True, True, False, "lock", "oc_demo"),
        (True, True, True, "book", "oc_demo"),
        (True, True, True, "query", "oc_demo"),
        (True, True, True, "lock", "oc_other"),
    ],
)
def test_only_opted_in_safe_success_replaces_reply(enabled, committed, safe, operation, group):
    client = Mock(spec=FeishuClient)
    adapter = FeishuAdapter(
        Config({"FEISHU_REACTION_REPLIES": enabled, "_AUTHORIZED_GROUP_IDS": ["oc_demo"], "_FEISHU_CLIENT": client})
    )
    adapter.send = Mock(return_value=[(200, "platform_accepted")])
    reply = {"receive_id": "oc_demo", "content": "unchanged"}
    adapter.send_command_reply(
        reply, {"group_id": group, "message_id": "om_demo"}, CommandResult(operation, committed, safe)
    )
    adapter.send.assert_called_once_with(reply)
    client.react.assert_not_called()


@pytest.mark.parametrize("operation", ["lock", "slock", "unlock", "alias"])
def test_success_uses_one_reaction_without_duplicate_text_and_failure_falls_back(operation):
    client = Mock(spec=FeishuClient)
    adapter = FeishuAdapter(
        Config({"FEISHU_REACTION_REPLIES": True, "_AUTHORIZED_GROUP_IDS": ["oc_demo"], "_FEISHU_CLIENT": client})
    )
    adapter.send = Mock()
    reply = {"receive_id": "oc_demo", "content": "original reply"}
    message = {"group_id": "oc_demo", "message_id": "om_demo"}
    result = CommandResult(operation, True, True)
    adapter.send_command_reply(reply, message, result)
    client.react.assert_called_once_with("om_demo")
    adapter.send.assert_not_called()
    client.react.side_effect = FeishuAPIError("permission or timeout")
    adapter.send_command_reply(reply, message, result)
    adapter.send.assert_called_once_with(reply)
    assert client.react.call_count == 2  # one attempt per delivery; never retry the command


@pytest.mark.parametrize("error", [requests.Timeout(), None])
def test_reaction_http_permission_and_timeout_have_one_attempt(monkeypatch, error):
    client = FeishuClient("cli_fixture", "not-a-secret")
    monkeypatch.setattr(client, "token", lambda: "fixture")
    post = Mock(side_effect=error, return_value=Mock(status_code=403, json=lambda: {"code": 99991672}))
    monkeypatch.setattr("requests.post", post)
    with pytest.raises(FeishuAPIError):
        client.react("om_demo")
    assert post.call_count == 1
    assert post.call_args.kwargs["json"] == {"reaction_type": {"emoji_type": "DONE"}}
    with pytest.raises(FeishuAPIError):
        client.react("om_bad/path")
    assert post.call_count == 1


def test_alias_never_claims_persistence_from_best_effort_failure(tmp_path, monkeypatch):
    bot = configured(tmp_path, ALLOW_USER_ALIAS=True)
    monkeypatch.setattr(bot, "_persist_config_override", lambda *_: None)
    with capture_result() as uncertain:
        execute_text_command("alice", "alias node_a short", bot)
    assert not uncertain.committed
    monkeypatch.setattr(bot, "_persist_config_override", lambda *_: True)
    with capture_result() as confirmed:
        execute_text_command("alice", "alias node_a short", bot)
    assert confirmed == CommandResult("alias", True, True)
