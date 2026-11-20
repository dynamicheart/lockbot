"""Typed card selection narrows the existing gateway authority and parser."""

import json
from io import BytesIO
from urllib.parse import parse_qs, urlencode

import pytest
from core.test_resource_gateway import AUTH, config, grant_fixture
from lockbot.backend.app.bots.datasource import UtilizationConfig
from lockbot.metrics.gateway import Gateway, Rejected, device_selection, device_selection_query, validate_query


def selection(devices=("GPU-a",), **changes):
    return {"version": 1, "node": "node-a", "devices": list(devices), **changes}


def payload(value=None, **fields):
    return urlencode({"device_selection": json.dumps(selection() if value is None else value), **fields}).encode()


def vector(device="GPU-a"):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {
                        "__name__": "lockbot_accelerator_utilization_percent",
                        "node": "node-a",
                        "device": device,
                    },
                    "value": [10000, "0"],
                },
                {
                    "metric": {
                        "__name__": "lockbot_sample_timestamp_seconds",
                        "node": "node-a",
                        "collector": "accelerator",
                    },
                    "value": [10000, "10000"],
                },
            ],
        },
    }


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        None,
        selection(version=True),
        selection(version=2),
        selection(node=""),
        selection(node="a" * 257),
        selection(node="a\n"),
        selection(devices=[]),
        selection(devices=["a"] * 2),
        selection(devices=[False]),
        selection(devices=["a" * 257]),
        selection(devices=[str(i) for i in range(65)]),
        selection(extra=True),
    ],
)
def test_selection_rejects_unknown_types_and_bounds(value):
    with pytest.raises(Rejected) as error:
        device_selection(json.dumps(value), {"node-a"})
    assert error.value.status == 400


@pytest.mark.parametrize("raw", ['{"version":1,"version":1,"node":"node-a","devices":["x"]}', "x" * 16385])
def test_selection_rejects_duplicate_keys_and_large_json(raw):
    with pytest.raises(Rejected):
        device_selection(raw, {"node-a"})


def test_gateway_selection_cache_and_upstream_are_exact():
    gateway = Gateway(config(), clock=lambda: 10000)
    requests = []

    def upstream(request, **_):
        query = parse_qs(request.data.decode())["query"][0]
        requests.append(query)
        return BytesIO(json.dumps(vector("GPU-b" if "GPU-b" in query else "GPU-a")).encode())

    gateway.opener.open = upstream
    first = json.loads(gateway.query(AUTH, payload()))
    assert first["lockbot_device_selection"] == selection()
    assert len(first["data"]["result"]) == 2
    gateway.query(AUTH, payload())
    gateway.query(AUTH, payload(selection(["GPU-b"])))
    assert len(requests) == 2 and requests[0] == device_selection_query(selection())
    assert 'collector="accelerator"' in requests[0] and " or " in requests[0]
    with pytest.raises(Rejected):
        validate_query(requests[0], {"node-a"})
    gateway.config["readers"] = []
    with pytest.raises(Rejected) as error:
        gateway.query(AUTH, payload())
    assert error.value.status == 401


@pytest.mark.parametrize("bad", [payload(query="up"), payload(unexpected="x"), payload() + b"&device_selection=x"])
def test_typed_and_legacy_forms_are_mutually_exclusive(bad):
    with pytest.raises(Rejected):
        Gateway(config()).query(AUTH, bad)


def test_card_scope_never_expands_and_cache_checks_expiry(tmp_path):
    gateway, key, legacy, jwt = grant_fixture(tmp_path)
    claims = {k: v for k, v in legacy.items() if k != "nodes"}
    claims.update(scope_version=2, resources={"node-a": ["GPU-a"]})
    auth = "Bearer " + jwt.encode(claims, key, algorithm="HS256")
    gateway.opener.open = lambda *_a, **_k: BytesIO(json.dumps(vector()).encode())
    gateway.query(auth, payload())
    with pytest.raises(Rejected) as error:
        gateway.query(auth, payload(selection(["GPU-b"])))
    assert error.value.status == 403
    with pytest.raises(Rejected):
        gateway.query(auth, payload(selection(node="node.b")))
    gateway.clock = lambda: claims["exp"]
    with pytest.raises(Rejected) as error:
        gateway.query(auth, payload())
    assert error.value.status == 401


@pytest.mark.parametrize("change", ["wrong_device", "host", "metadata_device", "process"])
def test_selected_response_cannot_smuggle_other_series(change):
    gateway = Gateway(config())
    result = vector()
    metric = result["data"]["result"][0]["metric"]
    if change == "wrong_device":
        metric["device"] = "GPU-b"
    elif change == "host":
        metric["__name__"] = "lockbot_host_cpu_utilization_percent"
    elif change == "process":
        metric["__name__"] = "lockbot_accelerator_process_info"
    else:
        result["data"]["result"][1]["metric"]["device"] = "GPU-a"
    gateway.opener.open = lambda *_a, **_k: BytesIO(json.dumps(result).encode())
    with pytest.raises(Rejected) as error:
        gateway.query(AUTH, payload())
    assert error.value.status == 502


def test_selection_labels_are_literals_not_expressions():
    value = selection(["a|b", "a\\b", '"x"', "卡.*"])
    parsed = device_selection(json.dumps(value), {"node-a"})
    query = device_selection_query(parsed)
    assert json.dumps("^(?:" + "|".join(['"x"', "a\\\\b", "a\\|b", "卡\\.\\*"]) + ")$") in query


@pytest.mark.parametrize("value", [None, False, "unknown", 1])
def test_datasource_capability_requires_exact_declaration(value):
    with pytest.raises(ValueError):
        UtilizationConfig(
            endpoint="https://metrics.example.invalid", nodeMapping={"n": "n"}, profile="lockbot-v1", deviceQuery=value
        )


def test_datasource_old_roundtrip_and_new_profile_are_explicit():
    old = UtilizationConfig(endpoint="https://metrics.example.invalid", nodeMapping={"n": "n"})
    assert "deviceQuery" not in old.model_dump()
    with pytest.raises(ValueError):
        UtilizationConfig(**old.model_dump(), deviceQuery="exact-v1")
    current = UtilizationConfig(**{**old.model_dump(), "profile": "lockbot-v1", "deviceQuery": "exact-v1"})
    assert UtilizationConfig.model_validate_json(current.model_dump_json()).deviceQuery == "exact-v1"


def test_selection_strips_unrequested_process_labels():
    gateway = Gateway(config())
    result = vector()
    result["data"]["result"][0]["metric"].update(command="private task", user="private owner", container_name="private")
    gateway.opener.open = lambda *_a, **_k: BytesIO(json.dumps(result).encode())
    assert b"private" not in gateway.query(AUTH, payload())
