"""Generic reported-utilization protocol; synthetic stdio and pure helpers only."""

# ruff: noqa: F811 -- imported pytest fixtures
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.test_resource_agent_helper import helper
from backend.test_resource_agent_resources_all import aggregate  # noqa: F401
from backend.test_resource_list_metrics import metrics_source, prepare  # noqa: F401
from backend.test_resource_mixed_sources import mixed, private  # noqa: F401

MODE = "reported_resource_utilization_v1"


def metrics(**updates):
    resource = {
        "status": "reported",
        "reported_mean_percent": 37,
        "reported_memory_mean_percent": 25,
        "percent": None,
        "sample_age_seconds": None,
        "sample_time_known": False,
        "report_age_seconds": 3,
        "reported_units": 2,
        "memory_reported_units": 2,
        "selected_units": 2,
        "sample_states": {"reported": 2},
        "memory_sample_states": {"reported": 2},
        "selection_complete": True,
    }
    resource.update(updates)
    return {"mode": MODE, "best_effort": True, "does_not_authorize_resources": True, "resource": resource}


def page(rows=None, **updates):
    result = {
        "rows": rows if rows is not None else [{"ref": "x-demo/node", "node": "node", "metrics": metrics()}],
        "has_more": False,
        "next_cursor": None,
        "scope_complete": True,
        "http_requests": 2,
        "capabilities": ["read_only"],
        "observed_only": True,
        "valid_for_seconds": 60,
        "coverage_valid_for_seconds": 60,
        "inventory_scope": "full_inventory",
        "metrics_mode": MODE,
        "metrics_requested": True,
        "metrics_read_complete": True,
        "metrics_unread_reasons": [],
        "metrics_retry_after_seconds": None,
        "metrics_valid_for_seconds": 60,
    }
    result.update(updates)
    return {"result": result}


def query(**changes):
    value = {
        "nodes": [],
        "model": None,
        "fields": ["node", "metrics"],
        "limit": 20,
        "mine": False,
        "with_metrics": True,
    }
    value.update(changes)
    return value


def validate(value):
    return helper._provider_response(value, action="list", query=query(), reference=None, private_paths=[])


def test_reported_values_are_accepted_without_becoming_fresh_samples():
    result = validate(page())["result"]
    reading = result["rows"][0]["metrics"]["resource"]
    assert reading["status"] == "reported" and reading["reported_mean_percent"] == 37
    assert reading["percent"] is None and reading["sample_age_seconds"] is None
    assert reading["sample_time_known"] is False
    assert result["scope_complete"] and result["metrics_read_complete"]


def test_memory_only_values_keep_separate_coverage():
    value = page()
    value["result"]["rows"][0]["metrics"] = metrics(
        status="missing", reported_mean_percent=None, reported_units=0, sample_states={"missing": 2}
    )
    reading = validate(value)["result"]["rows"][0]["metrics"]["resource"]
    assert reading["reported_units"] == 0 and reading["memory_reported_units"] == 2
    assert reading["reported_memory_mean_percent"] == 25


