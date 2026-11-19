"""Explicit providers use bounded stdio without consulting native credentials."""

import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_api import skill

from backend.test_resource_agent_helper import helper

PROVIDER = r"""
import json
import pathlib
import sys
import time

mode = sys.argv[1]
request = json.load(sys.stdin)
with pathlib.Path(__file__).with_name("requests.jsonl").open("a") as log:
    log.write(json.dumps({"request": request, "argv": sys.argv[2:]}) + "\n")
if mode == "timeout":
    time.sleep(30)
if mode == "oversize":
    print(" " * 9000)
    raise SystemExit(0)
if mode in ("error", "unsafe_error", "huge_retry", "crash"):
    print("private diagnostic /private/config lbr_" + "hidden" * 8, file=sys.stderr)
    value = {"error": "upstream_unavailable", "retry_mutation": False, "retry_after_seconds": 9}
    if mode == "unsafe_error":
        value["error"] = "private detail /private/config"
    if mode == "huge_retry":
        value["retry_after_seconds"] = 10 ** 1000
    if mode == "crash":
        raise SystemExit(7)
    print(json.dumps(value))
    raise SystemExit(2)
if request["action"] == "fields":
    print(json.dumps({"json_fields": ["ref", "node", "unit", "provider_extra"], "required_row_fields": ["ref"]}))
    raise SystemExit(0)
query = request["query"]
start = 0 if request["cursor"] is None else 2
rows = [{"ref": "x-demo/node-" + str(i), "node": "node-" + str(i), "unit": "whole_node", "provider_extra": i}
        for i in range(start, min(start + query["limit"], 3))]
if request["action"] == "view":
    rows = [{"ref": request["ref"], "node": "selected", "unit": "whole_node"}]
elif query["nodes"]:
    rows = [row for row in rows if row["node"].casefold() in query["nodes"]]
more = request["action"] == "list" and request["cursor"] is None and query["limit"] == 2
value = {"result": {"rows": rows, "has_more": more, "next_cursor": "opaque cursor: α/+" if more else None,
                    "scope_complete": not more, "http_requests": 2, "capabilities": ["read_only"],
                    "observed_only": True}}
result = value["result"]
if mode == "write_capability":
    result["capabilities"].append("write")
elif mode == "holding_only":
    result["rows"] = [{"ref": "x-demo/node", "holding_units": 1}]
elif mode == "booking_only":
    result["rows"] = [{"ref": "x-demo/node", "booking_units": 0}]
elif mode == "native_ref":
    rows[0]["ref"] = "s-" + "a" * 64 + "/node"
elif mode == "duplicate":
    result["rows"] = [rows[0], rows[0]]
elif mode == "row_limit":
    result["rows"] = [{"ref": "x-over/" + str(i)} for i in range(query["limit"] + 1)]
elif mode == "budget":
    result["http_requests"] = 13
elif mode == "nan":
    result["age_seconds"] = float("nan")
elif mode == "secret":
    rows[0]["model"] = "lbp_" + "hidden" * 8
elif mode == "owner":
    rows[0]["external_user_id"] = "private-person"
elif mode == "config_path":
    rows[0]["debug"] = sys.argv[-2]
elif mode == "wrong_view":
    rows[0]["ref"] = "x-other/node"
if query["fields"] is not None:
    result["rows"] = [{key: item[key] for key in ("ref", *query["fields"]) if key in item} for item in result["rows"]]
print(json.dumps(value, ensure_ascii=False))
"""


