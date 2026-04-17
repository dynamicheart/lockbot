"""Generate ENCRYPTION_KEY and JWT_SECRET for platform deployment."""

from __future__ import annotations

import secrets

from cryptography.fernet import Fernet

enc_key = Fernet.generate_key().decode()
jwt_secret = secrets.token_hex(32)

print(f"ENCRYPTION_KEY={enc_key}")
print(f"JWT_SECRET={jwt_secret}")
