import json
from types import SimpleNamespace

import pytest
import requests
from lockbot.core.platforms.feishu import FeishuClient, FeishuPanelAPIError


@pytest.mark.parametrize("mutation", [None, "sender", "chat", "id", "sender_type"])
def test_create_only_adopts_verified_own_app_message(monkeypatch, mutation):
    data = {
        "message_id": "om_own",
        "chat_id": "oc_group",
        "sender": {"id": "cli_app", "id_type": "app_id", "sender_type": "app"},
    }
    if mutation == "sender":
        data["sender"]["id"] = "cli_other"
    if mutation == "chat":
        data["chat_id"] = "oc_other"
    if mutation == "id":
        data["message_id"] = "../../foreign"
    if mutation == "sender_type":
        data["sender"]["sender_type"] = "user"
    client = FeishuClient("cli_app", "secret")
    monkeypatch.setattr(client, "token", lambda: "test-token")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": data})

    monkeypatch.setattr(requests, "post", post)
    payload = {"receive_id": "oc_group", "msg_type": "post", "content": "{}", "uuid": "random"}
    assert client.create_query_panel(payload) == (None if mutation else "om_own")
    assert calls[0][1]["json"] == payload
    assert calls[0][1]["params"] == {"receive_id_type": "chat_id"}
    assert calls[0][1]["timeout"] == (3, 8)
    assert calls[0][1]["allow_redirects"] is False


def test_update_uses_put_preserves_native_post_and_no_receive_or_uuid(monkeypatch):
    client = FeishuClient("cli_app", "secret")
    monkeypatch.setattr(client, "token", lambda: "token")
    calls = []
    monkeypatch.setattr(
        requests,
        "put",
        lambda url, **kwargs: calls.append((url, kwargs)) or SimpleNamespace(status_code=200, json=lambda: {"code": 0}),
    )
    content = json.dumps(
        {
            "zh_cn": {
                "title": "",
                "content": [
                    [{"tag": "a", "href": "https://example.com", "text": "链接"}],
                    [{"tag": "at", "user_id": "ou_alice"}],
                ],
            }
        },
        ensure_ascii=False,
    )
    client.update_query_panel("om_own", {"content": content, "receive_id": "oc_group", "uuid": "old"})
    assert calls[0][0].endswith("/messages/om_own")
    assert calls[0][1]["json"] == {"msg_type": "post", "content": content}


@pytest.mark.parametrize("response", ["timeout", "invalid", "permission"])
def test_panel_requests_fail_once_without_retries_or_error_body_leak(monkeypatch, response):
    client = FeishuClient("cli_app", "secret")
    monkeypatch.setattr(client, "token", lambda: "token")
    calls = []

    def put(*args, **kwargs):
        calls.append(1)
        if response == "timeout":
            raise requests.Timeout("SECRET_DETAILS")
        return SimpleNamespace(
            status_code=200, json=lambda: {"code": 230027, "msg": "SECRET_DETAILS"} if response == "permission" else []
        )

    monkeypatch.setattr(requests, "put", put)
    with pytest.raises(FeishuPanelAPIError) as error:
        client.update_query_panel("om_own", {"content": "{}"})
    assert calls == [1]
    assert "SECRET" not in str(error.value)
    assert error.value.uncertain == (response != "permission")
