from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet
from passlib.context import CryptContext

from transcribe_api.runtime.settings_recording import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def compute_key_lookup_hash(plain_key: str) -> str:
    """SHA-256 of the raw key for indexed DB lookup before bcrypt verification."""
    return hashlib.sha256(plain_key.encode()).hexdigest()


def verify_api_key(plain_key: str, hashed_key: str) -> bool:
    return _pwd_context.verify(plain_key, hashed_key)


def hash_api_key(plain_key: str) -> str:
    return _pwd_context.hash(plain_key)


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
