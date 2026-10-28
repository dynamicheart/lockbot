from unittest.mock import Mock

import pytest
import requests
from lockbot.core.config import Config
from lockbot.core.platforms.infoflow import InfoflowAdapter


@pytest.mark.parametrize("mention_all", [False, True])
def test_single_target_and_opt_in_mention(monkeypatch, mention_all):
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"errcode": 0, "data": {"fail": {}}}))
    monkeypatch.setattr("lockbot.core.platforms.infoflow.requests.post", post)
    adapter = InfoflowAdapter(Config({"WEBHOOK_URL": "https://example.test/webhook"}))
    assert adapter.send_broadcast("Announcement", "123", mention_all) == ("accepted", "platform_accepted")
    assert post.call_count == 1
    message = post.call_args.kwargs["json"]["message"]
    assert message["header"]["toid"] == [123]
    mentions = [b for b in message["body"] if b["type"] == "AT"]
    assert mentions == ([{"type": "AT", "atall": True}] if mention_all else [])
    assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "response, expected",
    [
        (Mock(status_code=200, json=lambda: {"errcode": 1}), "failed"),
        (Mock(status_code=200, json=lambda: {"errcode": 0, "data": {"fail": {"123": "denied"}}}), "failed"),
        (Mock(status_code=500), "failed"),
        (Mock(status_code=200, json=lambda: {}), "unknown"),
        (requests.Timeout(), "unknown"),
    ],
)
def test_no_retry_and_no_false_success(monkeypatch, response, expected):
    post = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
    monkeypatch.setattr("lockbot.core.platforms.infoflow.requests.post", post)
    adapter = InfoflowAdapter(Config({"WEBHOOK_URL": "https://example.test/webhook"}))
    assert adapter.send_broadcast("test", "123", True)[0] == expected
    assert post.call_count == 1
