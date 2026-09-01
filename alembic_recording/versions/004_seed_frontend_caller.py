"""Seed a Caller row for the frontend BFF service.

The frontend never talks to Azure Speech/Storage directly — it proxies
requests through the backend API on behalf of end users, authenticating
as its own Caller (see DIAAT-217). This migration upserts that Caller
from the FRONTEND_SERVICE_API_KEY env var so the key can be rotated by
changing the Key Vault secret and redeploying.

Idempotent and safe to re-run on every deploy. No-op if the env var is
unset — local/CI environments that haven't configured a frontend service
key yet are unaffected.

Revision ID: 004
Revises: 003
Create Date: 2026-07-13
"""

from __future__ import annotations

import os
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None

_CALLER_NAME = "frontend-service"


def upgrade() -> None:
    api_key = os.environ.get("FRONTEND_SERVICE_API_KEY")
    if not api_key:
        return

    from transcription_svc.auth.validators import (
        compute_key_lookup_hash,
        encrypt_webhook_secret,
        hash_api_key,
    )

    hashed_key = hash_api_key(api_key)
    key_lookup_hash = compute_key_lookup_hash(api_key)
    # The frontend never receives webhook callbacks; the column is NOT NULL,
    # so store an encrypted placeholder it will never use.
    webhook_secret = encrypt_webhook_secret("frontend-service-does-not-receive-webhooks")

    caller = sa.table(
        "caller",
        sa.column("id", PGUUID(as_uuid=True)),
        sa.column("created_datetime", sa.DateTime(timezone=True)),
        sa.column("name", sa.String),
        sa.column("hashed_key", sa.String),
        sa.column("key_lookup_hash", sa.String),
        sa.column("webhook_secret", sa.String),
        sa.column("is_active", sa.Boolean),
    )

    bind = op.get_bind()
    existing = bind.execute(sa.select(caller.c.id).where(caller.c.name == _CALLER_NAME)).first()

    if existing:
        bind.execute(
            caller.update()
            .where(caller.c.name == _CALLER_NAME)
            .values(hashed_key=hashed_key, key_lookup_hash=key_lookup_hash, is_active=True)
        )
    else:
        bind.execute(
            caller.insert().values(
                id=uuid4(),
                created_datetime=sa.func.now(),
                name=_CALLER_NAME,
                hashed_key=hashed_key,
                key_lookup_hash=key_lookup_hash,
                webhook_secret=webhook_secret,
                is_active=True,
            )
        )


def downgrade() -> None:
    # Deactivate rather than delete: once the frontend has submitted any
    # jobs, transcription_job.caller_id's FK constraint would reject a hard
    # delete of this row. Deactivating has the same practical effect (auth
    # only accepts active callers) without that failure mode.
    caller = sa.table("caller", sa.column("name", sa.String), sa.column("is_active", sa.Boolean))
    op.get_bind().execute(
        caller.update().where(caller.c.name == _CALLER_NAME).values(is_active=False)
    )
