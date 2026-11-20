"""Interrupted actual-ASGI observations never acquire a synthetic behavior grade."""

import io
import json
import shutil
import subprocess
import sys
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest
from test_trace_dataset import publisher
from test_trace_merge import matrix

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/trace-viewer"))
from actual_api_observation import CASE, KIND, PRIMARY_FILES, native_projection, strict_json  # noqa: E402
from lockbot.backend.app.agent_skill.scripts.lockbot_cli import (  # noqa: E402
    BUNDLE_FORMAT,
    REFERENCE_PATHS,
    canonical_package_version,
    content_version,
)

MODEL = "official:gpt-6-luna"


def write(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode())
    path.chmod(0o600)
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def fixture(tmp_path, *, timeout=False):
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    contents = {
        "SKILL.md": b"Synthetic public Skill\n<!-- LOCKBOT_DEPLOYMENT_API_ADDRESSES -->",
        "scripts/lockbot_cli.py": b"# Synthetic SDK\n",
        **{name: b"Synthetic reference" for name in REFERENCE_PATHS},
    }
    refs = {name: contents[name] for name in REFERENCE_PATHS}
    version = content_version(contents["SKILL.md"], contents["scripts/lockbot_cli.py"], refs)
    package = canonical_package_version(contents["SKILL.md"], contents["scripts/lockbot_cli.py"], "", refs)
    stamp = {
        "package_version": package,
        "version": version,
        "package_format": BUNDLE_FORMAT,
        "deployment_origin": "",
        "files": {key: sha256(value).hexdigest() for key, value in contents.items()},
    }
    contents["installation.json"] = json.dumps(stamp).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for key, value in contents.items():
            zipped.writestr("lockbot-agent/" + key, value)
    archive = buffer.getvalue()
    sdk_hash = sha256(contents["scripts/lockbot_cli.py"]).hexdigest()
    files = {}

    def put(role, name, value):
        ref = write(root / name, value)
        ref["path"] = str(Path(ref["path"]).relative_to(root))
        files[role] = ref

    put("producer_adapter", "adapter.py", b"# Private producer adapter; never published\n")
    put("producer_driver", "driver.py", b"# Private producer driver; never published\n")
    put("prompt", "phase-1/prompt.txt", b"List available resources")
    write(root / "phase-1/system.txt", b"Private system /Users/synthetic/private not-a-real-secret")
    write(root / "phase-1/mcp.json", {})
    put(
        "request",
        "phase-1/request.json",
        {
            "model": MODEL,
            "prompt_file": str(root / files["prompt"]["path"]),
            "system_file": str(root / "phase-1/system.txt"),
            "mcp_config": str(root / "phase-1/mcp.json"),
            "max_turns": 16,
            "output_tokens": 2000,
        },
    )
    init = {
        "type": "system",
        "subtype": "init",
        "model": MODEL,
        "session_id": "b335b64d-df43-4b21-81e4-8b604f8f3333",
        "cwd": "/Users/synthetic/private",
        "apiKeySource": "private-helper-metadata",
        "mcp_servers": [{"name": "fixture", "status": "connected"}],
        "tools": ["mcp__fixture__read_lockbot_skill", "mcp__fixture__run_lockbot"],
    }
    retry = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 10,
        "error_status": 503,
        "retry_delay_ms": 500,
        "error": "not-a-real-secret",
    }
    native = b"" if timeout else (json.dumps(init) + "\n" + json.dumps(retry) + "\n").encode()
    put("native", "phase-1/native-ducc.jsonl", native)
    put("response", "phase-1/response.json", {"kind": "empty_or_timed_out"})
    result = {
        "model": MODEL,
        "head": "a" * 40,
        "model_invocations": 1,
        "runner_exit_code": -15,
        "timed_out": timeout,
        "native_answer_present": False,
        "native_turns": None,
        "actual_http_requests": 0,
        "actual_query_responses": 0,
        "actual_resource_body_bytes": 0,
        "cli_invocations": 0,
        "cli_stdout_bytes": [],
        "resource_commands": 0,
        "deployments": 0,
        "state_unchanged": True,
        "configuration_unchanged": True,
        "no_production_requests": True,
    }
    put("result", "result.json", result)
    inputs = {
        "head": "a" * 40,
        "sdk_sha256": sdk_hash,
        "runner_sha256": "b" * 64,
        "package_version": package,
        "rendered_version": version,
        "rendered_zip_sha256": sha256(archive).hexdigest(),
        "installed_files": {key: sha256(value).hexdigest() for key, value in contents.items()},
        "private_adapter_sha256": files["producer_adapter"]["sha256"],
        "private_driver_sha256": files["producer_driver"]["sha256"],
        "configured_nodes": 25,
        "configured_cards": 200,
        "saved_config_sha256": "c" * 64,
        "source_status": "",
    }
    put("inputs", "inputs.json", inputs)
    summary = {
        "source_commit": "a" * 40,
        "SDK_sha256": sdk_hash,
        "runner_sha256": "b" * 64,
        "package_version": package,
        "native_stream_sha256": files["native"]["sha256"],
        "native_stream_bytes": len(native),
        "native_init_present": not timeout,
        "native_model_matches_requested": not timeout,
        "native_session_identifier_present": not timeout,
        "native_MCP_status": [] if timeout else [{"name": "fixture", "status": "connected"}],
        "native_invocations": 1,
        "native_internal_retry_events": [] if timeout else [retry],
        "runner_exit_code": -15,
        "outer_timeout_reached": timeout,
        "original_timeout_seconds": 180,
        "stopped_early": not timeout,
        "stopped_after_seconds": 180.1 if timeout else 84.8,
        "source_status": "clean",
        "model_token_usage": None,
        "usage_observed": False,
        "upstream_model_dispatch_confirmed": False,
        "verified_native_session_file_created": False,
        "resource_state_unchanged": True,
        "configuration_unchanged": True,
        "bridge_closed": True,
        "no_remaining_case_processes": True,
        **{
            key: 0
            for key in (
                "assistant_events",
                "final_result_events",
                "model_backend_http_recorded",
                "model_tools_recorded",
                "production_requests",
                "resource_commands",
                "deployments",
                "external_retry_or_probe_invocations",
            )
        },
    }
    put("summary", "summary.json", summary)
    put(
        "artifact_manifest",
        "artifacts/manifest.json",
        {
            "package_version": package,
            "version": version,
            "package_format": BUNDLE_FORMAT,
            "skill_sha256": sha256(contents["SKILL.md"]).hexdigest(),
            "installation": {"sha256": sha256(archive).hexdigest()},
        },
    )
    put("artifact_zip", "artifacts/lockbot-agent.zip", archive)
    manifest = {
        "schema_version": 1,
        "kind": KIND,
        "producer": "lockbot_actual_asgi_read_v1",
        "case_id": CASE,
        "source_id": "separate-observation",
        "client": "ducc",
        "model": MODEL,
        "package_version": package,
        "source_commit": "a" * 40,
        "sdk_sha256": sdk_hash,
        "runner_sha256": "b" * 64,
        "files": files,
        "related": [],
        "fixture": {
            "kind": "actual_local_asgi_v1",
            "configuration": "saved_names_and_models",
            "allocation_state": "synthetic_empty",
            "prebound_credentials": "ephemeral",
            "nodes": 25,
            "cards": 200,
            "saved_configuration_sha256": "c" * 64,
        },
    }
    assert set(files) == PRIMARY_FILES
    path = root / "observation.json"
    write(path, manifest)
    return path


