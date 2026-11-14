# Personal resource dashboard

A user can bookmark `/#/me` on their organization's Lockbot HTTPS origin. A headless Agent can authorize the first browser visit with the user's existing IM binding. Browser sessions last up to 30 days, including across restarts, subject to the continued validity of each binding. The dashboard only displays personal allocations and reservations.

## Identity and access

The issuer, platform, identity namespace and platform user ID identify an IM principal. The binding reference and bot scope determine what that principal may read. The backend revalidates each grant before returning data. A suspended bot, revocation or identity change invalidates the affected grant. A bot-scoped shared resource URL is insufficient for personal login.

The conservative namespace is deployment instance plus bot UUID. Only configure an attested realm across bots when you can verify they share the same platform identity namespace. For application-scoped IDs, this requires the corresponding application and tenant. Two platforms that happen to return the same string identify different principals. Separate bindings in an attested namespace can appear under one identity, with independent bot permissions.

An authenticated user can confirm another valid handoff and explicitly choose a combined view for this browser. The session keeps both grants with their own permissions. This does not create a durable account merger. Linking different IM principals to the same employee would require a separately verified directory mapping. A shared Agent machine must route credential use and private delivery based on an authenticated caller; a display name, prompt or shared service account does not establish that caller's personal identity.

## Enable

Use the existing Lockbot service origin that the Agent already trusts for credentials. The personal dashboard is a public Vue Router page at `/#/me` in that same process, with the management UI at `/` and the existing APIs at `/api`. It uses the shared Lockbot theme and has its own confirmed-IM session. A management account is not required to view personal allocations.

For HTTPS, terminate TLS on a trusted ingress, preserve the Host header, and restrict the backend's unencrypted listener to that ingress. Set `LOCKBOT_PERSONAL_PANEL_CONFIG` to a server-controlled JSON file, then start Lockbot normally. Keep the session database in a private, persistent directory. Example:

```json
{
  "origin": "https://lockbot.example.org",
  "state_file": "/data/personal-panel/sessions.sqlite3",
  "provider": {"issuer": "https://lockbot.example.org"},
  "reporters": []
}
```

Use the approved canonical service origin as the native provider issuer, as in the example, so connection cards can link to that service’s Agent Skill. Existing issuer identifiers remain valid; a non-URL identifier does not produce a Skill link.

The original application mounts the optional dashboard before its SPA fallback. Without configuration the original route setup is preserved. To disable, unset the environment variable and restart the service. To invalidate all browser sessions permanently, retire the dedicated session database while the service is stopped. This database stores digests of random browser tokens and references to confirmed bindings. IM authentication and the original group credentials stay under the existing Lockbot authority.

For a private intranet HTTP deployment, set `allow_private_http: true` and configure a literal RFC 1918, loopback or ULA IP origin using `http://`. This explicit mode uses a host-only HttpOnly SameSite=Strict cookie scoped to `/api/personal-panel`, without Secure or the `__Host-` prefix. Public HTTP IPs and HTTP hostnames are rejected. The Agent must already trust that exact HTTP origin and observe `transport: private-http` in capabilities. Origin checks, binding validation, ticket expiry, session rotation and logout remain enforced.

## Discovery in IM

When the panel and a bot's Agent access are enabled, explicit `help` replies include the fixed personal-resource link and first-visit instructions. With the existing Agent promotion setting enabled, successful `lock` replies also include a short “My resources” link alongside the existing Skill promotion. The existing promotion toggle controls both success-reply links. Group replies never mint or include a personal handoff ticket. The browser's fixed entrance reuses its session, and a new device obtains its first handoff privately from the user's bound Agent.

## Handoff

The Agent reads `GET /api/personal-panel/capabilities` from the trusted origin. A return visit only needs the fixed home. For first login, device recovery, or adding another binding, the Agent calls `POST /api/personal-panel/links` with its existing Bearer binding credential at the same trusted origin. The recipient must already have a verified private delivery channel. The handoff URL is itself a short-lived bearer secret; avoid group chats, unfurlers, ticket logs and external redirects.

The generated ticket expires in two minutes. It is placed in the URL fragment, removed from browser history by the landing page, previewed without consumption, and exchanged only on explicit confirmation. Consumption is atomic and rotates the browser session identifier. The browser receives a Secure, HttpOnly, SameSite=Strict host-only cookie. It can return by bookmark without contacting the Agent. Logout revokes the server-side session. A cleared browser cookie requires a fresh handoff.

## Optional identity provider

The default provider uses native confirmed bindings. An operator may instead select a reviewed local module with `"provider":{"file":"/opt/plugins/provider.py","config":{}}`. Its `create_provider(config)` factory must implement `authenticate(credential)`, `validate(grant)` and `report(grant)`. The provider must establish identity from its authoritative issuer, bind each grant to that issuer and scope, and revalidate revocation on protected reads. A matching display name does not establish identity. Upstream credentials belong in an operator-controlled secret store and are never returned to the browser. The module executes with the service's privileges and must be explicitly installed and reviewed.

## Optional reporters

Administrators may install a trusted local Python module exporting `create_reporter(config)` and add an entry of the form `{"file":"/opt/plugins/example.py","config":{}}` under `reporters`. Modules run with the service's privileges. Only reviewed, administrator-controlled local files belong here.

A reporter receives verified grant fields through `report(grant)`. It should pin the issuer, platform and realm it is authorized to interpret, return an empty list for unrelated identities, and use its own configured resource service. It must filter records by exact verified subject and return only that subject's allocations. Each group in its result supplies a stable group_id, label, status and allocations. Mark unavailable, incomplete or stale groups unknown. Browser inputs do not select plugins or remote request destinations.

## Optional enterprise identity recognition

An operator can install a trusted directory resolver to label already-authorized IM principals that belong to the same employee. For example, configure `"identity_resolver":{"file":"/opt/plugins/directory.py","config":{"directory":"corporate-idp"}}` to pin the trusted directory. The module's `create_resolver(config)` returns an object with `resolve(identity)`. It receives the verified IM issuer, platform, realm and subject. The resolver must authenticate its directory source and require a unique, current mapping to an immutable corporate subject. Ambiguous or missing matches return None. Never infer a match from a display name or email prefix.

The panel accepts a fresh result with directory, subject and expires_at from the pinned directory. It returns only same-person index groups for accounts already present in this browser. The mapping does not discover new accounts, add authorizations, or unlock more resources. The employee identifier stays on the server. The operator supplies the actual enterprise directory integration separately.

## Optional metrics adapters

Metrics are separate from allocation reporters. The [vendor-neutral metrics datasource protocol v1](metrics-datasource-protocol.md) defines the proposed normalized snapshot and adapter boundary for Prometheus, HTTP JSON and other monitored sources. Reporters continue to return verified personal allocations; metrics never grant resource access or drive lock mutations. The existing Prometheus integration is available on resource views; personal-panel metric attachment and additional adapters remain follow-up implementation, not capabilities enabled by this document.