@pytest.mark.parametrize(
    "field,value",
    [
        ("percent", 0),
        ("sample_age_seconds", 0),
        ("sample_time_known", True),
        ("report_age_seconds", -1),
        ("report_age_seconds", float("nan")),
        ("report_age_seconds", float("inf")),
        ("report_age_seconds", True),
        ("report_age_seconds", None),
        ("reported_mean_percent", -1),
        ("reported_mean_percent", 101),
        ("reported_mean_percent", True),
        ("reported_mean_percent", float("nan")),
        ("reported_mean_percent", "37"),
        ("reported_memory_mean_percent", 101),
        ("reported_units", True),
        ("reported_units", -1),
        ("reported_units", 1.5),
        ("reported_units", 3),
        ("memory_reported_units", 3),
        ("selected_units", True),
        ("selected_units", None),
        ("selected_units", 1),
        ("sample_states", {"ok": 2}),
        ("sample_states", {"reported": True}),
        ("sample_states", {"reported": 1}),
        ("memory_sample_states", {"reported": 1}),
        ("selection_complete", False),
        ("status", "ok"),
        ("driver_sample_at", 123),
    ],
)
def test_invalid_or_fake_fresh_reported_shapes_are_rejected(field, value):
    payload = page()
    payload["result"]["rows"][0]["metrics"]["resource"][field] = value
    with pytest.raises(helper.ClientError):
        validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "unreviewed-mode"),
        ("best_effort", False),
        ("does_not_authorize_resources", False),
    ],
)
def test_reported_outer_shape_cannot_claim_authority_or_another_mode(field, value):
    payload = page()
    payload["result"]["rows"][0]["metrics"][field] = value
    with pytest.raises(helper.ClientError):
        validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("metrics_mode", "unreviewed-mode"),
        ("metrics_requested", "true"),
        ("metrics_read_complete", None),
        ("metrics_unread_reasons", "missing"),
        ("metrics_unread_reasons", ["private detail"]),
        ("metrics_retry_after_seconds", -1),
        ("metrics_retry_after_seconds", True),
        ("metrics_valid_for_seconds", 61),
        ("metrics_valid_for_seconds", float("nan")),
    ],
)
def test_reply_requires_exact_acknowledgment_and_bounded_metadata(field, value):
    payload = page()
    payload["result"][field] = value
    with pytest.raises(helper.ClientError):
        validate(payload)


def test_malformed_metrics_retain_already_validated_source_request_cost():
    payload = page()
    payload["result"]["rows"][0]["metrics"]["resource"]["reported_mean_percent"] = 101
    with pytest.raises(helper.ClientError) as failure:
        validate(payload)
    assert getattr(failure.value, "http_requests", None) == 2


def test_known_empty_selection_is_not_a_zero_percent_measurement():
    payload = page()
    payload["result"]["rows"][0]["metrics"] = metrics(
        status="not_required",
        reported_mean_percent=None,
        reported_memory_mean_percent=None,
        reported_units=0,
        memory_reported_units=0,
        selected_units=0,
        sample_states={},
        memory_sample_states={},
    )
    reading = validate(payload)["result"]["rows"][0]["metrics"]["resource"]
    assert reading["reported_mean_percent"] is None and reading["selected_units"] == 0


def test_missing_optional_ack_preserves_valid_inventory_without_inventing_support():
    payload = page([{"ref": "x-demo/node", "node": "node", "states": {"idle": 1}}])
    result = payload["result"]
    for field in list(result):
        if field.startswith("metrics_"):
            result.pop(field)
    result = validate(payload)["result"]
    assert result["scope_complete"] and result["rows"][0]["states"] == {"idle": 1}
    assert result["metrics_read_complete"] is False and result["http_requests"] == 2
    assert result["metrics_unread_reasons"] == ["source_metrics_not_supported"]
    assert "metrics_mode" not in result and "metrics" not in result["rows"][0]


def test_missing_metrics_row_requires_explicit_detail_deferral():
    payload = page([{"ref": "x-demo/node", "node": "node", "observation_incomplete": True}])
    with pytest.raises(helper.ClientError):
        validate(payload)
    payload["result"].update(metrics_read_complete=False, metrics_unread_reasons=["metrics_detail_deferred"])
    result = validate(payload)["result"]
    assert result["metrics_read_complete"] is False and "metrics" not in result["rows"][0]


