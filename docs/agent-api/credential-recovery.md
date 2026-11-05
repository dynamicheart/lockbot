# Upgrade per-group credential discovery and recovery

Each group keeps its existing personal key. A valid key now also identifies its
verified IM principal for discovering and recovering that principal's other live
keys on the same service instance. Usernames supplied in a request cannot select
another identity. Feishu bindings are grouped only when their verified routes identify the same application and tenant; missing route or tenant evidence stays bot-scoped. Resource operations still require the destination group's key.

## Credential authority

A holder of one live personal key can recover the same principal's stored keys
inside the verified service/platform identity namespace. This is credential
distribution, not only a metadata directory: do not share a personal key with an
Agent that should not receive the user's other keys in that namespace. Resource
operations still validate the destination bot's individual key. If a personal
key is compromised, review and revoke all affected bindings rather than assuming
revoking only the initial bot key removes copies already recovered.

## Upgrade order

1. Upgrade the server first. Keep the original database, state directory, instance
   identity and encryption key. Startup adds a nullable `credential_encrypted`
   column; existing digests, keys, bot UUIDs and browser sessions are retained.
   No key rotation, re-enrollment or browser login is required by this migration.
2. Verify that existing keys still resolve their original `/api/agent/context`
   and that resource routes reject keys belonging to a different bot. Check the
   manifest for `own_binding_directory` and `own_binding_recovery`.
3. Update the downloaded `lockbot-resource-agent` Skill and reload it in each
   Agent environment. Use the manifest's bundle URL and hashes. Existing clients
   remain functional; a server update cannot force an old client to reload.
4. In each Agent's private store, enumerate existing keys by approved origin and
   verified instance. POST the advertised `binding_recovery_url` with each
   distinct existing key as Bearer authentication and no body. This backfills
   historical keys as they are presented, retaining their original value.
5. Merge recovery results by instance, bot UUID and verified principal. Store each
   recovered key privately, validate its context and retain the per-group mapping.
   Do not print response credentials, overwrite unrelated services or consolidate
   different groups into one key. Repeating recovery is safe and does not rebind.

New confirmed bindings include an encrypted recovery copy. Existing digest-only
bindings remain fully usable, but a digest cannot reconstruct a missing key.
Until that key is presented by an existing Agent, its recovery result is null.
Preserve valid local keys when merging incomplete results; do not re-enroll merely
because another Agent has not yet backfilled its key. Unreadable recovery copies
also return null without preventing recovery of healthy groups.

GET `bindings_url` returns metadata only. POST `binding_recovery_url` returns
credentials, with `Cache-Control: no-store` and `Vary: Authorization`. Pending,
revoked, expired, deleted and replaced-identity bindings are excluded. A disabled
bot's binding is discoverable with `agent_access_enabled: false`; recovery cannot
enable the bot or extend a key's lifetime.

## Mixed versions and rollback

New Skills inspect capabilities before calling new endpoints. Older servers keep
their saved-key/context flow; do not probe bot IDs or rebind to compensate for a
missing capability. If an advertised endpoint unexpectedly returns 404/405, retain
existing configuration and report a deployment/version mismatch before retrying
recovery. Do not erase local connections after an incomplete or failed response.

The schema addition is nullable and can be left in place if application code is
rolled back. Keep the original encryption key for subsequent upgrades and backups;
old clients continue using existing credentials. Follow the deployment's normal
state compatibility checks before rolling back application code.

Cross-service or cross-protocol connections remain separate. An aggregated
dashboard must distinguish its login identity, each report's origin and the
credential that authorizes each group; sharing a displayed username is not a
cross-service identity proof.
