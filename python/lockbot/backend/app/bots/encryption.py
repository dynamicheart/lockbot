"""
Sensitive data encryption utilities.
"""

import logging

from cryptography.fernet import Fernet

from lockbot.backend.app.config import BASE_DIR, ENCRYPTION_KEY

logger = logging.getLogger(__name__)

_fernet = None

# Persistent key file for dev mode (when ENCRYPTION_KEY env var is not set)
_KEY_FILE = BASE_DIR / "data" / ".encryption_key"


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = ENCRYPTION_KEY
        if not key:
            # Try to load from persistent file first
            if _KEY_FILE.exists():
                key = _KEY_FILE.read_text().strip()
                logger.info("Loaded encryption key from %s", _KEY_FILE)
            else:
                # Auto-generate and persist for dev
                key = Fernet.generate_key().decode()
                _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                _KEY_FILE.write_text(key)
                logger.warning(
                    "Generated new encryption key and saved to %s. Set ENCRYPTION_KEY env var in production.",
                    _KEY_FILE,
                )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def mask(value: str, show_last: int = 4) -> str:
    """Mask display: ***xxxx"""
    if not value or len(value) <= show_last:
        return "***"
    return "***" + value[-show_last:]