PROVIDER = r"""
import base64,copy,hashlib,json,pathlib,sys,urllib.parse
request=json.load(sys.stdin)
config_path=pathlib.Path(sys.argv[sys.argv.index('--config')+1])
config=json.loads(config_path.read_text())
with config_path.with_suffix('.events').open('a') as log: log.write(json.dumps(request)+'\n')
if request['action']=='fields':
    print(json.dumps({'json_fields':['ref','node','unit','metrics'],'required_row_fields':['ref']}));raise SystemExit(0)
query=request['query']; budget=request['budget']
enabled=query.get('with_metrics') is True
if config.get('old') and (enabled or 'metrics' in (query.get('fields') or [])):
    print(json.dumps({'error':'unsupported_query','retry_mutation':False,'http_requests':0}));raise SystemExit(2)
if config.get('mutate_registration'):
    path=pathlib.Path(config['mutate_registration']); registration=json.loads(path.read_text())
    registration.pop('capabilities',None);path.write_text(json.dumps(registration));path.chmod(0o600)
digest=hashlib.sha256(json.dumps(query,sort_keys=True).encode()).hexdigest()
offset=0
if request['cursor'] is not None:
    prefix,token_digest,position=request['cursor'].split(':'); assert prefix=='page' and token_digest==digest
    offset=int(position)
rows=[]
for name in config.get('names',['same-node']):
    reference='x-demo/'+urllib.parse.quote(name,safe='')
    if request['action']=='view' and reference!=request['ref']: continue
    if query['nodes'] and name.casefold() not in query['nodes']: continue
    row={'ref':reference,'node':name,'unit':'whole_node','snapshot_fresh':True,'states':{'idle':1}}
    if enabled: row['metrics']=copy.deepcopy(config['metrics'])
    rows.append(row)
all_rows=rows; rows=all_rows[offset:offset+budget.get('max_rows',query['limit'])]
if budget.get('reference_namespace'):
    for row in rows:
        encoded=base64.urlsafe_b64encode(row['ref'].encode()).rstrip(b'=').decode()
        row['ref']='e1.'+budget['reference_namespace']+'.'+encoded
if query.get('fields') is not None:
    rows=[{field:row[field] for field in ['ref',*query['fields']] if field in row} for row in rows]
def envelope(values):
    more=offset+len(values)<len(all_rows)
    result={'rows':values,'has_more':more,'next_cursor':('page:'+digest+':'+str(offset+len(values))) if more else None,
            'scope_complete':request['cursor'] is None and not more,'http_requests':2,'capabilities':['read_only'],
            'observed_only':True,'valid_for_seconds':60,'coverage_valid_for_seconds':60,'inventory_scope':'full_inventory'}
    if enabled:
        result.update(metrics_mode='reported_resource_utilization_v1',metrics_requested=True,metrics_read_complete=True,
                      metrics_unread_reasons=[],metrics_retry_after_seconds=None,metrics_valid_for_seconds=config.get('metrics_ttl',60))
    return {'result':result}
while len((json.dumps(envelope(rows),ensure_ascii=False)+'\n').encode())>budget.get('max_output_bytes',8192):
    if len(rows)>1: rows.pop()
    else:
        print(json.dumps({'error':'output_exceeds_budget','retry_mutation':False,'http_requests':2}))
        raise SystemExit(2)
print(json.dumps(envelope(rows),ensure_ascii=False))
"""


@pytest.fixture
def reported(mixed):
    def register(*, capable=True, **settings):
        registration = mixed.register(**{"metrics": metrics(), **settings})
        value = json.loads(registration.read_text())
        Path(value["command"][-1]).write_text(PROVIDER)
        if capable:
            value["capabilities"] = [MODE]
            private(registration, value)
        return registration

    mixed.register_reported = register
    return mixed


def test_capability_addition_keeps_reference_identity_but_invalidates_catalog_cursor(reported):
    registration = reported.register_reported(capable=False)
    source = reported.sources(native=False)
    before_phases, before_catalog = helper._mixed_sources(source)
    value = json.loads(registration.read_text())
    value["capabilities"] = [MODE]
    private(registration, value)
    after_phases, after_catalog = helper._mixed_sources(source)
    assert before_phases[0]["id"] == after_phases[0]["id"]
    assert before_catalog != after_catalog


def test_capability_difference_cannot_bypass_duplicate_provider_alias_detection(reported):
    registration = reported.register_reported(capable=False)
    value = json.loads(registration.read_text())
    value["capabilities"] = [MODE]
    alias = private(registration.with_name("capability-alias.json"), value)
    reported.registrations.append(alias)
    with pytest.raises(helper.ClientError):
        helper._mixed_sources(reported.sources(native=False))
    assert not reported.requests()


