"""Owners configure one non-secret source shared by authorized viewers, never a proxy."""

import json

import pytest
from lockbot.backend.app.bots.datasource import UtilizationConfig
from lockbot.backend.app.bots.models import Bot
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

from backend.test_bot_resource_view import api, headers, isolated_view_manager, link  # noqa: F401

SOURCE = {
    "endpoint": "https://metrics.example.test/prometheus",
    "nodeMapping": {"node1": "host1:9400"},
}


def test_owner_config_shared_only_after_view_authorization(client, api, db_session):  # noqa: F811
    path = f"/api/bots/{api['bot_id']}"
    context = link(client, api)
    view_path = f"/api/bot-views/{api['bot_id']}/state"
    assert client.get(view_path, headers=headers(context)).json()["utilization_config"] is None
    response = client.put(path, headers=api["auth_header"], json={"utilization_config": SOURCE})
    assert response.status_code == 200, response.text
    configured = json.loads(response.json()["utilization_config"])
    assert configured["endpoint"] == SOURCE["endpoint"]
    assert client.get(view_path, headers=headers(context)).json()["utilization_config"] == configured
    assert "metrics.example" not in client.get(view_path).text
    # Old clients omit this new optional field: unrelated edits must preserve it.
    assert client.put(path, headers=api["auth_header"], json={"name": "renamed"}).status_code == 200
    db_session.expire_all()
    assert json.loads(db_session.get(Bot, api["bot_id"]).utilization_config) == configured
    assert client.put(path, headers=api["auth_header"], json={"utilization_config": None}).status_code == 200
    assert client.get(view_path, headers=headers(context)).json()["utilization_config"] is None


def test_other_users_and_agent_keys_cannot_configure_source(client, api):  # noqa: F811
    from backend.test_bot_authz import _register_user

    outsider = _register_user(client, "sourceoutsider", "sourceoutsider@test.com", "pass123")
    path = f"/api/bots/{api['bot_id']}"
    for auth, expected in [(outsider, 404), (api["headers"], 401), ({}, 401)]:
        assert client.put(path, headers=auth, json={"utilization_config": SOURCE}).status_code == expected


@pytest.mark.parametrize(
    "patch",
    [
        {"endpoint": "https://user:password@metrics.test"},
        {"endpoint": "https://metrics.test?token=secret"},
        {"endpoint": "file:///etc/passwd"},
        {"endpoint": "https://metrics.test:70000"},
        {"endpoint": "https://metrics.test:invalid"},
        {"endpoint": "https://metrics.test:0"},
        {"bearer": "secret"},
        {"nodeMapping": {}},
        {"intervalSeconds": 1},
        {"deviceLabel": "instance"},
        {"utilizationMetric": "rate(metric[5m])"},
    ],
)
def test_reject_secrets_and_invalid_configuration(patch):
    with pytest.raises(ValidationError):
        UtilizationConfig.model_validate({**SOURCE, **patch})


def test_legacy_schema_migration_is_additive_and_repeatable(monkeypatch):
    from lockbot.backend.app import main

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE bots (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO bots VALUES (1, 'existing')"))
    monkeypatch.setattr(main, "engine", engine)
    main._migrate_bots_utilization_config()
    main._migrate_bots_utilization_config()
    assert "utilization_config" in {column["name"] for column in inspect(engine).get_columns("bots")}
    with engine.begin() as connection:
        assert connection.execute(text("SELECT name, utilization_config FROM bots")).one() == ("existing", None)
    engine.dispose()
