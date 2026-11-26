# Agent API identity tickets

`POST /api/agent/v2/identity-tickets` accepts a native Agent API v2 personal Key in the Authorization header and a JSON object with an `audience` string. An authenticated account can obtain a ticket for an administrator-registered audience. The successful response contains a Bearer `ticket` and its Unix `expires_at`.

The feature is enabled through the deployment environment variable `AGENT_IDENTITY_TICKETS_FILE`. Set it to an absolute path to a private JSON file with the following structure.

```json
{
  "version": 1,
  "key_file": "/private/lockbot/identity-signing.pem",
  "audiences": ["example-reports"]
}
```

Use a dedicated Ed25519 PKCS#8 PEM private key. Both the registry and key must be regular files with mode 0600, owned by the service account or root. Provision the public key, expected LockBot instance ID and expected audience to the relying service separately. Use an opaque audience identifier for a private service. Use a controlled HTTPS connection when exchanging credentials or tickets.

The relying service validates the `EdDSA` signature, `typ=lockbot-identity+jwt`, configured issuer and exact audience. Require `iss`, `sub`, `aud`, `iat` and `exp`. The issuer is the LockBot instance ID, the subject is the instance-local account identifier, and the lifetime is at most 120 seconds or the remaining lifetime of the originating credential. The key ID is the first 16 hex digits of SHA-256 of the 32-byte Ed25519 public key. Use `(iss, sub)` as the identity lookup key.

The relying service maintains its own permissions. An audience registration allows authenticated LockBot users to obtain an identity ticket for that audience. The relying service denies access unless the subject has its own applicable grant. The native personal Key stays with the issuing LockBot. A ticket issued before revocation can be used until it expires, so the relying service can revoke its own grant independently.

The registry is evaluated when issuing each ticket. Removing an audience prevents new issuance for it. To rotate a signing key, provision the new public key to the relying service before switching the issuer and allow the previous key only for the remaining lifetime of existing tickets.
