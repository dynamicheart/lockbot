"""
Encryption module tests.
"""

from lockbot.backend.app.bots.encryption import decrypt, encrypt, mask


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        original = "https://example.com/webhook?key=secret123"
        encrypted = encrypt(original)
        assert encrypted != original
        assert decrypt(encrypted) == original

    def test_empty_string(self):
        assert encrypt("") == ""
        assert decrypt("") == ""

    def test_mask_long(self):
        assert mask("abcdefghijk") == "***hijk"

    def test_mask_short(self):
        assert mask("ab") == "***"

    def test_mask_empty(self):
        assert mask("") == "***"
