"""Strict, read-only projection of interrupted DUCC actual-ASGI observations.

This is a provenance adapter, not an evaluator. Version 1 rejects model answers,
tool execution and completed runs instead of assigning synthetic-matrix grades.
Private producer files are hash-checked inputs and are never published verbatim.
"""

import hashlib
import io
import json
import math
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from lockbot.backend.app.agent_skill.scripts.lockbot_cli import (  # noqa: E402
    BUNDLE_FORMAT,
    REFERENCE_PATHS,
    ClientError,
    canonical_package_version,
    content_version,
)
from synthetic_transcript import clean_text, read_native_bytes  # noqa: E402

KIND = "actual_api_observation_v1"
CASE = "actual-api-saved-resource-query"
MAX_INPUT_BYTES = 2 * 1024 * 1024
HASH = re.compile(r"[a-f0-9]{64}")
REVISION = re.compile(r"[a-f0-9]{16}")
COMMIT = re.compile(r"[a-f0-9]{40}")
MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "producer",
    "case_id",
    "source_id",
    "client",
    "model",
    "package_version",
    "source_commit",
    "sdk_sha256",
    "runner_sha256",
    "fixture",
    "files",
    "related",
}
PRIMARY_FILES = {
    "prompt",
    "request",
    "response",
    "result",
    "inputs",
    "summary",
    "native",
    "artifact_manifest",
    "artifact_zip",
    "producer_adapter",
    "producer_driver",
}
FORBIDDEN = {
    "answer",
    "model_answer",
    "grade",
    "score",
    "observable_pass",
    "semantic_status",
    "behavior",
    "assessments",
    "manual_review_required",
    "human_gate",
    "human_confirmation",
    "tool_calls",
    "cli_calls",
    "api_key",
    "password",
    "authorization",
    "apiKeyHelper",
    "settings",
}
FORBIDDEN = {key.casefold() for key in FORBIDDEN}
FIXTURE = {
    "kind": "actual_local_asgi_v1",
    "configuration": "saved_names_and_models",
    "allocation_state": "synthetic_empty",
    "prebound_credentials": "ephemeral",
}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError("Invalid actual API observation: " + reason)


def exact(value, expected):
    return type(value) is type(expected) and value == expected


def shape(value, keys):
    require(isinstance(value, dict) and set(value) == keys, "unsupported schema or fields")


def no_grade(value):
    if isinstance(value, dict):
        require(
            not FORBIDDEN.intersection(str(key).casefold() for key in value),
            "unsupported sensitive or evaluation fields",
        )
        for child in value.values():
            no_grade(child)
    elif isinstance(value, list):
        for child in value:
            no_grade(child)


def safe_text(value, limit):
    require(isinstance(value, str) and 0 < len(value) <= limit and value.isprintable(), "unsafe public text")
    require(clean_text(value) == value, "private text cannot be published")
    require(
        not re.search(r"(?i)lba1\.|apiKeyHelper|native-ducc-settings|key_file|--settings", value),
        "private text cannot be published",
    )
    return value


def number(value, maximum=86400):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum, "invalid duration")
    return value


class BoundFiles:
    def __init__(self, root, refs, expected):
        shape(refs, expected)
        self.root, self.refs, self.hashes, self.paths = root, refs, {}, {}

    def read(self, role):
        ref = self.refs[role]
        shape(ref, {"path", "sha256"})
        path = ref["path"]
        require(
            isinstance(path, str)
            and path
            and not Path(path).is_absolute()
            and not any(part in {".", ".."} for part in path.split("/"))
            and "\\" not in path,
            "unsafe evidence path",
        )
        require(isinstance(ref["sha256"], str) and HASH.fullmatch(ref["sha256"]), "invalid evidence hash")
        target = self.root / path
        content = read_native_bytes(target, self.root)
        require(
            len(content) <= MAX_INPUT_BYTES and not target.stat().st_mode & 0o077, "private bounded inputs required"
        )
        require(digest(content) == ref["sha256"], "evidence hash changed")
        self.hashes[role], self.paths[role] = ref["sha256"], target
        return content

    def json(self, role):
        value = json.loads(self.read(role))
        require(isinstance(value, dict), "expected evidence object")
        no_grade(value)
        return value


