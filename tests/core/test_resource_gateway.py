"""The independent datasource reader cannot broaden Prometheus scope."""

import hashlib
import json
from io import BytesIO
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from lockbot.metrics.gateway import Gateway, Rejected, validate_query

KEY = "test-readonly-credential-not-a-real-secret-1234"
AUTH = "Bearer " + KEY


def config():
    return {"readers": [{"sha256": hashlib.sha256(KEY.encode()).hexdigest(), "nodes": ["node-a", "node.b"]}]}


def body(query, **extra):
    return urlencode({"query": query, **extra}).encode()


GOOD = 'lockbot_host_cpu_utilization_percent{node=~"^(?:node-a|node\\\\.b)$"}'


def test_precise_subset_and_batch_queries_are_accepted():
    assert validate_query(GOOD, {"node-a", "node.b"})[1] == {"node-a", "node.b"}
    query = (
        '{__name__=~"^(?:lockbot_host_cpu_utilization_percent|lockbot_accelerator_process_info)$",node=~"^(?:node-a)$"}'
    )
    assert validate_query(query, {"node-a"})[0] == {
        "lockbot_host_cpu_utilization_percent",
        "lockbot_accelerator_process_info",
    }


@pytest.mark.parametrize(
    "query",
    [
        "up",
        "sum(lockbot_host_cpu_utilization_percent)",
        'lockbot_host_cpu_utilization_percent{node=~".*"}',
        'lockbot_host_cpu_utilization_percent{node=~"^(?:node-c)$"}',
        'lockbot_host_cpu_utilization_percent{node=~"^(?:node-a)$"} or up',
        '{__name__=~"^(?:lockbot_host_cpu_utilization_percent|up)$",node=~"^(?:node-a)$"}',
        'lockbot_host_cpu_utilization_percent{node=~"^(?:node-a)$",secret="x"}',
    ],
)
def test_arbitrary_promql_or_scope_expansion_is_rejected(query):
    with pytest.raises(Rejected):
        validate_query(query, {"node-a"})


@pytest.mark.parametrize("auth", ["", "Bearer invalid", "Bearer " + "x" * 40, "Bearer lbr_" + "x" * 40])
def test_missing_invalid_and_lockbot_tokens_are_rejected(auth):
    with pytest.raises(Rejected) as error:
        Gateway(config()).query(auth, body(GOOD))
    assert error.value.status == 401


@pytest.mark.parametrize(
    "payload",
    [
        b"query=x&query=y",
        b"query=x&time=1&time=2",
        b"query=x&unexpected=y",
        body(GOOD, timeout="60s"),
        body(GOOD, time="NaN"),
        body(GOOD, time="1"),
        b"x" * 32769,
    ],
)
def test_repeated_parameters_oversized_bodies_and_unbounded_times_are_rejected(payload):
    with pytest.raises(Rejected):
        Gateway(config(), clock=lambda: 10000).query(AUTH, payload)


def test_cache_is_behind_current_reader_authorization_and_preserves_source_data():
    gateway = Gateway(config(), clock=lambda: 10000)
    upstream = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {
                        "__name__": "lockbot_host_cpu_utilization_percent",
                        "node": "node-a",
                        "secret_label": "hidden",
                    },
                    "value": [10000, "12"],
                }
            ],
        },
    }
    gateway.opener.open = Mock(side_effect=lambda *_a, **_k: BytesIO(json.dumps(upstream).encode()))
    first = gateway.query(AUTH, body(GOOD))
    assert first == gateway.query(AUTH, body(GOOD))
    assert gateway.opener.open.call_count == 1
    assert "hidden" not in first.decode()
    gateway.config["readers"] = []
    with pytest.raises(Rejected) as error:
        gateway.query(AUTH, body(GOOD))
    assert error.value.status == 401


def test_prometheus_cannot_return_a_different_node_or_metric():
    response = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"node": "other", "__name__": "up"}, "value": [1, "1"]},
            ],
        },
    }
    with pytest.raises(Rejected) as error:
        Gateway.filter_response(response, {"lockbot_host_cpu_utilization_percent"}, {"node-a"})
    assert error.value.status == 502


def test_reader_budget_applies_to_cached_queries():
    gateway = Gateway(config(), clock=lambda: 10000)
    for _ in range(30):
        gateway.budget("reader")
    with pytest.raises(Rejected) as error:
        gateway.budget("reader")
    assert error.value.status == 429


def test_upstream_must_be_loopback_and_cannot_contain_credentials_or_paths():
    for url in ["https://public.example.invalid", "http://127.0.0.1:19090/admin", "http://user:pass@127.0.0.1"]:
        with pytest.raises(ValueError):
            Gateway({"upstream": url})


