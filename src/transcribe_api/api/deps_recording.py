from __future__ import annotations

import hmac
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session

from transcribe_api.runtime.security import (
    compute_key_lookup_hash,
    encrypt_webhook_secret,
    is_local_env,
    verify_api_key,
)
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.runtime.db_recording import get_session
from transcribe_api.domain.interface_recording import get_all_active_callers, get_caller_by_lookup_hash
from transcribe_api.domain.models_recording import Caller


def _local_dev_caller() -> Caller:
    return Caller(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        name="local-dev",
        hashed_key="",
        webhook_secret=encrypt_webhook_secret("local-webhook-secret"),
        is_active=True,
    )


async def get_caller(
    authorization: str = Header(...),
    session: Session = Depends(get_session),
) -> Caller:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization[7:]

    if is_local_env():
        if hmac.compare_digest(token, get_settings().LOCAL_API_KEY):
            # Upsert so the row exists as a FK target for SpeechBatchJob.
            caller = session.merge(_local_dev_caller())
            session.commit()
            return caller
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Fast path: O(1) indexed lookup by SHA-256 of the token, then one bcrypt verify.
    # Fallback to linear scan for legacy Caller rows that pre-date key_lookup_hash.
    lookup_hash = compute_key_lookup_hash(token)
    candidate = get_caller_by_lookup_hash(session, lookup_hash)
    if candidate is not None:
        if verify_api_key(token, candidate.hashed_key):
            return candidate
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Legacy fallback: rows created before key_lookup_hash was added.
    for caller in get_all_active_callers(session):
        if caller.key_lookup_hash is None and verify_api_key(token, caller.hashed_key):
            return caller

    raise HTTPException(status_code=401, detail="Invalid API key")
