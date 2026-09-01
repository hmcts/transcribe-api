"""Re-seed the frontend BFF service Caller after secret rotation.

Migration 004 seeds the `frontend-service` Caller from FRONTEND_SERVICE_API_KEY,
but Alembic runs each revision exactly once, so 004 never re-runs. When the
frontend-api-key / webhook-signing-secret Key Vault secrets moved to being
Terraform-generated (DIAAT-20), any environment that had already run 004 kept a
`caller.hashed_key` derived from the OLD key — so the frontend's rotated key no
longer authenticated (401), and `caller.webhook_secret` was left encrypted under
the OLD Fernet key.

This revision re-runs the upsert once against the CURRENT env values, realigning:
- `hashed_key` / `key_lookup_hash` with the current FRONTEND_SERVICE_API_KEY, and
- `webhook_secret` (re-encrypted under the current WEBHOOK_SECRET_ENCRYPTION_KEY).

Idempotent and safe on every environment: greenfield databases already seeded by
004 with the generated values simply get the same values written again; no-op if
FRONTEND_SERVICE_API_KEY is unset (local/CI).

Revision ID: 013
Revises: 012
Create Date: 2026-08-21
"""

from __future__ import annotations

import os
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

_CALLER_NAME = "frontend-service"


# Environments where the frontend service is expected to exist, so a missing
# FRONTEND_SERVICE_API_KEY is a misconfiguration rather than a legitimate absence.
_DEPLOYED_ENVIRONMENTS = ("dev", "stg", "prod")


def upgrade() -> None:
    api_key = os.environ.get("FRONTEND_SERVICE_API_KEY")
    if not api_key:
        environment = os.environ.get("ENVIRONMENT", "local").lower()
        if environment in _DEPLOYED_ENVIRONMENTS:
            # Failing here (rather than returning) keeps Alembic from recording
            # 013 as applied, so the re-seed still runs on a later deploy once the
            # key is available. A silent return would stamp the revision and the
            # caller would stay stranded on the old hash forever.
            raise RuntimeError(
                "FRONTEND_SERVICE_API_KEY is not set but ENVIRONMENT="
                f"{environment!r}; refusing to record migration 013 as applied "
                "without re-seeding the frontend-service caller. Fix the Key "
                "Vault reference / app setting and re-deploy."
            )
        # local / test / CI: no frontend service is configured — nothing to seed.
        return

    from transcription_svc.auth.validators import (
        compute_key_lookup_hash,
        encrypt_webhook_secret,
        hash_api_key,
    )

    hashed_key = hash_api_key(api_key)
    key_lookup_hash = compute_key_lookup_hash(api_key)
    # Re-encrypt under the current Fernet key so the row is decryptable after a
    # WEBHOOK_SECRET_ENCRYPTION_KEY rotation. The frontend never receives webhook
    # callbacks; this placeholder exists only to satisfy the NOT NULL column.
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
            .values(
                hashed_key=hashed_key,
                key_lookup_hash=key_lookup_hash,
                webhook_secret=webhook_secret,
                is_active=True,
            )
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
    # No-op: this revision only realigns the frontend-service Caller's secrets
    # with the current environment; there is no meaningful state to roll back.
    pass
