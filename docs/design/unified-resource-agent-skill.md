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

## Proposed authenticated extension discovery

After native service discovery and key verification, the same Skill should learn
which reviewed extensions this deployment allows **this principal** to use. Additive
metadata must describe stable extension ID, protocol/revision, readable operations,
source label, credential mode and approved same-origin read links. Do not publish
private endpoints, secrets, implementation paths or unrelated users' resources in
the unauthenticated manifest. Unsupported/absent discovery retains today's native
workflow; clients must not guess routes, scrape admin configuration or probe plugins.

No discovery field name, endpoint or wire schema is implemented by this document.
Specify and review that contract before coding it. Plugin installation remains an
administrator action using reviewed local files; API metadata must never cause an
Agent or server to import arbitrary remote Python or execute upstream content.

An Agent read bridge, if added, must authenticate and revalidate the current caller,
project only that caller's allocations, bound time/size/fan-out, and preserve unknown,
stale and unavailable states. Reuse reporter filtering logic without reusing browser
cookies as Agent credentials. A reporter cannot mint or widen identity grants. Use
deployment-owned upstream secrets inside the adapter; return normalized records,
not upstream keys. If an adapter requires a separate user credential, require an
explicit, separately scoped private reference and verified destination contract.
It must never receive the native `lbr_` key merely because it contributes a report.

Discover `read-only` explicitly. Do not advertise native command links for reporter
resources or translate reported names into bot IDs. Write-capable adapters are out
of scope until their own authentication, authority and operation semantics are designed.
Native locks, existing APIs/keys, IM messages and browser sessions must retain their
current behavior when no extension is installed or an extension fails.

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
- [ ] Define authenticated extension metadata and caller-scoped read contract.
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
