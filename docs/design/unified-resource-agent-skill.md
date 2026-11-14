# One resource Skill, multiple scoped connections

Design follow-up for [#164](https://github.com/dynamicheart/lockbot/issues/164).
This document changes no API, plugin loader, credential, installed Skill or registry.
Unchecked work below is proposed, not an advertised protocol.

## User experience

Install `lockbot-resource-agent` once. The same Skill handles resource queries,
permitted resource commands and the optional personal dashboard. Adding another
service or group adds a privately stored connection, not another Skill. A private
registry can distribute that same package; it is not a separate personal-dashboard
Skill and does not become a credential authority.

Each native connection retains approved origin, verified instance ID, bot UUID,
verified platform/identity namespace/user, friendly label and `credential_ref`.
Resolve identity and bot from the existing authenticated context; users need not
supply UUIDs. Same-instance binding recovery preserves each bot's own key. Across
instances, equal usernames or resource names establish neither identity nor access.
Never send a saved key to another origin to discover whether it works.

A deployment plugin may contribute an authorized **read-only** personal report.
Display its source and permission separately from native bot connections. Seeing a
resource in a report does not authorize `lock`, renewal or release on that resource.
The public Skill contains only the common workflow; private service adapters,
addresses, matching rules and credentials remain in the deployment's private code
and configuration. Ordinary users should not install a second adapter Skill.

## What exists today

| Component | Implemented behavior | Limit |
| --- | --- | --- |
| Agent manifest/context | Public service/instance/API discovery; authenticated bot, identity, rules, operation links and optional metrics configuration | No dynamic Agent-visible reporter/extension directory |
| Native connections | Same-user binding directory/recovery; each resource key remains bot scoped | No cross-origin key discovery or implicit account merger |
| Personal dashboard | Existing Skill verifies `/api/personal-panel/capabilities`, issues a private one-use handoff and shares fixed `/#/me` for bookmarks | Browser report API is session/cookie based; it is not an authenticated Agent report API |
| Identity provider | `LOCKBOT_PERSONAL_PANEL_CONFIG` selects `NativeProvider` or reviewed local `create_provider(config)`; provider authenticates, validates and reports | Local module executes with service privileges; not sandboxed or remotely installable |
| Reporters | Reviewed local `create_reporter(config)` modules receive a verified grant, filter the exact subject, return groups/allocations and mark gaps unknown | Read-only browser aggregation; reporter presence alone grants no new Agent command capability |
| Identity directory | Optional `create_resolver(config)` uses a pinned directory and fresh unique mappings to label already-authorized principals | Equality is advisory; it does not add grants, discover keys or authorize other accounts |

These are existing deployment hooks in `personal_panel/setup.py`, `native.py` and
`router.py`. The app mounts them only when explicitly configured. The report path
revalidates grants, and source failure is unknown rather than an empty successful
report. Existing metrics configuration/grants are a separate data-source mechanism;
a utilization source is not an identity provider or resource-command backend.

## Authenticated extension discovery v1

The native Agent discovers the optional capability in its authenticated
`GET /api/agent/context` response. Only an active native binding with at least one
explicitly authorized extension receives `personal_report_extensions`. The
unauthenticated Skill manifest and the browser's panel capabilities never list
extensions. The field has this shape:

```json
{
  "protocol": "lockbot.personal-report.v1",
  "credential_mode": "same_origin_native",
  "items": [
    {
      "id": "team-report",
      "label": "Team resource report",
      "revision": "1",
      "mode": "read_only",
      "read_path": "/api/personal-panel/agent/extensions/team-report"
    }
  ]
}
```

IDs are operator-configured lowercase ASCII slugs. Labels are short plain text.
`revision` is an opaque adapter contract/configuration marker. The discovery
document contains only descriptors for which the exact caller is authorized.
Eligibility checks use local, explicit deployment configuration and never depend
on a report having data. The response is `Cache-Control: no-store`. When access
is disabled or no extension is authorized, omit the field. A missing field or
unknown protocol leaves the normal resource and browser handoff flows available.

The Agent may read a discovered `read_path` on the same verified origin with the
same native key. It validates the protocol and the fixed relative path grammar,
refuses redirects, and does not accept a caller-supplied actor, bot, group, or URL
selector. Browser cookies, OP keys, other instances' keys, and shared view links
do not authorize this route. A different dashboard host keeps its existing
explicitly authorized browser handoff. V1 does not forward a native key to that
host or expose a delegated cross-origin Agent credential.

The read response has `protocol`, `id`, `revision`, `status`, and `groups`.
Each group contains an opaque `id`, a short `label`, `status`, optional
`source_at` with an offset, and `allocations`. Each allocation contains
`kind` (`hold` or `reservation`), `node`, optional `device`, and optional
`expires_at` with an offset. The server projects these fields. Free-form notes,
raw reports, upstream URLs, execution instructions and credentials are excluded.
An unavailable or malformed report has `status: "unknown"`; an authorized
successful report with no allocations has `status: "ok"`. Stale sources are
marked per group. Unknown data may not be used as evidence that a resource is free.

The server authenticates the current native key, resolves the exact instance,
platform realm, subject, bot scope and binding, and checks the configured
extension grant on both discovery and read. Invalid credentials receive 401;
unknown and out-of-scope extension IDs both receive 404. Recheck the current key,
binding and grant after report generation. A stopped bot, disabled Agent access,
revocation or identity change cannot yield a report. Each request reads a single
extension. Use the native credential read budget with bounded concurrency,
timeouts and response sizes. A failed source stays isolated from native queries.

Administrators opt in each extension using reviewed local code and explicit
binding-to-reporter authorization. The adapter receives an immutable copy of the
verified identity metadata and no native bearer key. An existing browser-only
reporter is not automatically eligible. Opt-in adapters must document the
upstream identity verification and filter returned allocations to that subject.
Upstream credentials remain under deployment control. Extension metadata cannot
install packages, execute code or grant operational authority.

The public Skill and helper issue only the fixed optional read request, and can
show its output next to native and browser dashboard information. Resource
commands continue to require the native key for the target bot and do not accept
extension group/node identifiers. Removing an extension removes discovery and
makes its former read path unavailable.

## Package and update boundary

One package identity does not mean one remembered service URL or shared credential.
The installer/catalog owns package discovery; the private connection store owns
service identity and key references; authenticated responses own current rules.
A private registry is only another verified distribution channel for the same
package. Registry login and upstream/private-service login are separate authorities.

Today deployment-rendered Skill Markdown embeds an origin, so its revision can
differ across deployments. The helper prepared in [PR #161](https://github.com/dynamicheart/lockbot/pull/161)
also validates the local bundle; its delivery does not resolve the cross-service
revision boundary described here.
Before claiming install-once interoperability, separate portable package integrity
and helper compatibility from each service's rendered Skill/API revision and saved
origin. Preserve existing Markdown/ZIP URLs and update notices. A new service must
not silently overwrite the first connection, replace another service's key, or
ignore an incompatible revision to make a multi-service demo pass. This is a
remaining packaging/client acceptance decision, not completed behavior.

## Delivery checklist

- [x] Identify current provider/reporter/directory hooks and authority boundaries.
- [x] Document one Skill, multi-connection storage and read-only report semantics.
- [x] Define authenticated extension metadata and caller-scoped read contract.
- [ ] Define portable package vs per-service revision compatibility; retain old URLs.
- [ ] Implement optional extension discovery/read projection with bounded failure handling.
- [ ] Teach the existing Skill/helper supported extension protocols; no private adapter details.
- [ ] Verify optional private-registry installation delivers the same package identity/files.
- [ ] Test two native services, two bot keys, native plus read-only reporter, unknown data,
      revocation, absent extension, cross-origin rejection and no accidental writes.
- [ ] Run fresh-session install/discovery and authorized browser acceptance after contract review.

See [personal dashboard design](personal-resource-panel.md) and the
[continuity test plan](https://github.com/dynamicheart/lockbot/pull/163) in its
separate review PR. Neither design document is evidence of live extension discovery.