def mutate(path, role, change):
    manifest = json.loads(path.read_text())
    target = path.parent / manifest["files"][role]["path"]
    value = json.loads(target.read_text())
    change(value)
    write(target, value)
    manifest["files"][role]["sha256"] = sha256(target.read_bytes()).hexdigest()
    write(path, manifest)


def add_related(source):
    root = source.parent
    prior_parent = root / "prior"
    prior_parent.mkdir(mode=0o700)
    prior = fixture(prior_parent, timeout=True)
    prior_manifest = json.loads(prior.read_text())
    summary = {
        "source_commit": "a" * 40,
        "native_stream_available": False,
        "model_usage_tokens": None,
        "native_turns": None,
        "recorded_model_tool_calls": 0,
        "recorded_model_http_calls": 0,
        "product_or_semantic_pass_claimed": False,
        "single_native_invocation": True,
        "native_invocations": 1,
        "model_requests_confirmed": None,
        "retry_count": 0,
        "separate_provider_or_auth_probes": 0,
        "timeout_seconds": 180,
        "resource_commands": 0,
        "production_requests": 0,
        "deployments": 0,
        "state_unchanged": True,
        "configuration_unchanged": True,
        "native_runner_exit_code": -15,
        "observed_runner_wall_seconds": 180.01,
        "process_cleanup": {
            "driver_finally_closed_bridge": True,
            "runner_exit_observed": True,
            "no_remaining_mcp_or_native_process_for_case": True,
        },
    }
    mutate(prior, "summary", lambda value: (value.clear(), value.update(summary)))
    prior_manifest = json.loads(prior.read_text())
    prior_roles = {"prompt", "request", "response", "inputs", "result", "summary", "artifact_manifest", "artifact_zip"}
    prior_refs = {
        role: {**ref, "path": str((prior.parent / ref["path"]).relative_to(root))}
        for role, ref in prior_manifest["files"].items()
        if role in prior_roles
    }
    control = root / "control"
    refs = {}

    def put(role, name, value):
        ref = write(control / name, value)
        ref["path"] = str(Path(ref["path"]).relative_to(root))
        refs[role] = ref

    put("prompt", "prompt.txt", b"Reply with OK only.")
    write(control / "system.txt", b"Private system")
    write(control / "mcp.json", {})
    put(
        "request",
        "request.json",
        {
            "model": MODEL,
            "prompt_file": str(control / "prompt.txt"),
            "system_file": str(control / "system.txt"),
            "mcp_config": str(control / "mcp.json"),
            "max_turns": 1,
            "output_tokens": 16,
        },
    )
    retry = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 10,
        "error_status": 503,
        "retry_delay_ms": 500,
    }
    events = [
        {
            "type": "system",
            "subtype": "ui_invalidate",
            "event": "private UI metadata",
            "prompts_screened": True,
            "session_id": "control-session",
            "uuid": "opaque",
        },
        {
            "type": "system",
            "subtype": "init",
            "model": MODEL,
            "session_id": "control-session",
            "tools": [],
            "mcp_servers": [],
        },
        retry,
    ]
    put("native", "native-ducc.jsonl", ("\n".join(json.dumps(v) for v in events) + "\n").encode())
    put(
        "summary",
        "summary.json",
        {
            "skill_supplied": False,
            "mcp_servers": 0,
            "resource_api_calls": 0,
            "assistant_events": 0,
            "native_result_count": 0,
            "model_invocations": 1,
            "deployments": 0,
            "exit_code": -15,
            "native_init_count": 1,
            "native_api_retries": [{key: retry[key] for key in ("attempt", "max_retries", "error_status")}],
            "duration_seconds": 2.3,
            "source_head": "d" * 40,
            "files": {"native-ducc.jsonl": {"sha256": refs["native"]["sha256"]}},
        },
    )
    put(
        "verification",
        "verification.json",
        {
            "requested_model": MODEL,
            "prompt": "Reply with OK only.",
            "native_reported_models": [MODEL],
            "summary_sha256": refs["summary"]["sha256"],
            "tools_disabled": True,
            "no_skill_supplied": True,
            "mcp_servers": 0,
            "native_result_count": 0,
            "native_init_count": 1,
            "retry_statuses": [503],
            "current_owned_process_group_members": [],
        },
    )
    manifest = json.loads(source.read_text())
    manifest["related"] = [
        {"kind": "prior_native_timeout_v1", "files": prior_refs},
        {"kind": "minimal_native_control_v1", "files": refs},
    ]
    write(source, manifest)


