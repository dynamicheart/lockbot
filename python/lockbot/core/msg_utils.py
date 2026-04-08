"""Message formatting utilities (split long text, build replies, etc.)."""

import base64
import binascii
import hashlib

import six
from Crypto.Cipher import AES


def base64_urlsafe_decode(s):
    """Base64 decode with URL-safe character replacement and padding."""
    s = s.replace("-", "+").replace("_", "/") + "=" * (len(s) % 4)
    return base64.b64decode(s)


def check_signature(signature, rn, timestamp, TOKEN):
    """Verify request signature using MD5."""
    md5 = hashlib.md5()
    md5.update(f"{rn}{timestamp}{TOKEN}".encode())
    return md5.hexdigest() == signature


class AESCipher:
    """AES encryption/decryption helper."""

    def __init__(self, key, mode=AES.MODE_ECB, padding="PKCS7", encode="base64", **kwargs):
        """
        Args:
            key: AES key bytes.
            mode: AES mode (default ECB).
            padding: Padding scheme, PKCS7 or ZERO.
            encode: Output encoding: raw, base64, or hex.
        """
        self.key = key
        self.mode = mode
        self.padding = padding
        self.encode = encode
        self.kwargs = kwargs

        self.bs = AES.block_size

        self.IV = self.kwargs.get("IV", None)
        if self.IV and self.mode in (AES.MODE_ECB, AES.MODE_CTR):
            raise TypeError("ECB and CTR mode does not use IV")

    def _aes(self):
        return AES.new(self.key, self.mode, **self.kwargs)

    def encrypt(self, plaintext):
        """Encrypt plaintext and return encoded ciphertext."""
        # PKCS7 padding: https://en.wikipedia.org/wiki/Padding_(cryptography)#PKCS#5_and_PKCS#7
        if self.padding == "PKCS7":

            def pad(s):
                return s + (self.bs - len(s) % self.bs) * chr(self.bs - len(s) % self.bs).encode("utf-8")
        else:

            def pad(s):
                return s + (self.bs - len(s) % self.bs) * "\x00"

        if isinstance(plaintext, six.text_type):
            plaintext = plaintext.encode("utf-8")

        raw = self._aes().encrypt(pad(plaintext))

        if self.encode == "hex":
            return binascii.hexlify(raw)
        if self.encode == "base64":
            return base64.b64encode(raw)
        return raw

    def decrypt(self, ciphertext):
        """Decrypt ciphertext and return unpadded plaintext."""
        if not ciphertext:
            return None

        if self.padding == "PKCS7":
            if six.PY3:

                def unpad(s):
                    return s[0 : -s[-1]]
            else:

                def unpad(s):
                    return s[0 : -ord(s[-1])]
        else:

            def unpad(s):
                return s.rstrip("\x00")

        if isinstance(ciphertext, six.binary_type) and self.encode != "raw":
            ciphertext = ciphertext.decode("utf-8")
        if self.encode == "hex":
            ciphertext = binascii.unhexlify(ciphertext)
        if self.encode == "base64":
            ciphertext = base64_urlsafe_decode(ciphertext)

        return unpad(self._aes().decrypt(ciphertext))


def extract_msg_body(body):
    """Extract text/link content from message body, ignoring AT mentions."""
    rcv_info = ""
    for info in body:
        if info["type"] == "AT":
            pass
        elif info["type"] == "TEXT":
            rcv_info += info["content"]
        elif info["type"] == "LINK":
            rcv_info += info["label"]
        else:
            raise Exception("unknown message format" + str(info))
    return rcv_info