@pytest.fixture
def provider(tmp_path):
    with ZipFile(io.BytesIO(skill.installation_bundle())) as bundle:
        bundle.extractall(tmp_path)
    executable = tmp_path / "fixture_provider.py"
    executable.write_text(PROVIDER)
    registration = tmp_path / "provider.json"
    config = tmp_path / "private-provider-config.json"
    script = tmp_path / "lockbot-agent/scripts/lockbot_cli.py"

    def register(mode="success", **changes):
        value = {
            "schema_version": 1,
            "command": [sys.executable, "-I", "-S", str(executable), mode],
            "config_file": str(config),
        }
        value.update(changes)
        registration.write_text(json.dumps(value))
        registration.chmod(0o600)
        return value

    def invoke(*args, code=0, text=False):
        process = subprocess.run(
            [sys.executable, "-I", "-S", str(script), "--resource-provider", str(registration), *map(str, args)],
            env={**os.environ, "LOCKBOT_HOME": str(tmp_path / "missing-native-home")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert process.returncode == code, process.stderr
        assert len(process.stdout.encode()) <= helper.SUMMARY_BYTE_LIMIT
        assert "private diagnostic" not in process.stdout + process.stderr
        return process.stdout if text else json.loads(process.stdout if code == 0 else process.stderr)

    def requests():
        path = executable.with_name("requests.jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    register()
    return SimpleNamespace(
        register=register, invoke=invoke, requests=requests, path=registration, config=config, script=script
    )


def test_packaged_list_view_fields_and_opaque_cursor_preserve_provider_contract(provider):
    first = provider.invoke("resource", "list", "--json", "node,provider_extra", "--limit", 2)["result"]
    assert first["next_cursor"] == "opaque cursor: α/+"
    assert [row["provider_extra"] for row in first["rows"]] == [0, 1]
    tail = provider.invoke(
        "resource", "list", "--json", "node,provider_extra", "--limit", 2, "--cursor", first["next_cursor"]
    )["result"]
    assert [row["ref"] for row in first["rows"] + tail["rows"]] == [f"x-demo/node-{i}" for i in range(3)]
    assert not tail["has_more"] and tail["next_cursor"] is None
    view = provider.invoke("resource", "view", "x-demo/node-2", "--json", "node")["result"]
    assert view["rows"][0]["ref"] == "x-demo/node-2"
    fields = provider.invoke("resource", "list", "--json")
    assert "provider_extra" in fields["json_fields"] and fields["required_row_fields"] == ["ref"]
    assert [entry["request"]["action"] for entry in provider.requests()] == ["list", "list", "view", "fields"]
    for entry in provider.requests():
        assert entry["argv"] == ["--config", str(provider.config), "--provider-stdio"]
        assert entry["request"]["schema_version"] == 1
        assert entry["request"]["budget"] == {"max_http_requests": 12, "timeout_seconds": 60}
    assert not provider.config.exists()  # Neither parent nor field discovery needs the provider config.


def test_provider_path_never_constructs_a_native_client_or_reads_native_catalog(provider, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("native credential/catalog path must not run")

    original = helper.private_json

    def registration_only(path):
        assert str(path) == str(provider.path)
        return original(path)

    monkeypatch.setattr(helper, "Client", forbidden)
    monkeypatch.setattr(helper, "listed_connections", forbidden)
    monkeypatch.setattr(helper, "private_json", registration_only)
    assert (
        helper.main(
            [
                "--resource-provider",
                str(provider.path),
                "resource",
                "list",
                "--node",
                "NODE-1",
                "--mine",
                "--json",
                "provider_extra",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["rows"] == [{"ref": "x-demo/node-1", "provider_extra": 1}]
    assert provider.requests()[0]["request"]["query"]["nodes"] == ["node-1"]


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"command": []},
        {"command": "shell code"},
        {"command": ["python3"]},
        {"command": ["/missing/executable"]},
        {"config_file": "relative-config"},
        {"extra": "unsupported"},
    ],
)
def test_invalid_registration_never_starts_a_process(provider, changes):
    provider.register(**changes)
    value = provider.invoke("resource", "list", code=2)
    assert value["error"] == "resource_provider_registration_invalid" and provider.requests() == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits; Windows uses private ACL validation")
def test_public_registration_permissions_do_not_launch_a_provider(provider):
    provider.path.chmod(0o644)
    value = provider.invoke("resource", "list", code=2)
    assert value["error"] == "resource_provider_registration_invalid" and provider.requests() == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["resources-all"],
        ["resource", "devices", "x-demo/node"],
        ["command", "--operation", "lock", "--node", "node", "--whole-node", "--duration", "1h"],
        ["--connection", "/missing/native.json", "resource", "list"],
        ["--connection", "", "resource", "list"],
        ["--legacy-read", "resource", "list"],
        ["resource", "list", "--directory", "/missing/catalog"],
        ["resource", "view", "x-demo/node", "--exact-lease-times"],
    ],
)
def test_writes_and_native_mixed_flags_are_rejected_before_registration(provider, arguments):
    provider.path.unlink()
    value = provider.invoke(*arguments, code=2)
    assert value["error"] == "resource_provider_combination_not_supported" and provider.requests() == []


def test_external_view_rejects_native_references_without_a_process(provider):
    result = provider.invoke("resource", "view", "s-" + "a" * 64 + "/node", code=2)
    assert result["error"] == "resource_provider_reference_invalid" and provider.requests() == []


def test_empty_explicit_registration_does_not_fall_back_to_native_reads(provider, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("native reader must not run")

    monkeypatch.setattr(helper, "resource_list", forbidden)
    assert helper.main(["--resource-provider", "", "resource", "list"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "resource_provider_registration_invalid"
    assert provider.requests() == []


def test_external_references_never_select_a_provider_implicitly(provider):
    process = subprocess.run(
        [sys.executable, "-I", "-S", str(provider.script), "resource", "view", "x-demo/node"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 2
    assert json.loads(process.stderr)["error"] == "invalid_resource_reference"
    assert provider.requests() == []


def test_default_text_handles_opaque_refs_with_and_without_node_fields(provider):
    text = provider.invoke("resource", "view", "x-demo/node", text=True)
    assert "selected" in text and "x-demo/node" in text
    encoded = json.dumps({"result": {"rows": [{"ref": "x-demo/node"}], "scope_complete": True}})
    assert "x-demo/node" in helper.resource_list_text(encoded)


@pytest.mark.parametrize(
    "mode",
    ["write_capability", "native_ref", "duplicate", "row_limit", "budget", "nan", "secret", "owner", "config_path"],
)
def test_untrusted_success_is_rejected_without_relaying_private_output(provider, mode):
    provider.register(mode)
    value = provider.invoke("resource", "list", code=2)
    assert value["error"] == "resource_provider_response_invalid"


def test_view_cannot_return_another_resource(provider):
    provider.register("wrong_view")
    assert (
        provider.invoke("resource", "view", "x-demo/requested", code=2)["error"] == "resource_provider_response_invalid"
    )


def test_controlled_error_is_sanitized_and_stderr_is_discarded(provider):
    provider.register("error")
    result = provider.invoke("resource", "list", code=2)
    assert result == {"error": "upstream_unavailable", "retry_mutation": False, "retry_after_seconds": 9}
    provider.register("unsafe_error")
    assert provider.invoke("resource", "list", code=2)["error"] == "resource_provider_failed"
    provider.register("huge_retry")
    assert provider.invoke("resource", "list", code=2)["error"] == "resource_provider_failed"
    provider.register("crash")
    assert provider.invoke("resource", "list", code=2)["error"] == "resource_provider_failed"


def test_oversized_stdout_is_rejected_instead_of_trimming_a_page(provider):
    provider.register("oversize")
    assert provider.invoke("resource", "list", code=2)["error"] == "resource_provider_output_too_large"


def test_parent_timeout_terminates_a_real_provider_process(provider, monkeypatch, capsys):
    provider.register("timeout")
    monkeypatch.setattr(helper, "RESOURCE_PROVIDER_TIMEOUT", 0.05)
    assert helper.main(["--resource-provider", str(provider.path), "resource", "list"]) == 2
    value = capsys.readouterr()
    assert value.out == "" and json.loads(value.err)["error"] == "resource_provider_timeout"


@pytest.mark.parametrize("mode,expected", [("holding_only", "held:1 booked:?"), ("booking_only", "held:? booked:0")])
def test_generic_terminal_rows_tolerate_optional_personal_counts(provider, mode, expected):
    provider.register(mode)
    text = provider.invoke("resource", "list", text=True)
    assert expected in text and "x-demo/node" in text


def test_inventory_text_does_not_confuse_zero_personal_holds_with_vacancy():
    encoded = json.dumps(
        {
            "result": {
                "rows": [
                    {
                        "ref": "x-demo/node",
                        "node": "shared-node",
                        "states": {"busy": 1},
                        "holding_units": 0,
                        "booking_units": 0,
                    }
                ],
                "scope_complete": True,
            }
        }
    )
    inventory = helper.resource_list_text(encoded, mine=False)
    personal = helper.resource_list_text(encoded, mine=True)
    assert "busy:1" in inventory and "held:0" not in inventory
    assert "held:0 booked:0" in personal