def request_identity(files, model):
    prompt = safe_text(files.read("prompt").decode(), 32768)
    request = files.json("request")
    shape(request, {"model", "prompt_file", "system_file", "mcp_config", "max_turns", "output_tokens"})
    require(
        request["model"] == model and request["prompt_file"] == str(files.paths["prompt"]),
        "request/prompt identity changed",
    )
    for key in ("system_file", "mcp_config"):
        path = Path(request[key])
        require(
            path.is_absolute() and path.resolve(strict=True) == path and path.is_relative_to(files.root),
            "request path escaped evidence root",
        )
    for key in ("max_turns", "output_tokens"):
        require(type(request[key]) is int and 0 < request[key] <= 100000, "invalid native bounds")
    return prompt


def native_projection(content, model, *, control=False):
    events = []
    ui_sessions = []
    for line in content.decode().splitlines():
        if line.strip():
            value = json.loads(line)
            require(isinstance(value, dict), "invalid native event")
            no_grade(value)
            if control and value.get("type") == "system" and value.get("subtype") == "ui_invalidate":
                shape(value, {"type", "subtype", "event", "prompts_screened", "session_id", "uuid"})
                require(
                    type(value["prompts_screened"]) is bool and isinstance(value["event"], str),
                    "invalid control UI metadata",
                )
                ui_sessions.append(value["session_id"])
                continue  # Known control-only UI metadata is not model/tool activity or public text.
            require(
                value.get("type") == "system" and value.get("subtype") in {"init", "api_retry"},
                "answers, tools or unknown native events are unsupported",
            )
            events.append(value)
    initial = [row for row in events if row["subtype"] == "init"]
    require(len(initial) <= 1 and (not events or events[0]["subtype"] == "init"), "native init identity missing")
    if initial:
        row = initial[0]
        require(
            row.get("model") == model and isinstance(row.get("session_id"), str) and row["session_id"],
            "native model/session identity changed",
        )
        require(all(value == row["session_id"] for value in ui_sessions), "control UI session changed")
        servers = row.get("mcp_servers")
        require(isinstance(servers, list), "native MCP metadata missing")
        if control:
            require(servers == [] and row.get("tools") == [], "control unexpectedly includes tools")
        else:
            require(
                len(servers) == 1 and servers[0].get("name") == "fixture" and servers[0].get("status") == "connected",
                "actual API tool host not connected",
            )
            require(
                set(row.get("tools", [])) == {"mcp__fixture__read_lockbot_skill", "mcp__fixture__run_lockbot"},
                "unexpected native tool catalog",
            )
    require(not ui_sessions or initial, "control UI metadata has no native identity")
    retries = []
    for row in events:
        if row["subtype"] != "api_retry":
            continue
        for key, minimum, maximum in (
            ("attempt", 1, 100),
            ("max_retries", 1, 100),
            ("error_status", 400, 599),
            ("retry_delay_ms", 0, 3600000),
        ):
            require(type(row.get(key)) is int and minimum <= row[key] <= maximum, "invalid native retry metadata")
        require(
            row["attempt"] == len(retries) + 1 and row["attempt"] <= row["max_retries"], "native retry sequence changed"
        )
        retries.append({key: row[key] for key in ("attempt", "max_retries", "error_status", "retry_delay_ms")})
    return {
        "native_init_present": bool(initial),
        "native_model_matches_requested": bool(initial),
        "native_session_identifier_observed": bool(initial),
        "native_MCP_connected": bool(initial) and not control,
        "control_UI_metadata_events": len(ui_sessions),
        "native_API_retries": retries,
        "assistant_events": 0,
        "final_result_events": 0,
        "usage": None,
    }


