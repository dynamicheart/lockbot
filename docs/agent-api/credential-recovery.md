# Upgrade per-group credential discovery and recovery

Each group keeps its existing personal key. A valid key now also identifies its
verified IM principal for discovering and recovering that principal's other live
keys on the same service instance. Usernames supplied in a request cannot select
another identity. Feishu bindings are grouped only when their verified routes identify the same application and tenant; missing route or tenant evidence stays bot-scoped. Resource operations still require the destination group's key.

## Recovery and reset through chat

The user identifies the service when the Agent has no approved connection. The
user selects the robot by sending a one-time command in its existing group.
Group names, group IDs and bot IDs are never required as user inputs. The chat
event supplies the verified sender and bot. Keep service and platform identity
scopes independent, including private upstream providers.

| User intent | Agent and user steps | Expected result |
| --- | --- | --- |
| Use or migrate existing access | Find a saved connection or a privately supplied Key. Verify it, then recover eligible stored Keys on that instance. | Reuse the same Keys and save each verified connection. Skip chat confirmation when an existing Key already provides proof. |
| Find my Key or connect without a saved Key | Issue one recovery challenge on the approved service. Ask the user to send its code to the robot in the group they want to connect. | The Agent retrieves the original Key if it is recoverable. If no Key is active for the verified sender and bot, the server creates one and returns `outcome: created`. If an active Key has no readable copy, it stays valid. |
| Reset a lost or compromised Key | Explain that Agents sharing this Key will need an update. With explicit reset authorization, issue a reset challenge. The user sends its reset command in the robot's group. | Atomically invalidate the old Key and issue its replacement for the authenticated sender and bot. Retain other bindings, leases and bookings. |

A missing Key defaults to recovery. After the user explicitly authorizes reset, the
Agent can request a reset code. Both use `@bot bind <code>` in the chosen group.
Recovery codes start with `REC-` and reset codes with `RST-`. The purpose is
stored at issuance and the chat reply names the confirmed action.
A normal bind or recovery confirmation cannot authorize reset. Return credential
material only to the initiating Agent through proof of possession of the original
private challenge secret. Chat replies acknowledge the action without sending
the Key. The code and public challenge ID alone grant no API credential access.
Store the result privately and verify its live context before use. P800 renewal
continues to use its own issuer and shared credential.

Before exposing the proposed chat recovery and reset flows, verify the following:

- A fresh Agent with no local Key can recover the same value after chat proof.
  The existing Agent still authenticates. A missing ciphertext never rotates the
  Key, and the newly issued enrollment credential remains inert.
- With no active binding, the verified recovery confirmation creates one Key.
  Another recovery with the same sender and bot returns that Key.
- An explicitly confirmed reset invalidates the original credential, keeps the
  verified identity and resource state, and leaves another bot's Key valid.
- Expired, mismatched-purpose, replayed and concurrently used codes do not
  disclose values or reset credentials twice. Changing bot authority or revoking
  the selected binding before retrieval prevents credential delivery.
- A connection can resume after a new Agent session using its private challenge
  state. A lost or uncertain reset response requires fresh proof and state
  reconciliation before considering another reset.

On deployments advertising `chat_credential_challenges`, issue a code with
`key-challenge --origin <approved-origin> --authorized`. For a user-approved reset
add `--purpose reset`. After the user confirms, run `finish-enrollment --enrollment-dir <path>`.
The result is available through the issuing service until the code deadline.
The binding generation must still match. A service restart clears outstanding
codes and receipts. After an unavailable reset result, use fresh recovery proof
to retrieve the current Key before considering another reset.

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
   verified instance. When context advertises `credential_recovery_health`, inspect
   the caller-only field: `recoverable` means the current copy decrypts and matches
   this binding/digest, `legacy_missing` means no encrypted copy, and `unreadable`
   means a copy exists but cannot be verified (including an unavailable encryption
   key). None of these states revokes a valid key. For a missing/unreadable copy,
   POST the advertised `binding_recovery_url` once with that distinct saved key as
   Bearer authentication and no body. This backfills the presented key's exact value;
   it does not reconstruct another group's historical key from its digest.
5. Merge recovery results by instance, bot UUID and verified principal. Store each
   recovered key privately, validate its context and retain the per-group mapping.
   Do not print response credentials, overwrite unrelated services or consolidate
   different groups into one key. Repeating recovery is safe and does not rebind.
6. Find the `current: true` recovery entry, privately compare its key with the
   presented key, and verify context still identifies the same instance/bot/user and
   now reports `recoverable`. Keep the original key value and private-store reference;
   record only the health result, never either key value. With multiple saved bot
   keys, repeat independently for each missing/unreadable copy. A missing B copy does
   not invalidate A or let A's key authenticate to B's resource routes.

New confirmed bindings include an encrypted recovery copy. Existing digest-only
bindings remain fully usable, but a digest cannot reconstruct a missing key.
Until that key is presented by an existing Agent, its recovery result is null.
Preserve valid local keys when merging incomplete results; do not re-enroll merely
because another Agent has not yet backfilled its key. Unreadable recovery copies
also return null without preventing recovery of healthy groups.
If encryption cannot create a recovery copy, POST returns HTTP 503 with
`detail.code: credential_recovery_unavailable`; retain the local key/configuration,
continue its existing authorized usage, and report the recovery failure. Do not
loop, reset or silently change credentials. The health hint is absent on older
servers; use their capability/context flow without treating absence as an error.

## Exceptional revocation and re-enrollment

A digest is irreversible. If a key is genuinely unavailable from all authorized
local stores and recoverable copies, or disclosure/compromised authorization is
suspected, explain the supported human-confirmed flow before proposing revocation.
Routine releases, Skill updates, new Agent installation and missing recovery material
alone are not reasons to reset. Recovery preserves an active binding and can
create one when the verified sender and bot have none.

Ask for explicit user authorization identifying the service and explaining the affected scope. The user selects the bot through chat confirmation.
All Agents sharing the revoked group key lose access and need private configuration
with the replacement key. The verified human can use chat `bindings` and
`unbind <short-id>` in that bot's group to revoke their own binding, then confirm
a fresh `bind <code>`. The authorized owner can instead revoke all personal resource
bindings for the bot through the existing management flow; that broader action must
be explicitly identified. The chat reset challenge requires a verified chat confirmation. The `lbk_` operator API key has its own separate reset flow.

Credential revocation does not itself release, migrate or recreate existing locks
or queues. Explain those resource consequences separately and obtain authorization
for any resource mutation. A denied/failed revocation is not success: retain the
configuration, report the failure and verify authorization status before any new
enrollment. Never use another bot/user/service key to bypass the failure.

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