def mutate_related(path, index, role, change):
    manifest = json.loads(path.read_text())
    ref = manifest["related"][index]["files"][role]
    target = path.parent / ref["path"]
    value = json.loads(target.read_text())
    change(value)
    write(target, value)
    ref["sha256"] = sha256(target.read_bytes()).hexdigest()
    write(path, manifest)


def tree(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    "role", ["manifest", "request", "response", "result", "inputs", "summary", "artifact_manifest", "native"]
)
def test_duplicate_json_keys_are_rejected_in_every_producer_json_surface(tmp_path, role):
    source = fixture(tmp_path)
    manifest = json.loads(source.read_text())
    if role == "manifest":
        target = source
        prefix = b'{"schema_version":2,'
    else:
        target = source.parent / manifest["files"][role]["path"]
        keys = {
            "request": ("model", '"OtherModel"'),
            "response": ("kind", '"forged_answer"'),
            "result": ("actual_http_requests", "1"),
            "inputs": ("head", '"forged_source"'),
            "summary": ("model_tools_recorded", "1"),
            "artifact_manifest": ("version", '"forged_version"'),
            "native": ("model", '"OtherModel"'),
        }
        key, value = keys[role]
        prefix = ("{" + json.dumps(key) + ":" + value + ",").encode()
    write(target, prefix + target.read_bytes()[1:])
    if role != "manifest":
        manifest["files"][role]["sha256"] = sha256(target.read_bytes()).hexdigest()
        write(source, manifest)
    with pytest.raises(ValueError, match="duplicate JSON"):
        publisher.export_actual_api_observation(source, tmp_path / "board")
    assert not (tmp_path / "board/datasets/index.json").exists()


