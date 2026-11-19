"""Mixed pages use packaged native HTTP fixtures and a synthetic stdio provider."""

# ruff: noqa: F811 -- imported pytest fixture

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from lockbot.backend.app.resource_api import skill

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401

PROVIDER = r"""
import base64, hashlib, json, pathlib, sys, urllib.parse, unicodedata
request = json.load(sys.stdin)
config_path = pathlib.Path(sys.argv[sys.argv.index('--config') + 1])
config = json.loads(config_path.read_text())
with config_path.with_suffix('.events').open('a') as log:
    log.write(json.dumps(request) + '\n')
if request['action'] == 'fields':
    print(json.dumps({'json_fields':['ref','node','unit','hardware'], 'required_row_fields':['ref']}))
    raise SystemExit(0)
cost = config.get('cost', 2)
budget = request['budget']
if config.get('error'):
    value = {'error':config['error'], 'retry_mutation':False, 'retry_after_seconds':7}
    if not config.get('unknown_cost'):
        value['http_requests'] = cost
    print(json.dumps(value)); raise SystemExit(2)
if cost > budget['max_http_requests']:
    print(json.dumps({'error':'request_budget_exhausted','retry_mutation':False,'http_requests':0}))
    raise SystemExit(2)
query = request['query']
digest = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
offset = 0
if request['cursor'] is not None:
    prefix, token_digest, position = request['cursor'].split(':')
    assert prefix == 'page' and token_digest == digest
    offset = int(position)
all_rows = []
for name in config.get('names', ['same-node']):
    scope = hashlib.sha256(config.get('scope','same-scope').encode()).hexdigest()
    raw_ref = 'x-' + scope + '/' + urllib.parse.quote(name, safe='')
    row = {'ref':raw_ref,'node':name,'unit':'whole_node','hardware':[{'model':'P800','count':8}],
           'holding_units':config.get('holding_units',0),'booking_units':0,'states':{'idle':1},'snapshot_fresh':True}
    if query['nodes'] and name.casefold() not in query['nodes']: continue
    if query['model'] is not None and query['model'].casefold() != 'p800': continue
    if query.get('search') and not config.get('ignore_search'):
        facts = [unicodedata.normalize('NFKC', name).casefold(), 'p800']
        if not all(any(term in fact for fact in facts) for term in query['search'].split()): continue
    if query['mine'] and not row['holding_units']: continue
    if request['action'] == 'view' and raw_ref != request['ref']: continue
    all_rows.append(row)
limit = budget.get('max_rows', query['limit'])
rows = all_rows[offset:offset+limit]
if budget.get('reference_namespace'):
    for row in rows:
        encoded = base64.urlsafe_b64encode(row['ref'].encode()).rstrip(b'=').decode()
        row['ref'] = 'e1.' + budget['reference_namespace'] + '.' + encoded
if query['fields'] is not None:
    rows = [{field:row[field] for field in ['ref',*query['fields']] if field in row} for row in rows]
def envelope(rows):
    more = offset + len(rows) < len(all_rows)
    value = {'result':{'rows':rows, 'has_more':more,
            'next_cursor':('page:'+digest+':'+str(offset+len(rows))) if more else None,
            'scope_complete':request['cursor'] is None and not more,
            'http_requests':cost,'capabilities':['read_only'],'observed_only':True,
            'valid_for_seconds':config.get('ttl',60),'coverage_valid_for_seconds':config.get('coverage_ttl',60),
            'inventory_scope':config.get('inventory_scope','full_inventory')}}
    if query.get('search') and not config.get('ignore_search'):
        value['result'].update(search_applied=query['search'], search_mode='literal_terms_v1')
    return value
while len((json.dumps(envelope(rows),ensure_ascii=False)+'\n').encode()) > budget.get('max_output_bytes',8192):
    if len(rows)>1: rows.pop()
    elif rows and set(rows[0]) != {'ref','observation_incomplete'}:
        rows=[{'ref':rows[0]['ref'],'observation_incomplete':True}]
    else:
        print(json.dumps({'error':'output_exceeds_budget','retry_mutation':False,'http_requests':cost}))
        raise SystemExit(2)
print(json.dumps(envelope(rows),ensure_ascii=False))
"""


