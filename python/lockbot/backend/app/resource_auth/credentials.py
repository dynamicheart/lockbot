"""Credential and bind-code generation, digesting and comparison.

Two secrets are issued by one binding-creation call:

- the **credential**, which the agent keeps and sends as a Bearer token. It is inert until
  the binding is confirmed in chat.
- the **bind code**, which the human pastes into the bot's IM group as ``bind <code>``. It
  is public by nature, short enough to type, and single-use.

Authentication uses digests; optional recovery copies are encrypted with the service's
persistent encryption key and pinned to the binding identifier.
"""

import hashlib
import hmac
import json
import secrets

#: Distinguishes a personal resource credential from the legacy per-bot service key
#: (``lbk_``), so the two principal types cannot be confused by inspection.
CREDENTIAL_PREFIX = "lbr_"

#: Unambiguous when typed or read aloud: no 0/O, 1/I/L, 8/B.
_BIND_CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY2345679"
_BIND_CODE_LENGTH = 8


def generate_credential() -> str:
    """Return a fresh opaque per-bot credential."""
    return CREDENTIAL_PREFIX + secrets.token_urlsafe(32)


def looks_like_credential(token: str) -> bool:
    """Cheap shape check used to route a Bearer token to the right principal type."""
    return bool(token) and token.startswith(CREDENTIAL_PREFIX)


def digest_credential(credential: str) -> str:
    """Return the hex sha256 digest stored in place of the credential."""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def credential_matches(stored_digest: str, candidate: str) -> bool:
    """Constant-time comparison of a presented credential against a stored digest."""
    if not stored_digest or not candidate:
        return False
    return hmac.compare_digest(stored_digest, digest_credential(candidate))


def generate_bind_code() -> str:
    """Return a fresh short bind code for a human to type into chat."""
    return "".join(secrets.choice(_BIND_CODE_ALPHABET) for _ in range(_BIND_CODE_LENGTH))


def normalize_bind_code(code: str) -> str:
    """Normalize a bind code as typed, so casing and padding do not matter."""
    return (code or "").strip().upper()


def digest_bind_code(code: str) -> str:
    """Return the hex sha256 digest of a normalized bind code."""
    return hashlib.sha256(normalize_bind_code(code).encode("utf-8")).hexdigest()


def generate_public_id() -> str:
    """Return an unguessable public identifier for a binding."""
    return secrets.token_urlsafe(16)


def seal_credential(binding_id: str, credential: str) -> str:
    from lockbot.backend.app.bots.encryption import encrypt

    return encrypt(json.dumps({"binding_id": binding_id, "credential": credential}))


def recover_credential(binding) -> str | None:
    from lockbot.backend.app.bots.encryption import decrypt

    if not binding.credential_encrypted:
        return None
    try:
        data = json.loads(decrypt(binding.credential_encrypted))
        token = data["credential"]
        if data["binding_id"] != binding.public_id or not credential_matches(binding.credential_digest, token):
            return None
        return token
    except Exception:
        return None


def short_id(public_id: str) -> str:
    """Return the safe short handle used in chat for `unbind <id>`.

    Short enough to retype, long enough that it does not collide within one sender's
    own bindings, and derived from the public id rather than the row id.
    """
    return public_id[:8]