@pytest.mark.parametrize(
    "content", ['{"nested":{"count":1,"count":0}}', '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}']
)
def test_strict_json_rejects_nested_duplicates_and_nonstandard_numeric_constants(content):
    with pytest.raises(ValueError):
        strict_json(content)


def test_duplicate_installed_stamp_keys_are_rejected_after_archive_hashes_are_rebound(tmp_path):
    source = fixture(tmp_path)
    manifest = json.loads(source.read_text())
    target = source.parent / manifest["files"]["artifact_zip"]["path"]
    with zipfile.ZipFile(target) as zipped:
        entries = {name: zipped.read(name) for name in zipped.namelist()}
    stamp_name = "lockbot-agent/installation.json"
    entries[stamp_name] = b'{"package_version":"forged_version",' + entries[stamp_name][1:]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, content in entries.items():
            zipped.writestr(name, content)
    write(target, buffer.getvalue())
    archive_hash = sha256(target.read_bytes()).hexdigest()
    manifest["files"]["artifact_zip"]["sha256"] = archive_hash
    write(source, manifest)
    mutate(
        source,
        "inputs",
        lambda v: (
            v.update(rendered_zip_sha256=archive_hash),
            v["installed_files"].update({"installation.json": sha256(entries[stamp_name]).hexdigest()}),
        ),
    )
    mutate(source, "artifact_manifest", lambda v: v["installation"].update(sha256=archive_hash))
    with pytest.raises(ValueError, match="duplicate JSON"):
        publisher.export_actual_api_observation(source, tmp_path / "board")
    assert not (tmp_path / "board/datasets/index.json").exists()


@pytest.mark.parametrize(
    "session", ["absent", None, "b335b64d-df43-4b21-81e4-8b604f8f3333", "other-session", False, 3, ""]
)
def test_retry_session_is_optional_but_must_match_init_when_explicit(tmp_path, session):
    source = fixture(tmp_path)
    manifest = json.loads(source.read_text())
    target = source.parent / manifest["files"]["native"]["path"]
    events = [json.loads(line) for line in target.read_text().splitlines()]
    if session != "absent":
        events[1]["session_id"] = session
    native = ("\n".join(json.dumps(row) for row in events) + "\n").encode()
    if session in ("absent", events[0]["session_id"]):
        assert native_projection(native, MODEL)["native_API_retries"][0]["error_status"] == 503
    else:
        with pytest.raises(ValueError, match="retry session identity"):
            native_projection(native, MODEL)


