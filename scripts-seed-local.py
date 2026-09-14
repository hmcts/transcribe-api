#!/usr/bin/env python3
"""Seed the local database with a machine Caller so the frontend BFF can call
the recording surface.

The recording API's non-user-facing ingress authenticates with an API key held
as a `Caller` row (bcrypt hash plus an indexed SHA-256 lookup hash). Without one
the frontend's /api/jobs route handler fails with
"TRANSCRIPTION_API_KEY is not configured", which surfaces as a 502.

Prints the plaintext key to put in transcribe-web/.env.local.
Local development only.
"""
from __future__ import annotations

import os
import secrets
import sys

os.environ.setdefault("ENVIRONMENT", "local")

from sqlmodel import Session, create_engine, select  # noqa: E402

from transcribe_api.domain.models_recording import Caller  # noqa: E402
from transcribe_api.runtime.security import (  # noqa: E402
    compute_key_lookup_hash,
    encrypt_webhook_secret,
    hash_api_key,
)

NAME = "local-dev-frontend"


def main() -> int:
    url = os.environ.get(
        "DATABASE_CONNECTION_STRING",
        "postgresql://transcribe:transcribe@localhost:5432/transcribe",
    )
    engine = create_engine(url)
    with Session(engine) as session:
        existing = session.exec(select(Caller).where(Caller.name == NAME)).first()
        if existing:
            print(f"Caller '{NAME}' already exists (id={existing.id}).")
            print("Delete it first if you need a fresh key.")
            return 0

        plain_key = "loc_" + secrets.token_urlsafe(32)
        caller = Caller(
            name=NAME,
            hashed_key=hash_api_key(plain_key),
            key_lookup_hash=compute_key_lookup_hash(plain_key),
            webhook_secret=encrypt_webhook_secret(secrets.token_urlsafe(32)),
            is_active=True,
        )
        session.add(caller)
        session.commit()
        session.refresh(caller)

    print(f"Created Caller '{NAME}' (id={caller.id}).")
    print()
    print("Put this in transcribe-web/.env.local:")
    print(f"  TRANSCRIPTION_API_KEY={plain_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
