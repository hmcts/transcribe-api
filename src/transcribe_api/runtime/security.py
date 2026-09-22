from __future__ import annotations

import hashlib

import bcrypt
from cryptography.fernet import Fernet

from transcribe_api.runtime.settings_recording import get_settings

# bcrypt hashes at most the first 72 bytes of input and bcrypt>=4 raises on
# anything longer, where passlib silently truncated. Truncating here keeps the
# behaviour passlib gave us, so hashes minted before this module dropped passlib
# still verify (both produce the same $2b$ format).
_BCRYPT_MAX_BYTES = 72


def _prepare(plain_key: str) -> bytes:
    return plain_key.encode()[:_BCRYPT_MAX_BYTES]


def compute_key_lookup_hash(plain_key: str) -> str:
    """SHA-256 of the raw key for indexed DB lookup before bcrypt verification."""
    return hashlib.sha256(plain_key.encode()).hexdigest()


def verify_api_key(plain_key: str, hashed_key: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(plain_key), hashed_key.encode())
    except ValueError:
        # Malformed or non-bcrypt stored hash: treat as a failed match rather
        # than a 500 on the authentication path.
        return False


def hash_api_key(plain_key: str) -> str:
    return bcrypt.hashpw(_prepare(plain_key), bcrypt.gensalt()).decode()


def is_local_env() -> bool:
    return get_settings().ENVIRONMENT.lower() == "local"


def encrypt_webhook_secret(plaintext: str) -> str:
    """Encrypt a webhook secret for storage in the database."""
    key = get_settings().WEBHOOK_SECRET_ENCRYPTION_KEY.encode()
    return Fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_webhook_secret(ciphertext: str) -> str:
    """Decrypt a stored webhook secret. Raises InvalidToken on key or data mismatch."""
    key = get_settings().WEBHOOK_SECRET_ENCRYPTION_KEY.encode()
    return Fernet(key).decrypt(ciphertext.encode()).decode()