@pytest.mark.parametrize("timeout", [False, True])
def test_explicit_interrupted_observations_have_no_answer_grade_or_model_calls(tmp_path, timeout):
    source = fixture(tmp_path, timeout=timeout)
    before = tree(source.parent)
    report = publisher.export_actual_api_observation(source, tmp_path / "board")
    identity = report["dataset"]["dataset_id"]
    publisher.verify_dataset(tmp_path / "board/datasets", identity)
    raw = json.loads((tmp_path / "board/datasets" / identity / "raw.json").read_text())
    trial = raw["cases"][CASE][0]
    assert trial["state"] == ("timeout" if timeout else "blocked")
    assert [value["role"] for value in trial["sessions"][0]] == ["user", "missing", "tools"]
    assert trial["sessions"][0][0]["text"] == "List available resources"
    assert trial["sessions"][0][2]["steps"][0][0] == "native_client_diagnostic"
    assert trial["metrics"]["phases"][0] == {
        "phase": 1,
        "duration_seconds": 180.1 if timeout else 84.8,
        "tool_calls": 0,
        "model_tool_calls": 0,
        "cli_calls": 0,
        "http_requests": 0,
    }
    assert report["assessment_overlay"] is None
    assert not {"assessments", "observable_pass", "manual_review_required"}.intersection(raw)
    new = b"\n".join(tree(tmp_path / "board").values())
    assert b"not-a-real-secret" not in new and b"/Users/" not in new and str(tmp_path).encode() not in new
    assert before == tree(source.parent)


def test_related_attempts_are_distinct_hash_bound_diagnostics_not_new_runs(tmp_path):
    source = fixture(tmp_path)
    add_related(source)
    report = publisher.export_actual_api_observation(source, tmp_path / "board")
    assert report["dataset"]["run_count"] == report["dataset"]["case_count"] == 1
    projected = list((tmp_path / "board/evidence").iterdir())
    observation = next(
        json.loads(path.read_text()) for path in projected if json.loads(path.read_text()).get("kind") == KIND
    )
    assert [value["state"] for value in observation["related"]] == ["timeout", "blocked"]
    assert [value["kind"] for value in observation["related"]] == [
        "prior_native_timeout_v1",
        "minimal_native_control_v1",
    ]
    assert observation["related"][1]["package"] is None
    assert observation["related"][1]["native_status"]["control_UI_metadata_events"] == 1
    assert all(value["usage"] is None and value["model_tool_calls"] == 0 for value in observation["related"])


@pytest.mark.parametrize(
    "bad",
    [
        "prior-usage",
        "prior-source",
        "prior-timeout",
        "prior-probes",
        "prior-answer",
        "prior-trace",
        "control-init",
        "control-retry",
        "control-identity",
        "control-answer",
    ],
)
def test_related_attempt_contradictions_fail_closed(tmp_path, bad):
    source = fixture(tmp_path)
    add_related(source)
    if bad == "prior-usage":
        mutate_related(source, 0, "summary", lambda v: v.update(model_usage_tokens=1))
    elif bad == "prior-source":
        mutate_related(source, 0, "inputs", lambda v: v.update(head="e" * 40))
    elif bad == "prior-timeout":
        mutate_related(source, 0, "summary", lambda v: v.update(observed_runner_wall_seconds=1))
    elif bad == "prior-probes":
        mutate_related(source, 0, "summary", lambda v: v.update(separate_provider_or_auth_probes=1))
    elif bad == "prior-answer":
        mutate_related(source, 0, "response", lambda v: v.update(text="forged answer"))
    elif bad == "prior-trace":
        request = json.loads(source.read_text())["related"][0]["files"]["request"]["path"]
        write(source.parent / Path(request).parent / "trace.jsonl", b'{"tool":"run_lockbot"}\n')
    elif bad == "control-init":
        mutate_related(source, 1, "verification", lambda v: v.update(native_init_count=0))
    elif bad == "control-retry":
        mutate_related(source, 1, "summary", lambda v: v.update(native_api_retries=[]))
    elif bad == "control-identity":
        mutate_related(source, 1, "verification", lambda v: v.update(native_reported_models=["OtherModel"]))
    else:
        mutate_related(source, 1, "verification", lambda v: v.update(answer="forged"))
    with pytest.raises(ValueError):
        publisher.export_actual_api_observation(source, tmp_path / "board")
    assert not (tmp_path / "board/datasets/index.json").exists()


