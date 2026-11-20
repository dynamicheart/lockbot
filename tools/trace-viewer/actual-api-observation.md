# Actual API observation input

This local import is a provenance adapter, not a grader or an execution harness.
It accepts only the interrupted DUCC producer described below. Successful answers
and tool execution require a separately reviewed producer contract; version 1
rejects them. It never invokes a client, network service or authentication helper.

The private manifest is a JSON object with these exact fields:

| Field | Version 1 value or constraint |
| --- | --- |
| `schema_version` | Integer `1` |
| `kind` | `actual_api_observation_v1` |
| `producer` | `lockbot_actual_asgi_read_v1` |
| `case_id` | Stable `actual-api-saved-resource-query` |
| `source_id` | Public, bounded source identifier |
| `client`, `model` | `ducc` and the exact requested/native model |
| `package_version` | Canonical 16-character package revision |
| `source_commit` | Full source commit hash |
| `sdk_sha256`, `runner_sha256` | Exact source hashes |
| `fixture` | Object described below |
| `files` | Exact role-to-file bindings described below |
| `related` | Up to two distinct typed attempts described below |

Every file binding has exactly `path` and `sha256`. Paths are relative to the
manifest's directory. Symlinks, escapes, nonregular files, hash changes and files
readable by other users are rejected. The manifest is bounded to 64 KiB and each
input to 2 MiB. The request's prompt path must point at its bound prompt; its
system and MCP configuration paths must remain within the same private root.
Those latter files are not published.

`fixture` has exactly `kind: actual_local_asgi_v1`,
`configuration: saved_names_and_models`, `allocation_state: synthetic_empty`,
`prebound_credentials: ephemeral`, integer `nodes` and `cards`, and
`saved_configuration_sha256`. These describe a local backend prepared from saved
configuration names/models, not production allocations or a synthetic HTTP server.

The primary `files` roles are `prompt`, `request`, `response`, `result`, `inputs`,
`summary`, `native`, `artifact_manifest`, `artifact_zip`, `producer_adapter` and
`producer_driver`. Their contents must match the existing actual-ASGI read
producer: one bounded invocation, no model answer, no CLI/tool/resource HTTP
activity, unchanged resource/configuration state and completed process cleanup.
The canonical seven-file installed Skill archive is checked against its manifest,
stamp, package revision, rendered revision, SDK hash and per-file hashes.

Native events are limited to one matching `system/init` and sequential
`system/api_retry` records. A blocked observation requires recorded failure
metadata; a timeout can have no captured native events. Native identity, MCP
connection, retry metadata, summary, termination and duration must agree. Neither
an init event nor a retry establishes upstream model dispatch or token usage.
Raw native fields, error strings, session identifiers and private paths are not
copied into the exported allowlist.

Each related attempt has exactly `kind` and `files`:

- `prior_native_timeout_v1`: `prompt`, `request`, `inputs`, `result`, `summary`,
  `artifact_manifest`, `artifact_zip`; the older bounded timeout without a native
  stream, checked against its own rendered archive and the same canonical SDK.
- `minimal_native_control_v1`: `prompt`, `request`, `summary`, `verification`,
  `native`; an interrupted short prompt with no Skill, MCP server or tool. The
  producer's known `system/ui_invalidate` metadata is allowed only here and must
  correlate with the native session. It is never conversation or tool activity.

Only reconstructed public fields and role-to-SHA bindings enter a single
content-addressed diagnostic attachment. Related attempts do not add behavior
rows. The behavior row contains the exact user prompt, an explicit missing-answer
notice, zero recorded model/CLI/HTTP calls, unavailable usage and no assessments.
The existing immutable dataset registry, validation and evidence checks remain
responsible for sealing and appending the result. Existing datasets, comments,
anchors and matrix assessment policies are unchanged.
