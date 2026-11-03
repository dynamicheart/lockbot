# Personal resource dashboard

A user can bookmark `/me/` on their organization's Lockbot HTTPS origin. A headless Agent can authorize the first browser visit with the user's existing IM binding. Browser sessions last up to 30 days, including across restarts, subject to the continued validity of each binding. The dashboard only displays personal allocations and reservations.

## Identity and access

The issuer, platform, identity namespace and platform user ID identify an IM principal. The binding reference and bot scope determine what that principal may read. The backend revalidates each grant before returning data. A suspended bot, revocation or identity change invalidates the affected grant. A bot-scoped shared resource URL is insufficient for personal login.

The conservative namespace is deployment instance plus bot UUID. Only configure an attested realm across bots when you can verify they share the same platform identity namespace. For application-scoped IDs, this requires the corresponding application and tenant. Two platforms that happen to return the same string identify different principals. Separate bindings in an attested namespace can appear under one identity, with independent bot permissions.

An authenticated user can confirm another valid handoff and explicitly choose a combined view for this browser. The session keeps both grants with their own permissions. This does not create a durable account merger. Linking different IM principals to the same employee would require a separately verified directory mapping. A shared Agent machine must route credential use and private delivery based on an authenticated caller; a display name, prompt or shared service account does not establish that caller's personal identity.

## Enable

Configure a dedicated HTTPS origin that the Agent already trusts for Lockbot credentials. Terminate TLS on a trusted ingress, preserve the Host header, and restrict the backend's unencrypted listener to that ingress. Set `LOCKBOT_PERSONAL_PANEL_CONFIG` to a server-controlled JSON file, then start Lockbot normally. Keep the session database in a private, persistent directory. Example:

```json
{
  "origin": "https://lockbot.example.org",
  "state_file": "/data/personal-panel/sessions.sqlite3",
  "provider": {"issuer": "production-lockbot"},
  "reporters": []
}
```

The original application mounts the optional dashboard before its SPA fallback. Without configuration the original route setup is preserved. To disable, unset the environment variable and restart the service. To invalidate all browser sessions permanently, retire the dedicated session database while the service is stopped. This database stores digests of random browser tokens and references to confirmed bindings. IM authentication and the original group credentials stay under the existing Lockbot authority.

## Handoff

The Agent reads `GET /me/capabilities` from the trusted origin. A return visit only needs the fixed home. For first login, device recovery, or adding another binding, the Agent calls `POST /me/links` with its existing Bearer binding credential at the same trusted origin. The recipient must already have a verified private delivery channel. The handoff URL is itself a short-lived bearer secret; avoid group chats, unfurlers, ticket logs and external redirects.

The generated ticket expires in two minutes. It is placed in the URL fragment, removed from browser history by the landing page, previewed without consumption, and exchanged only on explicit confirmation. Consumption is atomic and rotates the browser session identifier. The browser receives a Secure, HttpOnly, SameSite=Strict host-only cookie. It can return by bookmark without contacting the Agent. Logout revokes the server-side session. A cleared browser cookie requires a fresh handoff.

## Optional reporters

Administrators may install a trusted local Python module exporting `create_reporter(config)` and add an entry of the form `{"file":"/opt/plugins/example.py","config":{}}` under `reporters`. Modules run with the service's privileges. Only reviewed, administrator-controlled local files belong here.

A reporter receives verified grant fields through `report(grant)`. It should pin the issuer, platform and realm it is authorized to interpret, return an empty list for unrelated identities, and use its own configured resource service. It must filter records by exact verified subject and return only that subject's allocations. Each group in its result supplies a stable group_id, label, status and allocations. Mark unavailable, incomplete or stale groups unknown. Browser inputs do not select plugins or remote request destinations.