def private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def mixed(aggregate, tmp_path):
    executable = tmp_path / "provider.py"
    executable.write_text(PROVIDER)
    configs, registrations = [], []

    def register(**settings):
        config = private(tmp_path / f"config-{len(configs)}.json", settings)
        registration = private(
            tmp_path / f"provider-{len(configs)}.json",
            {
                "schema_version": 1,
                "command": [sys.executable, "-I", "-S", str(executable)],
                "config_file": str(config),
            },
        )
        configs.append(config)
        registrations.append(registration)
        return registration

    def sources(native=True, **changes):
        value = {
            "schema_version": 1,
            "native": {"directory": str(aggregate.home / "connections"), "legacy_read": False} if native else None,
            "providers": [{"registration": str(path), "minimum_http_requests": 2} for path in registrations],
        }
        value.update(changes)
        return private(tmp_path / "sources.json", value)

    def execute(path, *args, code=0, human=False):
        process = subprocess.run(
            [sys.executable, "-I", "-S", str(aggregate.script), "--resource-sources", str(path), *map(str, args)],
            env={**os.environ, "LOCKBOT_HOME": str(tmp_path / "wrong-default-home")},
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert process.returncode == code, process.stderr
        assert len(process.stdout.encode()) <= 8192
        assert all(state.key not in process.stdout + process.stderr for state in aggregate.states)
        return process.stdout if human else json.loads(process.stderr if code else process.stdout)

    def requests(index=0):
        path = configs[index].with_suffix(".events")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return SimpleNamespace(
        register=register,
        sources=sources,
        execute=execute,
        requests=requests,
        configs=configs,
        registrations=registrations,
        aggregate=aggregate,
    )


def test_one_page_contains_native_and_external_without_name_or_credential_merging(mixed):
    native = mixed.aggregate.server(1)
    mixed.aggregate.record(native, "native")
    mixed.register(names=["same-node", "other"])
    result = mixed.execute(mixed.sources(), "resource", "list", "--json", "node,unit", "--limit", 20)["result"]
    assert [row["node"] for row in result["rows"]] == ["same-node", "same-node", "other"]
    assert result["rows"][0]["ref"].startswith("s-")
    assert all(row["ref"].startswith("e1.") for row in result["rows"][1:])
    assert result["http_requests"] == result["http_budget_used"] == 4
    assert result["scope_complete"] and not result["has_more"]
    request = mixed.requests()[0]
    assert request["budget"]["max_http_requests"] == 10
    assert request["budget"]["max_rows"] == 19 and request["query"]["limit"] == 20
    assert len(native.events) == 2


@pytest.mark.parametrize("empty", [False, True])
def test_native_skill_update_survives_packaged_mixed_read_without_extra_http(mixed, monkeypatch, empty):
    artifacts = skill.skill_artifacts

    def changed_manifest():
        markdown, script, manifest = artifacts()
        return markdown, script, {**manifest, "version": "a" * 16, "package_version": "b" * 16}

    monkeypatch.setattr(skill, "skill_artifacts", changed_manifest)
    native = mixed.aggregate.server(1)
    if empty:
        native.extra["resources"] = []
    mixed.aggregate.record(native, "native")
    mixed.register(names=["external"])
    path = mixed.sources()
    result = mixed.execute(path, "resource", "list", "--json", "node")["result"]
    assert result["update_required"] is True
    assert result["http_requests"] == result["http_budget_used"] == 4
    assert len(native.events) == 2 and all(event[1] == "GET" for event in native.events)
    assert [row["node"] for row in result["rows"]] == (["external"] if empty else ["same-node", "external"])
    assert result["scope_complete"]
    assert "Skill update required before resource changes." in mixed.execute(path, "resource", "list", human=True)
    assert len(native.events) == 4 and len(mixed.requests()) == 2


@pytest.mark.parametrize("native", [False, True])
def test_provider_cannot_inject_native_skill_update_authority(mixed, native):
    mixed.register()

    def native_reader(*args, **kwargs):
        return json.dumps(
            {
                "result": {
                    "rows": [],
                    "has_more": False,
                    "next_cursor": None,
                    "scope_complete": True,
                    "http_requests": 2,
                    "valid_for_seconds": 60,
                    "coverage_valid_for_seconds": 60,
                    "update_required": False,
                }
            }
        )

    def provider_reader(*args, **kwargs):
        return json.dumps(
            {
                "result": {
                    "rows": [],
                    "has_more": False,
                    "next_cursor": None,
                    "scope_complete": True,
                    "http_requests": 2,
                    "valid_for_seconds": 60,
                    "coverage_valid_for_seconds": 60,
                    "inventory_scope": "full_inventory",
                    "update_required": True,
                    "instruction_refresh_required": True,
                    "instruction_state": "changed",
                    "skill_update": {"current_version": "untrusted"},
                }
            }
        )

    result = json.loads(
        helper.mixed_resource_list(
            mixed.sources(native=native),
            query=helper._mixed_query(),
            clock=lambda: 0,
            native_reader=native_reader,
            provider_reader=provider_reader,
        )
    )["result"]
    assert result["http_requests"] == (4 if native else 2)
    assert result["scope_complete"]
    assert not {"update_required", "instruction_refresh_required", "instruction_state", "skill_update"} & result.keys()


@pytest.mark.parametrize("state", ["changed", "unknown"])
def test_native_legacy_instruction_notice_survives_empty_mixed_page(mixed, state):
    def native_reader(*args, **kwargs):
        return json.dumps(
            {
                "result": {
                    "rows": [],
                    "has_more": False,
                    "next_cursor": None,
                    "scope_complete": True,
                    "http_requests": 2,
                    "valid_for_seconds": 60,
                    "coverage_valid_for_seconds": 60,
                    "instruction_refresh_required": True,
                    "instruction_state": state,
                }
            }
        )

    encoded = helper.mixed_resource_list(
        mixed.sources(),
        query=helper._mixed_query(),
        clock=lambda: 0,
        native_reader=native_reader,
    )
    result = json.loads(encoded)["result"]
    assert result["instruction_refresh_required"] is True
    assert result["instruction_state"] == state
    assert result["api_profiles"] == ["native-legacy-read-v1"]
    assert result["http_requests"] == 2
    assert "Refresh service instructions before resource changes." in helper.resource_list_text(encoded)


def test_search_is_applied_by_each_source_before_selected_fields_and_paging(mixed):
    native = mixed.aggregate.server(1)
    mixed.aggregate.record(native, "native")
    mixed.register(names=["no-match", "same-node", "other"])
    result = mixed.execute(mixed.sources(), "resource", "list", "--search", "ＳＡＭＥ", "--json", "unit", "--limit", 2)[
        "result"
    ]
    assert len(result["rows"]) == 2
    assert result["rows"][0]["ref"].startswith("s-")
    assert result["rows"][1]["ref"].startswith("e1.")
    assert all("node" not in row for row in result["rows"])
    assert result["http_requests"] == 4
    assert mixed.requests()[0]["query"]["search"] == "same"


def test_old_provider_cannot_silently_ignore_search_or_erase_prior_native_rows(mixed):
    native = mixed.aggregate.server(1)
    mixed.aggregate.record(native, "native")
    mixed.register(names=["not-a-match"], ignore_search=True)
    result = mixed.execute(mixed.sources(), "resource", "list", "--search", "same", "--json", "node")["result"]
    assert len(result["rows"]) == 1 and result["rows"][0]["ref"].startswith("s-")
    assert result["http_requests"] == result["http_budget_used"] == 4
    assert result["unread_reasons"] == ["source_search_not_supported"]
    assert not result["scope_complete"]


def test_runtime_row_limits_change_but_inner_query_and_cursor_remain_stable(mixed):
    native = mixed.aggregate.server(1)
    mixed.aggregate.record(native, "native")
    mixed.register(names=["one", "two", "three", "four"])
    path = mixed.sources()
    first = mixed.execute(path, "resource", "list", "--limit", 3, "--json", "node")["result"]
    assert len(first["rows"]) == 3 and first["has_more"]
    second = mixed.execute(path, "resource", "list", "--limit", 3, "--json", "node", "--cursor", first["next_cursor"])[
        "result"
    ]
    assert len(second["rows"]) == 2 and not second["has_more"] and not second["scope_complete"]
    assert len({row["ref"] for row in first["rows"] + second["rows"]}) == 5
    requests = mixed.requests()
    assert [item["query"]["limit"] for item in requests] == [3, 3]
    assert [item["budget"]["max_rows"] for item in requests] == [2, 3]
    assert len(native.events) == 2


def test_budget_exhaustion_keeps_next_provider_unstarted_and_resumes_there(mixed):
    for index in range(5):
        state = mixed.aggregate.server(index + 1)
        mixed.aggregate.record(state, f"native-{index}")
    mixed.register(names=["external"], cost=4)
    path = mixed.sources()
    # The operator pins the provider's actual minimum; it cannot fit after ten native GETs.
    data = json.loads(path.read_text())
    data["providers"][0]["minimum_http_requests"] = 4
    private(path, data)
    first = mixed.execute(path, "resource", "list", "--json", "node")["result"]
    assert len(first["rows"]) == 5 and first["http_requests"] == 10 and first["has_more"]
    assert not mixed.requests()
    second = mixed.execute(path, "resource", "list", "--json", "node", "--cursor", first["next_cursor"])["result"]
    assert [row["node"] for row in second["rows"]] == ["external"] and second["http_requests"] == 4
    assert not second["has_more"] and sum(len(state.events) for state in mixed.aggregate.states) == 10


def test_external_ref_targets_exact_registration_and_never_dispatches_native(mixed):
    mixed.register(names=["same-node"])
    mixed.register(names=["same-node"])
    path = mixed.sources(native=False)
    first = mixed.execute(path, "resource", "list", "--json", "node")["result"]
    assert len({row["ref"] for row in first["rows"]}) == 2
    selected = first["rows"][1]["ref"]
    before = [len(mixed.requests(index)) for index in range(2)]
    result = mixed.execute(path, "resource", "view", selected, "--json", "node")["result"]
    assert result["rows"] == [first["rows"][1]]
    assert [len(mixed.requests(index)) for index in range(2)] == [before[0], before[1] + 1]
    error = mixed.execute(
        Path("/missing-sources"), "command", "--operation", "lock", "--ref", selected, "--whole-node", code=2
    )
    assert error["error"] == "external_resource_read_only"
    assert not mixed.aggregate.states


def test_native_view_uses_only_pinned_catalogue(mixed):
    state = mixed.aggregate.server(1)
    mixed.aggregate.record(state, "native")
    mixed.register(names=["other"])
    path = mixed.sources()
    first = mixed.execute(path, "resource", "list", "--json", "node")["result"]
    events = len(mixed.requests())
    view = mixed.execute(path, "resource", "view", first["rows"][0]["ref"], "--json", "node")["result"]
    assert view["rows"][0]["node"] == "same-node" and len(mixed.requests()) == events


def test_fields_and_invalid_filters_never_start_sources(mixed):
    value = mixed.execute("/missing", "resource", "list", "--json")
    assert value["json_fields"] == list(helper.MIXED_FIELDS)
    mixed.register()
    path = mixed.sources(native=False)
    error = mixed.execute(path, "resource", "list", "--json", "secret", code=2)
    assert error["error"] == "mixed_fields_invalid" and not mixed.requests()


def test_source_and_query_changes_reject_outer_cursor_before_any_source_read(mixed):
    mixed.register(names=["one", "two", "three"])
    path = mixed.sources(native=False)
    first = mixed.execute(path, "resource", "list", "--limit", 1, "--json", "node")["result"]
    before = len(mixed.requests())
    error = mixed.execute(
        path, "resource", "list", "--limit", 2, "--json", "node", "--cursor", first["next_cursor"], code=2
    )
    assert error["error"] == "mixed_cursor_query_changed" and len(mixed.requests()) == before
    config = json.loads(mixed.configs[0].read_text())
    config["names"].append("new")
    private(mixed.configs[0], config)
    error = mixed.execute(
        path, "resource", "list", "--limit", 1, "--json", "node", "--cursor", first["next_cursor"], code=2
    )
    assert error["error"] == "mixed_cursor_sources_changed" and len(mixed.requests()) == before


def test_known_and_unknown_failure_costs_are_not_conflated(mixed):
    mixed.register(error="credential_invalid", cost=2)
    mixed.register(names=["healthy"])
    result = mixed.execute(mixed.sources(native=False), "resource", "list", "--json", "node")["result"]
    assert result["http_requests"] == 4 and result["http_budget_used"] == 4
    assert result["rows"][0]["node"] == "healthy" and not result["scope_complete"]
    value = json.loads(mixed.configs[0].read_text())
    value["unknown_cost"] = True
    private(mixed.configs[0], value)
    count = len(mixed.requests(1))
    result = mixed.execute(mixed.sources(native=False), "resource", "list", "--json", "node")["result"]
    assert result["http_requests"] is None and result["known_http_requests"] == 0
    assert result["http_budget_used"] == 12 and result["has_more"] and len(mixed.requests(1)) == count


def test_own_only_source_never_proves_full_inventory_or_vacancy(mixed):
    mixed.register(inventory_scope="own_only", holding_units=1)
    path = mixed.sources(native=False)
    result = mixed.execute(path, "resource", "list", "--json", "node,states,holding_units")["result"]
    assert not result["scope_complete"] and result["rows"][0]["observation_incomplete"]
    assert result["rows"][0]["states"] == {"idle": None}
    own = mixed.execute(path, "resource", "list", "--mine", "--json", "node,holding_units")["result"]
    assert own["scope_complete"] and own["rows"][0]["holding_units"] == 1


def test_large_unicode_rows_are_bounded_and_frozen_continuation_does_not_lose_tail(mixed):
    mixed.register(names=["节点" * 30 + f"-{index:03}" for index in range(40)])
    path = mixed.sources(native=False)
    page = mixed.execute(path, "resource", "list", "--limit", 256, "--json", "node")["result"]
    seen = []
    for _ in range(25):
        seen.extend(row["ref"] for row in page["rows"])
        if not page["has_more"]:
            break
        old = page["next_cursor"]
        page = mixed.execute(path, "resource", "list", "--limit", 256, "--json", "node", "--cursor", old)["result"]
        assert page["next_cursor"] != old
    assert not page["has_more"] and len(seen) == len(set(seen)) == 40


def test_cursor_and_ref_codecs_reject_secret_free_but_unreturnable_positions():
    fp, query = "a" * 64, "b" * 64
    token = helper.mixed_cursor(fp, query, 1, "opaque α/+")
    assert helper._mixed_position(token, fp, query, 2) == (1, "opaque α/+")
    with pytest.raises(helper.ClientError, match="mixed_inner_cursor_too_large"):
        helper.mixed_cursor(fp, query, 1, '"' * 2048)
    with pytest.raises(helper.ClientError, match="mixed_cursor_sources_changed"):
        helper._mixed_position(token, "c" * 64, query, 0)
    ref = helper.mixed_reference(fp, "x-demo/node")
    assert helper.parse_mixed_reference(ref) == (fp, "x-demo/node")
    with pytest.raises(helper.ClientError, match="mixed_reference_invalid"):
        helper.mixed_reference(fp, "s-" + query + "/node")
    payload = json.loads(base64.urlsafe_b64decode(token[4:] + "=" * (-len(token[4:]) % 4)))
    assert payload == [1, fp, query, 1, "opaque α/+"]


def test_later_sources_expire_rows_and_empty_ownership_coverage(mixed):
    mixed.register()
    path = mixed.sources()
    current = [0.0]
    ref = "s-" + "a" * 64 + "/native"

    def native_reader(*args, **kwargs):
        rows = (
            []
            if kwargs["query"]["mine"]
            else [{"ref": ref, "node": "native", "holding_units": 1, "states": {"busy": 1}}]
        )
        return json.dumps(
            {
                "result": {
                    "rows": rows,
                    "has_more": False,
                    "next_cursor": None,
                    "scope_complete": True,
                    "http_requests": 2,
                    "valid_for_seconds": 5,
                    "coverage_valid_for_seconds": 5,
                }
            }
        )

    def provider_reader(path, **kwargs):
        current[0] += 6
        return json.dumps(
            {
                "result": {
                    "rows": [],
                    "has_more": False,
                    "next_cursor": None,
                    "scope_complete": True,
                    "http_requests": 2,
                    "valid_for_seconds": 60,
                    "coverage_valid_for_seconds": 60,
                    "inventory_scope": "full_inventory",
                }
            }
        )

    for mine in [False, True]:
        current[0] = 0
        result = json.loads(
            helper.mixed_resource_list(
                path,
                query=helper._mixed_query(mine=mine),
                clock=lambda: current[0],
                native_reader=native_reader,
                provider_reader=provider_reader,
            )
        )["result"]
        assert not result["scope_complete"]
        if not mine:
            row = result["rows"][0]
            assert row["holding_units"] is None and row["states"] == {"busy": None} and row["observation_incomplete"]


@pytest.mark.parametrize("encoded", [False, True])
def test_wrapped_inner_locator_cannot_hide_standard_secret_material(mixed, encoded):
    secret = "lbr_" + "synthetic-private-material" * 2
    inner = "x-demo/" + ("%6c" + secret[1:] if encoded else secret)
    namespace = "a" * 64
    with pytest.raises(helper.ClientError, match="mixed_reference_invalid"):
        helper.mixed_reference(namespace, inner)
    wrapped = "e1." + namespace + "." + base64.urlsafe_b64encode(inner.encode()).rstrip(b"=").decode()
    value = {
        "result": {
            "rows": [{"ref": wrapped}],
            "has_more": False,
            "next_cursor": None,
            "scope_complete": True,
            "http_requests": 2,
            "capabilities": ["read_only"],
            "observed_only": True,
        }
    }
    with pytest.raises(helper.ClientError, match="resource_provider_response_invalid"):
        helper._provider_response(
            value, action="list", query={"limit": 20}, reference=None, private_paths=[], ref_namespace=namespace
        )
    error = mixed.execute("/missing-sources", "resource", "view", wrapped, code=2)
    assert error["error"] == "mixed_reference_invalid" and secret not in str(error)


def test_wrapped_locator_private_path_is_checked_after_decoding():
    from urllib.parse import quote

    path = "/private/synthetic-config-file"
    namespace = "a" * 64
    wrapped = helper.mixed_reference(namespace, "x-demo/" + quote(path, safe=""))
    value = {
        "result": {
            "rows": [{"ref": wrapped}],
            "has_more": False,
            "next_cursor": None,
            "scope_complete": True,
            "http_requests": 2,
            "capabilities": ["read_only"],
            "observed_only": True,
        }
    }
    with pytest.raises(helper.ClientError, match="resource_provider_response_invalid"):
        helper._provider_response(
            value, action="list", query={"limit": 20}, reference=None, private_paths=[path], ref_namespace=namespace
        )


def test_unwrappable_child_cursor_is_a_gap_and_preserves_prior_native_rows(mixed):
    mixed.register()
    path = mixed.sources()
    native_ref = "s-" + "a" * 64 + "/native"

    def native_reader(*args, **kwargs):
        return json.dumps(
            {
                "result": {
                    "rows": [{"ref": native_ref, "node": "native"}],
                    "has_more": False,
                    "next_cursor": None,
                    "scope_complete": True,
                    "http_requests": 2,
                    "valid_for_seconds": 5,
                    "coverage_valid_for_seconds": 5,
                }
            }
        )

    def provider_reader(path, **kwargs):
        value = {
            "result": {
                "rows": [{"ref": helper.mixed_reference(kwargs["ref_namespace"], "x-demo/node")}],
                "has_more": True,
                "next_cursor": '"' * 2048,
                "scope_complete": False,
                "http_requests": 2,
                "valid_for_seconds": 60,
                "coverage_valid_for_seconds": 0,
                "capabilities": ["read_only"],
                "inventory_scope": "full_inventory",
                "observed_only": True,
            }
        }
        helper._provider_response(
            value,
            action="list",
            query=kwargs["query"],
            reference=None,
            private_paths=[],
            page_limit=kwargs["page_limit"],
            ref_namespace=kwargs["ref_namespace"],
        )
        return json.dumps(value)

    result = json.loads(
        helper.mixed_resource_list(
            path,
            query=helper._mixed_query(fields=["node"]),
            clock=lambda: 0,
            native_reader=native_reader,
            provider_reader=provider_reader,
        )
    )["result"]
    assert result["rows"][0]["ref"] == native_ref and len(result["rows"]) == 1
    assert result["unread_reasons"] == ["source_continuation_invalid"]
    assert result["http_requests"] == result["http_budget_used"] == 4 and not result["scope_complete"]


def test_worst_case_expiry_bytes_refine_once_and_keep_every_resource(mixed):
    mixed.register()
    path = mixed.sources(native=False)
    requests = []

    def reader(path, **kwargs):
        requests.append(kwargs)
        start = int(kwargs["cursor"]) if kwargs["cursor"] else 0
        end = min(80, start + kwargs["page_limit"])
        rows = [{"ref": helper.mixed_reference(kwargs["ref_namespace"], f"x-{number}")} for number in range(start, end)]
        return json.dumps(
            {
                "result": {
                    "rows": rows,
                    "has_more": end < 80,
                    "next_cursor": str(end) if end < 80 else None,
                    "scope_complete": end == 80,
                    "http_requests": 2,
                    "valid_for_seconds": 5,
                    "coverage_valid_for_seconds": 5,
                    "inventory_scope": "full_inventory",
                }
            }
        )

    query = helper._mixed_query(fields=["ref"], limit=256)
    first = json.loads(helper.mixed_resource_list(path, query=query, clock=lambda: 0, provider_reader=reader))["result"]
    assert first["packing_requeries"] == 1 and first["http_requests"] == 4
    assert 0 < len(first["rows"]) < 80 and first["has_more"]
    second = json.loads(
        helper.mixed_resource_list(
            path, query=query, cursor=first["next_cursor"], clock=lambda: 0, provider_reader=reader
        )
    )["result"]
    assert len({row["ref"] for row in first["rows"] + second["rows"]}) == 80 and not second["has_more"]
    assert [request["query"]["limit"] for request in requests] == [256, 256, 256]


def test_duplicate_registration_alias_does_not_double_read_or_count_inventory(mixed):
    first = mixed.register()
    alias = private(first.with_name("alias.json"), json.loads(first.read_text()))
    mixed.registrations.append(alias)
    error = mixed.execute(mixed.sources(native=False), "resource", "list", code=2)
    assert error["error"] == "mixed_sources_invalid" and not mixed.requests()


def test_packing_refinement_that_cannot_afford_source_fails_without_beginning_cursor(mixed):
    mixed.register()
    path = mixed.sources(native=False)
    value = json.loads(path.read_text())
    value["providers"][0]["minimum_http_requests"] = 1
    private(path, value)
    calls = []

    def reader(path, **kwargs):
        calls.append(kwargs["max_calls"])
        if kwargs["max_calls"] < 8:
            error = helper.ClientError("request_budget_exhausted")
            error.http_requests = 0
            raise error
        return json.dumps(
            {
                "result": {
                    "rows": [{"ref": helper.mixed_reference(kwargs["ref_namespace"], f"x-{n}")} for n in range(80)],
                    "has_more": False,
                    "next_cursor": None,
                    "scope_complete": True,
                    "http_requests": 8,
                    "valid_for_seconds": 5,
                    "coverage_valid_for_seconds": 5,
                    "inventory_scope": "full_inventory",
                }
            }
        )

    with pytest.raises(helper.ClientError, match="mixed_page_exceeds_output_budget"):
        helper.mixed_resource_list(
            path, query=helper._mixed_query(fields=["ref"], limit=256), clock=lambda: 0, provider_reader=reader
        )
    assert calls == [12, 4]


def test_understated_minimum_defers_unstarted_provider_instead_of_skipping(mixed):
    for index in range(5):
        state = mixed.aggregate.server(index + 1)
        mixed.aggregate.record(state, f"native-{index}")
    mixed.register(names=["external"], cost=4)
    path = mixed.sources()
    first = mixed.execute(path, "resource", "list", "--json", "node")["result"]
    assert len(first["rows"]) == 5 and first["http_requests"] == 10 and first["has_more"]
    assert not first["unread_reasons"]
    assert mixed.requests()[0]["budget"]["max_http_requests"] == 2
    second = mixed.execute(path, "resource", "list", "--json", "node", "--cursor", first["next_cursor"])["result"]
    assert second["rows"][0]["node"] == "external" and second["http_requests"] == 4


def test_registration_symlink_cannot_bypass_private_file_check(mixed):
    registration = mixed.register()
    link = registration.with_name("registration-link.json")
    link.symlink_to(registration)
    mixed.registrations[:] = [link]
    error = mixed.execute(mixed.sources(native=False), "resource", "list", code=2)
    assert error["error"] == "mixed_sources_invalid" and not mixed.requests()
