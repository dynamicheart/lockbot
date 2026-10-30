"""The own-bot probe is uncached, strictly typed, proxy-aware and bounded."""

from types import SimpleNamespace

import pytest
from lockbot.core.platforms.feishu import FeishuAPIError, FeishuClient


@pytest.mark.parametrize("present", [True, False])
def test_own_bot_membership_probe_is_uncached_and_uses_proxy(monkeypatch, present):
    calls = []
    monkeypatch.setattr(FeishuClient, "token", lambda _: "FAKE_TOKEN")
    monkeypatch.setattr(
        FeishuClient, "_http_options", property(lambda _: {"proxies": {"https": "http://proxy.invalid"}})
    )

    def get(url, **options):
        calls.append((url, options))
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": {"is_in_chat": present}})

    monkeypatch.setattr("lockbot.core.platforms.feishu.requests.get", get)
    client = FeishuClient("cli_fixture", "FAKE_SECRET")
    assert client.bot_in_group("oc_fixture") is present
    assert client.bot_in_group("oc_fixture") is present
    assert len(calls) == 2
    for url, options in calls:
        assert url.endswith("/im/v1/chats/oc_fixture/members/is_in_chat")
        assert options["timeout"] == (3, 5) and options["allow_redirects"] is False
        assert options["proxies"]["https"] == "http://proxy.invalid"


@pytest.mark.parametrize(
    "body",
    [
        {"code": 0, "data": {"is_in_chat": "true"}},
        {"code": 0, "data": {"is_in_chat": 1}},
        {"code": 0, "data": {}},
        {"code": 99991672},
    ],
)
def test_membership_probe_does_not_coerce_or_swallow_provider_failure(monkeypatch, body):
    monkeypatch.setattr(FeishuClient, "token", lambda _: "FAKE_TOKEN")
    monkeypatch.setattr(
        "lockbot.core.platforms.feishu.requests.get",
        lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: body),
    )
    with pytest.raises(FeishuAPIError):
        FeishuClient("cli_fixture", "FAKE_SECRET").bot_in_group("oc_fixture")


def test_membership_probe_rejects_invalid_chat_before_network(monkeypatch):
    monkeypatch.setattr(FeishuClient, "token", lambda _: pytest.fail("network must not run"))
    with pytest.raises(ValueError):
        FeishuClient("cli_fixture", "FAKE_SECRET").bot_in_group("oc_fixture/../../users")