def test_explicit_command_mode_imports_without_using_a_matrix(tmp_path, monkeypatch, capsys):
    source = fixture(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["publish_dataset.py", str(source), str(tmp_path / "board"), "--actual-api-observation", "--minimal-summary"],
    )
    publisher.main()
    result = json.loads(capsys.readouterr().out)
    assert result["dataset"]["source_kind"] == KIND
    assert result["assessment_overlay"] is None
    assert not result["publication_authorized"]


@pytest.mark.parametrize("option", ["--cli-assessments", "--release-assessments", "--assessment-review"])
def test_observation_command_rejects_all_matrix_assessment_options(tmp_path, monkeypatch, capsys, option):
    source = fixture(tmp_path)
    args = ["publish_dataset.py", str(source), str(tmp_path / "board"), "--actual-api-observation", option]
    if option == "--assessment-review":
        args += ["unused-sidecar.json", "unused-report.md"]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as error:
        publisher.main()
    assert error.value.code == 2
    assert "do not accept synthetic assessment" in capsys.readouterr().err
    assert not (tmp_path / "board").exists()


def test_idempotency_and_fourteen_prior_datasets_comments_and_anchors_are_preserved(tmp_path):
    board = tmp_path / "board"
    for index in range(14):
        source, _ = matrix(tmp_path / f"old-{index}", version=f"{index:016x}", terminal=True)
        publisher.export_dataset(source, board)
    write(board / "review-comments.sqlite3", b"Existing comments unchanged")
    old = tree(board)
    source = fixture(tmp_path)
    first = publisher.export_actual_api_observation(source, board, minimal_summary=True)
    assert publisher.export_actual_api_observation(source, board, minimal_summary=True) == first
    index = json.loads((board / "datasets/index.json").read_text())["datasets"]
    assert len(index) == 15 and index[-1] == first["dataset"]["dataset_id"]
    assert all((board / name).read_bytes() == value for name, value in old.items() if name != "datasets/index.json")
    assert json.loads(old["datasets/index.json"])["datasets"] == index[:-1]


