"""Notifications use a single network attempt, unlike legacy reply retries."""

from types import SimpleNamespace

import pytest
import requests
from lockbot.core.config import Config
from lockbot.core.platforms.infoflow import InfoflowAdapter


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"errcode": 0}, "accepted"),
        ({"errcode": 1}, "failed"),
        ({}, "unknown"),
        ({"errcode": 0, "data": {"fail": ["group"]}}, "failed"),
    ],
)
def test_single_notification_parses_platform_acceptance(monkeypatch, result, expected):
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: result)

    monkeypatch.setattr("lockbot.core.platforms.infoflow.requests.post", post)
    adapter = InfoflowAdapter(Config({"WEBHOOK_URL": "https://example.invalid/notification"}))
    outcome, _ = adapter.send_notification(adapter.build_reply("operation", []))
    assert outcome == expected and len(calls) == 1
    assert calls[0]["allow_redirects"] is False and calls[0]["timeout"] == (3, 5)


def test_uncertain_timeout_has_no_automatic_retry(monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout("private-response-must-not-be-logged")

    monkeypatch.setattr("lockbot.core.platforms.infoflow.requests.post", post)
    adapter = InfoflowAdapter(Config({"WEBHOOK_URL": "https://example.invalid/notification"}))
    assert adapter.send_notification(adapter.build_reply("operation", [])) == ("unknown", "delivery_uncertain")
    assert len(calls) == 1
