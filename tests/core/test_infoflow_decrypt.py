"""The Infoflow adapter must refuse any payload it cannot decrypt.

The callback signature is ``MD5(rn + timestamp + TOKEN)`` and covers none of the body
(see ``check_signature`` in ``msg_utils``). Successful AES decryption is therefore the only
evidence that the body actually came from the platform. If an undecryptable body were parsed
as plaintext JSON, anyone replaying a captured signature could hand-craft a body and choose
the sender identity, which the dispatch and binding layers above treat as trusted.

These tests pin the real :class:`InfoflowAdapter` (not a fake): a valid encrypted payload
still decrypts, and every undecryptable shape returns ``None`` with no plaintext fallback.
"""

import base64
import json

from lockbot.core.msg_utils import AESCipher, base64_urlsafe_decode
from lockbot.core.platforms.infoflow import InfoflowAdapter

AES_KEY_B64 = base64.b64encode(b"k" * 32).decode()


class _Config:
    """Minimal config exposing just the AESKEY the adapter reads."""

    def __init__(self, values):
        self._values = values

    def get_val(self, key, default=None):
        return self._values.get(key, default)


def _adapter():
    return InfoflowAdapter(_Config({"AESKEY": AES_KEY_B64}))


def _encrypt(payload):
    """Encrypt a dict exactly as the platform would, for the positive path."""
    key = base64_urlsafe_decode(AES_KEY_B64)
    return AESCipher(key).encrypt(json.dumps(payload))


def test_valid_encrypted_payload_decrypts():
    payload = {"message": {"header": {"fromuserid": "u1"}, "body": []}}
    assert _adapter().decrypt_payload(_encrypt(payload)) == payload


def test_plaintext_json_body_is_refused():
    """The replay attack: a correct signature but an unencrypted, attacker-chosen body."""
    forged = json.dumps({"message": {"header": {"fromuserid": "attacker"}, "body": []}})
    assert _adapter().decrypt_payload(forged) is None


def test_garbage_ciphertext_is_refused():
    assert _adapter().decrypt_payload(b"not-valid-ciphertext") is None


def test_ciphertext_under_a_different_key_is_refused():
    other = AESCipher(b"x" * 32).encrypt(json.dumps({"message": {}}))
    assert _adapter().decrypt_payload(other) is None


def test_empty_payload_is_refused():
    adapter = _adapter()
    assert adapter.decrypt_payload("") is None
    assert adapter.decrypt_payload(None) is None
