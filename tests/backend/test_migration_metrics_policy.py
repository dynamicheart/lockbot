"""Only deployment-reviewed expiration/clock bounds can authorize the grant drain."""

import json
import time
from types import SimpleNamespace

import pytest
from lockbot.backend.app.bot_migrations.coordinator import MigrationBlocked
from lockbot.backend.app.bot_migrations.metrics_policy import reviewed_metrics_policy
from lockbot.backend.app.settings.models import SiteSetting


@pytest.fixture
def policy(db_session, monkeypatch, tmp_path):
    from lockbot.backend.app.bot_migrations import metrics_policy

    endpoint = "https://metrics.example.test"
    monkeypatch.setattr(metrics_policy, "select_metrics_signer", lambda _: SimpleNamespace(audience=endpoint))
    db_session.add(SiteSetting(key="resource_api_instance_id", value="fixture-instance"))
    db_session.commit()
    path = tmp_path / "review.json"
    monkeypatch.setenv("LOCKBOT_MIGRATION_METRICS_POLICY_FILE", str(path))
    value = {
        "version": 1,
        "issuer": "fixture-instance",
        "audience": endpoint,
        "profile": "lockbot-gateway-v1",
        "maximum_token_ttl_seconds": 300,
        "expiry_leeway_seconds": 15,
        "verify_expiration": True,
        "review_reference": "fixture-reviewed-gateway-clock",
        "maximum_clock_skew_seconds": 2,
        "review_valid_until": time.time() + 3600,
    }
    source = {"endpoint": endpoint, "accessMode": "short_lived"}

    def write():
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    write()
    return SimpleNamespace(utilization_config=json.dumps(source)), value, write


def test_absent_source_does_not_require_clock_file(db_session):
    assert reviewed_metrics_policy(db_session, SimpleNamespace(utilization_config=None)) is None


def test_reviewed_expiry_profile_and_clock_bound(db_session, policy):
    bot, _, _ = policy
    assert reviewed_metrics_policy(db_session, bot)["maximum_clock_skew_seconds"] == 2


@pytest.mark.parametrize("change", ["ttl", "leeway", "expiry", "issuer", "skew", "verify"])
def test_unreviewed_or_weaker_verifier_policy_is_refused(db_session, policy, change):
    bot, value, write = policy
    if change == "ttl":
        value["maximum_token_ttl_seconds"] = 600
    elif change == "leeway":
        value["expiry_leeway_seconds"] = 30
    elif change == "expiry":
        value["review_valid_until"] = time.time() - 1
    elif change == "issuer":
        value["issuer"] = "other"
    elif change == "skew":
        value["maximum_clock_skew_seconds"] = 301
    else:
        value["verify_expiration"] = False
    write()
    with pytest.raises(MigrationBlocked, match="reviewed_metrics"):
        reviewed_metrics_policy(db_session, bot)