def artifact_identity(files, inputs, manifest):
    metadata = files.json("artifact_manifest")
    archive = files.read("artifact_zip")
    require(
        metadata.get("package_version") == manifest["package_version"] == inputs["package_version"],
        "package identity changed",
    )
    require(
        metadata.get("version") == inputs["rendered_version"]
        and metadata.get("installation", {}).get("sha256") == digest(archive) == inputs["rendered_zip_sha256"],
        "rendered archive identity changed",
    )
    expected_files = {
        "installation.json",
        "SKILL.md",
        "scripts/lockbot_cli.py",
        "references/connections.md",
        "references/resource-operations.md",
        "references/resource-queries.md",
        "references/workbench-and-metrics.md",
    }
    require(
        isinstance(inputs.get("installed_files"), dict) and set(inputs["installed_files"]) == expected_files,
        "unsupported package layout",
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        require(
            len(entries) == len(inputs["installed_files"]) and sum(v.file_size for v in entries) <= MAX_INPUT_BYTES,
            "incomplete or oversized package",
        )
        names = [v.filename for v in entries]
        require(
            len(names) == len(set(names))
            and set(names) == {"lockbot-agent/" + key for key in inputs["installed_files"]},
            "package layout changed",
        )
        contents = {key: zipped.read("lockbot-agent/" + key) for key in inputs["installed_files"]}
    require(
        all(digest(value) == inputs["installed_files"][key] for key, value in contents.items()),
        "installed package bytes changed",
    )
    require(
        digest(contents["scripts/lockbot_cli.py"]) == manifest["sdk_sha256"]
        and digest(contents["SKILL.md"]) == metadata.get("skill_sha256"),
        "packaged SDK/Skill changed",
    )
    stamp = json.loads(contents["installation.json"])
    require(
        stamp.get("package_version") == manifest["package_version"] and stamp.get("version") == metadata["version"],
        "package stamp changed",
    )
    references = {key: contents[key] for key in REFERENCE_PATHS}
    require(
        metadata.get("package_format") == stamp.get("package_format") == BUNDLE_FORMAT
        and content_version(contents["SKILL.md"], contents["scripts/lockbot_cli.py"], references) == metadata["version"]
        and canonical_package_version(
            contents["SKILL.md"], contents["scripts/lockbot_cli.py"], stamp.get("deployment_origin"), references
        )
        == manifest["package_version"]
        and stamp.get("files") == {key: digest(value) for key, value in contents.items() if key != "installation.json"},
        "canonical package identity changed",
    )
    return {
        "package_version": manifest["package_version"],
        "rendered_version": metadata["version"],
        "archive_sha256": digest(archive),
        "SDK_sha256": manifest["sdk_sha256"],
        "Skill_sha256": metadata["skill_sha256"],
    }


def zero_execution(result):
    for key in (
        "actual_http_requests",
        "actual_query_responses",
        "actual_resource_body_bytes",
        "cli_invocations",
        "resource_commands",
        "deployments",
    ):
        require(exact(result.get(key), 0), "observed model/tool activity is unsupported")
    require(
        result.get("cli_stdout_bytes") == []
        and result.get("native_turns") is None
        and exact(result.get("native_answer_present"), False),
        "model output is unsupported",
    )
    for key in ("state_unchanged", "configuration_unchanged", "no_production_requests"):
        require(exact(result.get(key), True), "actual API safety evidence changed")
    require(
        exact(result.get("model_invocations"), 1)
        and type(result.get("runner_exit_code")) is int
        and result["runner_exit_code"] in {-15, -9},
        "unsupported native termination",
    )


def no_tool_trace(files):
    # The producer records tool activity beside its request, independently of
    # summary counters. Never import a contradictory transcript as zero activity.
    for name in ("trace.jsonl", "cli-http.jsonl"):
        target = files.paths["request"].parent / name
        if target.exists() or target.is_symlink():
            require(read_native_bytes(target, files.root).strip() == b"", "observed tools are unsupported")


def related_projection(root, item, model, package):
    shape(item, {"kind", "files"})
    kind = item["kind"]
    require(kind in {"prior_native_timeout_v1", "minimal_native_control_v1"}, "unsupported related observation")
    expected = (
        {"prompt", "request", "response", "inputs", "result", "summary", "artifact_manifest", "artifact_zip"}
        if kind == "prior_native_timeout_v1"
        else {
            "prompt",
            "request",
            "summary",
            "verification",
            "native",
        }
    )
    files = BoundFiles(root, item["files"], expected)
    prompt = request_identity(files, model)
    summary = files.json("summary")
    if kind == "prior_native_timeout_v1":
        inputs, result = files.json("inputs"), files.json("result")
        zero_execution(result)
        require(files.json("response") == {"kind": "empty_or_timed_out"}, "prior model response is unsupported")
        no_tool_trace(files)
        require(
            exact(result.get("timed_out"), True)
            and result.get("model") == model
            and result.get("head") == inputs.get("head") == summary.get("source_commit"),
            "prior identity changed",
        )
        require(
            inputs.get("package_version") == package["package_version"]
            and inputs.get("sdk_sha256") == package["SDK_sha256"],
            "prior package identity changed",
        )
        require(
            exact(summary.get("native_stream_available"), False)
            and summary.get("model_usage_tokens") is None
            and summary.get("native_turns") is None
            and exact(summary.get("recorded_model_tool_calls"), 0)
            and exact(summary.get("recorded_model_http_calls"), 0)
            and exact(summary.get("product_or_semantic_pass_claimed"), False),
            "unsupported prior observation",
        )
        for key, expected_value in (
            ("single_native_invocation", True),
            ("native_invocations", 1),
            ("model_requests_confirmed", None),
            ("retry_count", 0),
            ("separate_provider_or_auth_probes", 0),
            ("timeout_seconds", 180),
            ("resource_commands", 0),
            ("production_requests", 0),
            ("deployments", 0),
            ("state_unchanged", True),
            ("configuration_unchanged", True),
            ("native_runner_exit_code", result["runner_exit_code"]),
        ):
            require(exact(summary.get(key), expected_value), "unsupported prior execution")
        require(number(summary.get("observed_runner_wall_seconds")) >= 180, "prior timeout duration changed")
        require(
            summary.get("process_cleanup")
            == {
                "driver_finally_closed_bridge": True,
                "runner_exit_observed": True,
                "no_remaining_mcp_or_native_process_for_case": True,
            },
            "prior cleanup evidence changed",
        )
        prior_package = artifact_identity(
            files, inputs, {"package_version": inputs["package_version"], "sdk_sha256": inputs["sdk_sha256"]}
        )
        projection = {
            "state": "timeout",
            "duration_seconds": number(summary.get("observed_runner_wall_seconds")),
            "native_stream_available": False,
            "source_commit": inputs["head"],
            "package": prior_package,
        }
    else:
        verification = files.json("verification")
        native = native_projection(files.read("native"), model, control=True)
        require(
            verification.get("requested_model") == model
            and verification.get("prompt") == prompt
            and verification.get("native_reported_models") == [model]
            and verification.get("summary_sha256") == files.hashes["summary"],
            "control identity changed",
        )
        for key, expected_value in (
            ("tools_disabled", True),
            ("no_skill_supplied", True),
            ("mcp_servers", 0),
            ("native_result_count", 0),
            ("native_init_count", 1),
        ):
            require(exact(verification.get(key), expected_value), "unsupported control execution")
        for key, expected_value in (
            ("skill_supplied", False),
            ("mcp_servers", 0),
            ("resource_api_calls", 0),
            ("assistant_events", 0),
            ("native_result_count", 0),
            ("model_invocations", 1),
            ("deployments", 0),
        ):
            require(exact(summary.get(key), expected_value), "unsupported control execution")
        require(
            summary.get("files", {}).get("native-ducc.jsonl", {}).get("sha256") == files.hashes["native"],
            "control native stream changed",
        )
        require(
            verification.get("retry_statuses") == [row["error_status"] for row in native["native_API_retries"]]
            and native["native_init_present"]
            and native["native_API_retries"]
            and exact(summary.get("native_init_count"), 1)
            and type(summary.get("exit_code")) is int
            and summary["exit_code"] in {-15, -9}
            and verification.get("current_owned_process_group_members") == [],
            "control retry/init identity changed",
        )
        require(
            summary.get("native_api_retries")
            == [
                {key: row[key] for key in ("attempt", "max_retries", "error_status")}
                for row in native["native_API_retries"]
            ],
            "control retry summary changed",
        )
        projection = {
            "state": "blocked",
            "duration_seconds": number(summary.get("duration_seconds")),
            "source_commit": summary.get("source_head"),
            "Skill_supplied": False,
            "package": None,
            "native_status": native,
        }
    require(
        isinstance(projection["source_commit"], str) and COMMIT.fullmatch(projection["source_commit"]),
        "invalid related source identity",
    )
    return {
        "kind": kind,
        "prompt": prompt,
        "client": "ducc",
        "requested_model": model,
        "assistant_answer_present": False,
        "model_tool_calls": 0,
        "usage": None,
        "not_a_separate_behavior_row_or_grade": True,
        **projection,
        "source_binding": files.hashes,
    }


def _load_observation(path):
    path = path.absolute()
    root = path.parent
    content = read_native_bytes(path, root)
    require(not path.stat().st_mode & 0o077 and len(content) <= 65536, "private bounded manifest required")
    manifest = json.loads(content)
    shape(manifest, MANIFEST_KEYS)
    require(
        exact(manifest["schema_version"], 1)
        and manifest["kind"] == KIND
        and manifest["producer"] == "lockbot_actual_asgi_read_v1"
        and manifest["case_id"] == CASE,
        "unsupported version, producer or case",
    )
    require(manifest["client"] == "ducc", "unsupported native client")
    model = safe_text(manifest["model"], 128)
    require(
        isinstance(manifest["source_id"], str)
        and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", manifest["source_id"]),
        "unsafe source identity",
    )
    safe_text(manifest["source_id"], 80)
    for key, pattern in (
        ("package_version", REVISION),
        ("source_commit", COMMIT),
        ("sdk_sha256", HASH),
        ("runner_sha256", HASH),
    ):
        require(isinstance(manifest[key], str) and pattern.fullmatch(manifest[key]), "invalid source/artifact identity")
    fixture = manifest["fixture"]
    shape(fixture, set(FIXTURE) | {"nodes", "cards", "saved_configuration_sha256"})
    require(all(fixture[key] == value for key, value in FIXTURE.items()), "unsupported actual API fixture")
    for key in ("nodes", "cards"):
        require(type(fixture[key]) is int and 0 <= fixture[key] <= 1000000, "invalid fixture size")
    require(
        isinstance(fixture["saved_configuration_sha256"], str)
        and HASH.fullmatch(fixture["saved_configuration_sha256"]),
        "invalid configuration hash",
    )
    files = BoundFiles(root, manifest["files"], PRIMARY_FILES)
    prompt = request_identity(files, model)
    inputs, result, summary = files.json("inputs"), files.json("result"), files.json("summary")
    zero_execution(result)
    require(files.json("response") == {"kind": "empty_or_timed_out"}, "model response is unsupported")
    require(
        result.get("model") == model
        and result.get("head") == inputs.get("head") == summary.get("source_commit") == manifest["source_commit"],
        "actual source/model identity changed",
    )
    for key in ("sdk_sha256", "runner_sha256"):
        require(inputs.get(key) == manifest[key], "actual producer identity changed")
    require(
        summary.get("SDK_sha256") == manifest["sdk_sha256"]
        and summary.get("runner_sha256") == manifest["runner_sha256"]
        and summary.get("package_version") == manifest["package_version"],
        "summary identity changed",
    )
    require(
        digest(files.read("producer_adapter")) == inputs.get("private_adapter_sha256")
        and digest(files.read("producer_driver")) == inputs.get("private_driver_sha256"),
        "producer code changed",
    )
    require(
        exact(inputs.get("configured_nodes"), fixture["nodes"])
        and exact(inputs.get("configured_cards"), fixture["cards"])
        and inputs.get("saved_config_sha256") == fixture["saved_configuration_sha256"],
        "fixture provenance changed",
    )
    native_content = files.read("native")
    native = native_projection(native_content, model)
    require(
        summary.get("native_stream_sha256") == files.hashes["native"]
        and exact(summary.get("native_stream_bytes"), len(native_content)),
        "native stream identity changed",
    )
    for key in (
        "assistant_events",
        "final_result_events",
        "model_backend_http_recorded",
        "model_tools_recorded",
        "production_requests",
        "resource_commands",
        "deployments",
        "external_retry_or_probe_invocations",
    ):
        require(exact(summary.get(key), 0), "unsupported summary activity")
    for key in ("usage_observed", "upstream_model_dispatch_confirmed", "verified_native_session_file_created"):
        require(exact(summary.get(key), False), "unsupported summary outcome")
    for key in ("resource_state_unchanged", "configuration_unchanged", "bridge_closed", "no_remaining_case_processes"):
        require(exact(summary.get(key), True), "actual API cleanup/state evidence changed")
    require(
        summary.get("model_token_usage") is None
        and exact(summary.get("native_invocations"), 1)
        and summary.get("runner_exit_code") == result["runner_exit_code"],
        "unsupported native execution",
    )
    require(
        type(result.get("timed_out")) is bool and exact(summary.get("outer_timeout_reached"), result["timed_out"]),
        "termination status changed",
    )
    state = "timeout" if result["timed_out"] else "blocked"
    require(
        state == "timeout" or native["native_init_present"] and native["native_API_retries"],
        "blocked observation lacks native failure evidence",
    )
    for key in ("native_init_present", "native_model_matches_requested"):
        require(exact(summary.get(key), native[key]), "native summary identity changed")
    require(
        exact(summary.get("native_session_identifier_present"), native["native_init_present"])
        and summary.get("native_MCP_status")
        == ([{"name": "fixture", "status": "connected"}] if native["native_init_present"] else []),
        "native session/MCP summary changed",
    )
    summary_retries = summary.get("native_internal_retry_events")
    require(
        isinstance(summary_retries, list)
        and all(isinstance(row, dict) for row in summary_retries)
        and encoded(
            [
                {key: row.get(key) for key in ("attempt", "max_retries", "error_status", "retry_delay_ms")}
                for row in summary_retries
                if isinstance(row, dict)
            ]
        )
        == encoded(native["native_API_retries"]),
        "native retry summary changed",
    )
    require(
        exact(summary.get("original_timeout_seconds"), 180)
        and exact(summary.get("stopped_early"), not result["timed_out"])
        and inputs.get("source_status") == ""
        and summary.get("source_status") == "clean",
        "unsupported producer bounds/source status",
    )
    duration = number(summary.get("stopped_after_seconds"))
    require(duration >= 180 if result["timed_out"] else duration < 180, "termination duration changed")
    no_tool_trace(files)
    package = artifact_identity(files, inputs, manifest)
    related = manifest["related"]
    require(isinstance(related, list) and len(related) <= 2, "too many related observations")
    kinds = [item.get("kind") for item in related if isinstance(item, dict)]
    require(len(kinds) == len(related) == len(set(kinds)), "duplicate related observations")
    attachments = [related_projection(root, item, model, package) for item in related]
    projection = {
        "schema_version": 1,
        "kind": KIND,
        "source_id": manifest["source_id"],
        "case_id": CASE,
        "client": "ducc",
        "requested_model": model,
        "state": state,
        "source_commit": manifest["source_commit"],
        "runner_sha256": manifest["runner_sha256"],
        "package": package,
        "fixture": fixture,
        "native_status": native,
        "duration_seconds": duration,
        "runner_exit_code": result["runner_exit_code"],
        "model_tool_calls": 0,
        "CLI_calls": 0,
        "resource_HTTP_requests": 0,
        "assistant_answer_present": False,
        "usage": None,
        "semantic_evaluation": "not_evaluated",
        "manual_confirmation_required": False,
        "upstream_model_dispatch_confirmed": False,
        "related": attachments,
        "source_binding": files.hashes,
    }
    return projection, prompt


def load_observation(path):
    try:
        return _load_observation(path)
    except (
        KeyError,
        TypeError,
        AttributeError,
        UnicodeError,
        RecursionError,
        OverflowError,
        zipfile.BadZipFile,
        ClientError,
    ):
        raise ValueError("Invalid actual API observation: malformed producer evidence") from None