def test_direct_unsupported_metrics_is_rejected_before_provider_or_native_lookup(reported, monkeypatch, capsys):
    registration = reported.register_reported(capable=False, old=True)

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported provider telemetry must not enter native lookup")

    monkeypatch.setattr(helper, "Client", forbidden)
    monkeypatch.setattr(helper, "listed_connections", forbidden)
    assert helper.main(["--resource-provider", str(registration), "resource", "list", "--with-metrics"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]
    assert not reported.requests()


def test_legacy_provider_preserves_inventory_once_and_reports_metrics_gap(reported):
    reported.register_reported(capable=False, old=True)
    result = reported.execute(
        reported.sources(native=False), "resource", "list", "--with-metrics", "--json", "node,metrics"
    )["result"]
    assert [row["node"] for row in result["rows"]] == ["same-node"]
    assert result["scope_complete"] and not result["metrics_read_complete"]
    assert any("not_supported" in reason for reason in result["metrics_unread_reasons"])
    assert result["http_requests"] == result["http_budget_used"] == 2
    assert len(reported.requests()) == 1
    dispatched = reported.requests()[0]["query"]
    assert not dispatched.get("with_metrics") and "metrics" not in dispatched["fields"]


def test_malformed_metrics_quarantine_one_source_without_consuming_healthy_sources_budget(reported):
    reported.register_reported(metrics=metrics(reported_mean_percent=101))
    reported.register_reported(names=["healthy"])
    result = reported.execute(
        reported.sources(native=False), "resource", "list", "--with-metrics", "--json", "node,metrics"
    )["result"]
    assert [row["node"] for row in result["rows"]] == ["healthy"]
    assert result["known_http_requests"] == result["http_requests"] == result["http_budget_used"] == 4
    assert not result["scope_complete"] and not result["metrics_read_complete"]
    assert "resource_provider_response_invalid" in result["unread_reasons"]
    assert len(reported.requests(0)) == len(reported.requests(1)) == 1


def test_capable_provider_receives_bound_mode_without_native_fallback(reported, monkeypatch):
    reported.register_reported()

    def forbidden(*args, **kwargs):
        pytest.fail("External telemetry cannot dispatch a native read")

    monkeypatch.setattr(helper, "Client", forbidden)
    result = json.loads(
        helper.mixed_resource_list(
            reported.sources(native=False), query=helper._mixed_query(fields=["node", "metrics"], with_metrics=True)
        )
    )["result"]
    reading = result["rows"][0]["metrics"]["resource"]
    assert reading["reported_mean_percent"] == 37 and reading["percent"] is None
    assert result["scope_complete"] and result["metrics_read_complete"]
    assert reported.requests()[0]["query"]["with_metrics"] is True


def test_exact_e1_view_with_reported_metrics_stays_on_its_registered_provider(reported, monkeypatch, capsys):
    reported.register_reported()
    source = reported.sources(native=False)
    first = reported.execute(source, "resource", "list", "--with-metrics", "--json", "node,metrics")["result"]
    reference = first["rows"][0]["ref"]

    def forbidden(*args, **kwargs):
        pytest.fail("An external exact view cannot construct a native client")

    monkeypatch.setattr(helper, "Client", forbidden)
    assert (
        helper.main(
            [
                "--resource-sources",
                str(source),
                "resource",
                "view",
                reference,
                "--with-metrics",
                "--json",
                "node,metrics",
            ]
        )
        == 0
    )
    row = json.loads(capsys.readouterr().out)["result"]["rows"][0]
    assert row["ref"] == reference and row["metrics"]["resource"]["reported_mean_percent"] == 37
    assert [entry["action"] for entry in reported.requests()] == ["list", "view"]


def test_midflight_capability_removal_is_detected_even_when_e1_identity_is_stable(reported):
    registration = reported.register_reported()
    config_path = reported.configs[0]
    config = json.loads(config_path.read_text())
    config["mutate_registration"] = str(registration)
    private(config_path, config)
    with pytest.raises(helper.ClientError) as failure:
        helper.resource_provider_output(registration, action="list", query=query())
    assert failure.value.code == "resource_provider_registration_changed"
    assert getattr(failure.value, "http_requests", None) == 2
    assert len(reported.requests()) == 1


def test_failed_registration_reread_keeps_validated_request_cost(monkeypatch):
    registration = {
        "schema_version": 1,
        "command": ["/synthetic/provider"],
        "config_file": "/synthetic/config",
        "capabilities": [MODE],
    }
    reads = [registration, helper.ClientError("resource_provider_registration_invalid")]

    def load(path):
        result = reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def execute(*args, **options):
        options["stdout"].write(json.dumps(page()).encode())
        options["stdout"].flush()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(helper, "_provider_registration", load)
    monkeypatch.setattr(helper.subprocess, "run", execute)
    with pytest.raises(helper.ClientError) as failure:
        helper.resource_provider_output("/synthetic/registration", action="list", query=query())
    assert getattr(failure.value, "http_requests", None) == 2


def test_unavailable_native_scope_does_not_immediately_expire_another_scopes_healthy_metrics(reported, metrics_source):
    first, first_path, _mapping = prepare(reported.aggregate, metrics_source, 1)
    second, _second_path, _other_mapping = prepare(reported.aggregate, metrics_source, 2)
    second.extra["context"]["utilization"]["configuration"]["profile"] = "unsupported"
    source = reported.sources(native=True)

    def native_reader(directory, **options):
        return helper.resource_list(directory, root=reported.aggregate.script.parents[1], **options)

    result = json.loads(
        helper.mixed_resource_list(
            source,
            query=helper._mixed_query(fields=["node", "metrics"], with_metrics=True),
            native_reader=native_reader,
        )
    )["result"]
    ref = helper.resource_reference(json.loads(first_path.read_text()), "node-000", width=64)
    row = next(row for row in result["rows"] if row["ref"] == ref)
    assert result["scope_complete"] and not result["metrics_read_complete"]
    assert row["metrics"]["cpu"]["status"] == "ok" and row["metrics"]["cpu"]["percent"] == 0
    assert row["metrics"]["accelerator"]["mean_percent"] == 30
    assert first.key not in json.dumps(row)


def test_metrics_mode_change_invalidates_outer_cursor_before_launch(reported):
    reported.register_reported(names=["node-0", "node-1"])
    source = reported.sources(native=False)
    first = reported.execute(source, "resource", "list", "--with-metrics", "--json", "node,metrics", "--limit", 1)[
        "result"
    ]
    assert first["has_more"]
    requests = len(reported.requests())
    result = reported.execute(
        source, "resource", "list", "--json", "node,metrics", "--limit", 1, "--cursor", first["next_cursor"], code=2
    )
    assert "cursor" in result["error"] and len(reported.requests()) == requests


def test_mixed_holdback_expires_reported_values_without_erasing_valid_inventory(reported):
    reported.register_reported()
    reported.register_reported()
    source = reported.sources(native=False)
    clock, calls = [0], [0]

    def reader(path, **options):
        calls[0] += 1
        if calls[0] == 2:
            clock[0] = 2
        row = {
            "ref": helper.mixed_reference(options["ref_namespace"], "x-demo/node"),
            "node": "node",
            "states": {"idle": 1},
            "snapshot_fresh": True,
            "metrics": metrics(),
        }
        return json.dumps(page([row], metrics_valid_for_seconds=1 if calls[0] == 1 else 60))

    result = json.loads(
        helper.mixed_resource_list(
            source,
            query=helper._mixed_query(fields=["node", "states", "snapshot_fresh", "metrics"], with_metrics=True),
            clock=lambda: clock[0],
            provider_reader=reader,
        )
    )["result"]
    assert result["scope_complete"] and result["metrics_read_complete"]
    first, second = result["rows"]
    assert first["snapshot_fresh"] and first["states"] == {"idle": 1}
    stale = first["metrics"]["resource"]
    assert stale["status"] == "stale" and stale["reported_mean_percent"] is None
    assert stale["reported_memory_mean_percent"] is None and stale["report_age_seconds"] >= 5
    assert second["metrics"]["resource"]["status"] == "reported"


def test_reported_metrics_byte_paging_never_skips_unreturned_rows(reported):
    names = ["设备-" + str(index).zfill(3) + "-" + "x" * 80 for index in range(25)]
    reported.register_reported(names=names)
    source = reported.sources(native=False)
    cursor, rows = None, []
    for _ in range(25):
        arguments = ["resource", "list", "--with-metrics", "--json", "node,metrics", "--limit", 256]
        if cursor is not None:
            arguments += ["--cursor", cursor]
        result = reported.execute(source, *arguments)["result"]
        rows.extend(result["rows"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("Bounded synthetic paging did not terminate")
    assert [row["node"] for row in rows] == names
    assert len({row["ref"] for row in rows}) == len(names)
