# Optional proven-connection metadata adapter

This additive panel protocol supplies secret-free Agent handoff instructions for
one already-proven connection. It does not reveal keys, authorize resource writes,
or enable credential viewing/reset. Native `NativeProvider` remains on v1; an
external reporter never acquires this interface automatically. This implements a
bounded part of [#164](https://github.com/dynamicheart/lockbot/issues/164), not a
general private-platform resource-command protocol.

An operator may add this reviewed local configuration beside the existing provider;
existing provider/session storage is retained:

```json
{
  "connection_metadata": {
    "adapter": {"file": "/opt/plugins/connection_metadata.py", "config": {}},
    "allowed_service_origins": ["https://resources.example.test"],
    "instance_pins": {"https://resources.example.test": "verified-instance-id"}
  }
}
```

The module exports `create_metadata_adapter(config)`. Its object implements
`describe_connection(grant)`, receiving a copy of exactly one currently validated
provider grant. It returns `None` for unsupported grants or one dictionary with
exactly these fields (extra fields, including URLs/instructions, are rejected):

```json
{
  "service_id": "lockbot.resource-api", "api_version": "1",
  "service_origin": "https://resources.example.test", "instance_id": "verified-instance-id",
  "bot": {"id": 7, "uuid": "22222222-2222-4222-8222-222222222222", "name": "Development", "type": "NODE"},
  "actor": {"platform": "infoflow", "external_user_id": "verified-user"},
  "identity_revision": 1, "expires_at": null,
  "credential_recovery_health": "recoverable"
}
```

`credential_recovery_health` permits `recoverable`, `legacy_missing`, `unreadable`
or `unknown`. Use `unknown` if the upstream cannot authoritatively report recovery
health; do not infer it from a local adapter copy. `identity_revision: null`
means the upstream does not expose that display metadata; never guess zero. The
original provider must still validate the exact live grant, so this nullable field
is not authorization evidence. Exact native v1 revision behavior is unchanged. Unavailable binding authority is
a failure, not merely unknown recovery health.

The adapter must verify current authoritative native Resource API identity and the
exact grant's binding/bot scope; do not enumerate a user's other bots, infer a native
service from issuer labels, or relabel a report-only protocol as Resource API. Actor
platform/subject must equal the proven grant. Cached labels alone are insufficient
proof. Keep upstream keys in deployment-owned private storage, use bounded I/O and
fail closed on unavailable authority. Reviewed modules run with service privileges;
this is not a sandbox or remote plugin installer.

Core validates origin/instance pins and metadata shape, expiry and secret-token
patterns, revalidates the provider before releasing the batch, and checks logout,
revocation and scope changes. `None`/initially expired grants yield a partial result
when another connection is independently proven; no usable metadata yields denial.
Adapter failures expose a generic error, never their exception text. No ref/digest/
key fields are returned. Detailed task notes are not part of this protocol.

Anonymous `/api/personal-panel/capabilities` advertises only v2 metadata availability
and revalidation semantics, not service pins. Authenticated `/connections` returns
`schema_version: 2`, connections, a `some_authorizations_unavailable` flag and only
the `service_pins` used by that response. Each pin contains `origin`, `instance_id`,
`service_id` and `api_version`. Each connection has a generated selector, original
public browser identity, normalized metadata, core-generated `skill` URLs and
handoff/recovery instructions. The frontend accepts fixed Resource API ZIP/Markdown/
manifest paths only at those pinned origins/instances. It does not derive URLs from
reporter data or issuer display labels. V1 remains unchanged.

Each service's Skill links point to that service. Deployment-rendered package revision
portability across services is not solved here; following the correct source link
must not be confused with implementing a portable multi-service package updater.
Private adapter implementation, upstream credentials and deployment verification are
separate operator work. No private adapter or service address is bundled here.