def test_http_cors_scope_auth_and_duplicate_body_parameters(tmp_path, monkeypatch):
    import http.client
    import threading
    import urllib.request

    from lockbot.metrics.gateway import make_server

    policy = config()
    policy["origins"] = ["https://lockbot.example.invalid"]
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(policy))
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda *_a, **_kw: BytesIO(b'{"status":"success","data":{"resultType":"vector","result":[]}}'),
    )
    server = make_server(str(path), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, payload=None, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(method, "/api/v1/query", body=payload, headers=headers)
        response = connection.getresponse()
        status, output_headers = response.status, dict(response.getheaders())
        response.read()
        connection.close()
        return status, output_headers

    try:
        assert request("OPTIONS", Origin="https://unknown.example.invalid")[0] == 403
        status, headers = request("OPTIONS", Origin="https://lockbot.example.invalid")
        assert status == 204 and headers["Access-Control-Allow-Origin"] == "https://lockbot.example.invalid"
        assert request("GET")[0] == 404
        content_type = {"Content-Type": "application/x-www-form-urlencoded"}
        assert request("POST", body(GOOD), **content_type)[0] == 401
        assert request("POST", b"query=x&query=y", Authorization=AUTH, **content_type)[0] == 400
        assert (
            request("POST", body(GOOD), Authorization=AUTH, Origin="https://unknown.example.invalid", **content_type)[0]
            == 403
        )
        assert request("POST", body(GOOD), Authorization=AUTH, **content_type)[0] == 200
        assert request("POST", b"x" * 32769, Authorization=AUTH, **content_type)[0] == 413
        # A cached query cannot survive revocation in the file.
        path.write_text(json.dumps({**policy, "readers": []}))
        assert request("POST", body(GOOD), Authorization=AUTH, **content_type)[0] == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("value", [None, [], {"data": []}, {"data": {"result": [None]}, "status": "success"}])
def test_malformed_upstream_shapes_fail_with_a_controlled_response(value):
    with pytest.raises(Rejected) as error:
        Gateway.filter_response(value, {"lockbot_host_cpu_utilization_percent"}, {"node-a"})
    assert error.value.status == 502


def grant_fixture(tmp_path):
    import secrets
    import time

    import jwt

    key = secrets.token_bytes(32)
    key_file = tmp_path / "grant.key"
    key_file.write_bytes(key)
    key_file.chmod(0o600)
    now = int(time.time() // 30) * 30
    audience = "https://metrics.example.invalid"
    claims = {"iss": "service-a", "sub": "bot-a", "aud": audience, "iat": now, "exp": now + 300, "nodes": ["node-a"]}
    policy = {
        "audience": audience,
        "issuers": {"service-a": {"key_file": str(key_file), "bots": {"bot-a": ["node-a"]}}},
    }
    return Gateway(policy), key, claims, jwt


def test_short_grants_share_bot_budget_and_reload_key_before_cache(tmp_path):
    gateway, key, claims, jwt = grant_fixture(tmp_path)
    one = "Bearer " + jwt.encode(claims, key, algorithm="HS256")
    two = "Bearer " + jwt.encode({**claims, "iat": claims["iat"] + 1, "exp": claims["exp"] + 1}, key, algorithm="HS256")
    assert gateway.reader(one) == gateway.reader(two) == ("grant:service-a:bot-a", frozenset({"node-a"}))
    for _ in range(30):
        gateway.budget(gateway.reader(one)[0])
    with pytest.raises(Rejected) as error:
        gateway.budget(gateway.reader(two)[0])
    assert error.value.status == 429
    from pathlib import Path

    Path(gateway.config["issuers"]["service-a"]["key_file"]).write_bytes(b"different-key-for-rotation-test-123456")
    with pytest.raises(Rejected) as error:
        gateway.reader(one)
    assert error.value.status == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "other-service"},
        {"sub": "other-bot"},
        {"aud": "https://management.example.invalid"},
        {"nodes": ["other-node"]},
        {"nodes": ["node-a", "node-a"]},
        {"nodes": "node-a"},
        {"iat": True},
        {"exp": True},
        {"exp": float("inf")},
        {"iat": float("inf")},
        {"nodes": []},
        {"nodes": ["node-a"] * 513},
    ],
)
def test_invalid_signature_scope_or_claim_types_are_rejected(tmp_path, changes):
    gateway, key, claims, jwt = grant_fixture(tmp_path)
    token = jwt.encode({**claims, **changes}, key, algorithm="HS256")
    with pytest.raises(Rejected) as error:
        gateway.reader("Bearer " + token)
    assert error.value.status in {401, 403}


def test_grants_cannot_outlive_five_minutes_or_use_future_clocks(tmp_path):
    import time

    gateway, key, claims, jwt = grant_fixture(tmp_path)
    now = int(time.time())
    for changes in (
        {"exp": claims["iat"] + 301},
        {"iat": now + 40, "exp": now + 100},
        {"nbf": now + 40},
        {"iat": now - 400, "exp": now - 100},
        {"nbf": True},
    ):
        with pytest.raises(Rejected):
            gateway.reader("Bearer " + jwt.encode({**claims, **changes}, key, algorithm="HS256"))
    with pytest.raises(Rejected):
        gateway.reader("Bearer " + jwt.encode(claims, b"different-signing-key-12345678901234", algorithm="HS256"))
    with pytest.raises(Rejected):
        gateway.reader("Bearer " + jwt.encode(claims, key, algorithm="HS384"))


def test_grant_key_file_requires_private_permissions(tmp_path):
    import pathlib

    gateway, key, claims, jwt = grant_fixture(tmp_path)
    pathlib.Path(gateway.config["issuers"]["service-a"]["key_file"]).chmod(0o644)
    with pytest.raises(Rejected):
        gateway.reader("Bearer " + jwt.encode(claims, key, algorithm="HS256"))