@pytest.mark.parametrize(
    "bad",
    [
        "version",
        "bool-version",
        "kind",
        "case",
        "client",
        "grade",
        "answer",
        "sensitive",
        "secret-model",
        "secret-prompt",
        "path",
        "symlink",
        "hash",
        "request-model",
        "source-commit",
        "package",
        "SDK",
        "native-model",
        "native-answer",
        "native-tool",
        "native-grade",
        "response",
        "trace",
        "summary-usage",
        "summary-tools",
        "summary-retry",
        "summary-session",
        "summary-MCP",
        "summary-retry-extra",
        "summary-duration",
        "duplicate-related",
    ],
)
def test_unverifiable_or_sensitive_observations_never_publish_membership(tmp_path, bad):
    source = fixture(tmp_path)
    manifest = json.loads(source.read_text())
    if bad in {"version", "bool-version"}:
        manifest["schema_version"] = 2 if bad == "version" else True
    elif bad == "kind":
        manifest["kind"] = "some_other_producer"
    elif bad == "case":
        manifest["case_id"] = "resource-cli-list-resources"
    elif bad == "client":
        manifest["client"] = "unknown-client"
    elif bad in {"grade", "answer", "sensitive"}:
        manifest[{"sensitive": "api_key"}.get(bad, bad)] = "forged"
    elif bad == "secret-model":
        manifest["model"] = "lbr_" + "K" * 43
    elif bad == "path":
        manifest["files"]["prompt"]["path"] = "../outside.txt"
    elif bad == "symlink":
        target = source.parent / manifest["files"]["prompt"]["path"]
        original = target.with_suffix(".original")
        target.rename(original)
        target.symlink_to(original)
    elif bad == "hash":
        manifest["files"]["inputs"]["sha256"] = "0" * 64
    elif bad == "duplicate-related":
        manifest["related"] = [{"kind": "minimal_native_control_v1", "files": {}}] * 2
    write(source, manifest)
    if bad == "request-model":
        mutate(source, "request", lambda v: v.update(model="OtherModel"))
    elif bad == "source-commit":
        mutate(source, "inputs", lambda v: v.update(head="d" * 40))
    elif bad == "package":
        mutate(source, "inputs", lambda v: v.update(package_version="d" * 16))
    elif bad == "SDK":
        mutate(source, "inputs", lambda v: v.update(sdk_sha256="d" * 64))
    elif bad == "response":
        mutate(source, "response", lambda v: v.update(text="Invented model success"))
    elif bad == "summary-usage":
        mutate(source, "summary", lambda v: v.update(usage_observed=True))
    elif bad == "summary-tools":
        mutate(source, "summary", lambda v: v.update(model_tools_recorded=1))
    elif bad == "summary-retry":
        mutate(source, "summary", lambda v: v.update(native_internal_retry_events=[]))
    elif bad == "summary-session":
        mutate(source, "summary", lambda v: v.update(native_session_identifier_present=False))
    elif bad == "summary-MCP":
        mutate(source, "summary", lambda v: v.update(native_MCP_status=[]))
    elif bad == "summary-retry-extra":
        mutate(source, "summary", lambda v: v["native_internal_retry_events"].append(None))
    elif bad == "summary-duration":
        mutate(source, "summary", lambda v: v.update(stopped_after_seconds=181))
    elif bad == "secret-prompt":
        manifest = json.loads(source.read_text())
        target = source.parent / manifest["files"]["prompt"]["path"]
        write(target, b"Read /Users/synthetic/private/key")
        manifest["files"]["prompt"]["sha256"] = sha256(target.read_bytes()).hexdigest()
        write(source, manifest)
    elif bad == "trace":
        write(source.parent / "phase-1/trace.jsonl", b'{"tool":"run_lockbot"}\n')
    elif bad.startswith("native-"):
        manifest = json.loads(source.read_text())
        target = source.parent / manifest["files"]["native"]["path"]
        events = [json.loads(line) for line in target.read_text().splitlines()]
        if bad == "native-model":
            events[0]["model"] = "OtherModel"
        elif bad == "native-grade":
            events[0]["grade"] = "pass"
        else:
            events.append(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use" if bad == "native-tool" else "text"}]},
                }
            )
        write(target, ("\n".join(json.dumps(v) for v in events) + "\n").encode())
        manifest["files"]["native"]["sha256"] = sha256(target.read_bytes()).hexdigest()
        write(source, manifest)
    with pytest.raises((ValueError, OSError)):
        publisher.export_actual_api_observation(source, tmp_path / "board")
    assert not (tmp_path / "board/datasets/index.json").exists()


def test_renderer_counts_diagnostic_group_as_logs_not_model_calls_and_marks_case_updated(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the existing renderer check")
    source = fixture(tmp_path)
    exported = publisher.export_actual_api_observation(source, tmp_path / "board")
    path = tmp_path / "board/datasets" / exported["dataset"]["dataset_id"] / "raw.json"
    script = """
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const fn=source.slice(source.indexOf('function buildCurrentBoard(snapshots)'));
const context={};vm.createContext(context);vm.runInContext(fn+';this.build=buildCurrentBoard',context);
const data=JSON.parse(fs.readFileSync(process.argv[2]));
const old={schema:1,version:'old',cases:{old:[{model:'Old',state:'pass',sessions:[]}]}};
const board=context.build([{data:old,metadata:{dataset_id:'old'}},{data,metadata:{dataset_id:'new'}}]);
if(Object.keys(board.cases).length!==2 ||
 !board.updated_cases.includes('actual-api-saved-resource-query'))process.exit(1);
const start=source.indexOf('function toolActivityLabel('),end=source.indexOf('function diagnosticDownload(',start);
vm.runInContext(source.slice(start,end)+';this.label=toolActivityLabel',context);
const entry=data.cases['actual-api-saved-resource-query'][0].sessions[0][2];
if(context.label(entry.steps,entry.evidence)!=='运行日志')process.exit(2);
"""
    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(Path(__file__).resolve().parents[2] / "frontend/src/features/agent-traces/interactions.js"),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
