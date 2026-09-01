"""Make caller_id nullable on transcription_job and correction_dataset_entry.

JWT-authenticated submissions (DIAAT-20) have no Caller row — caller_id is an
API-key concept. New jobs submitted via Azure AD JWT will have caller_id=NULL
and user_id set instead. Existing API-key-only jobs keep their caller_id.

Also adds a partial unique index on (user_id, idempotency_key) to enforce
idempotency for JWT callers, mirroring the existing
uq_transcription_job_caller_idempotency constraint for API-key callers.

Revision ID: 012
Revises: 011
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "transcription_job",
        "caller_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    # Idempotency for JWT-authenticated callers: enforce uniqueness on
    # (user_id, idempotency_key) when both are non-NULL, matching the behaviour
    # of uq_transcription_job_caller_idempotency for API-key callers.
    op.create_index(
        "ix_transcription_job_user_idempotency",
        "transcription_job",
        ["user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL AND idempotency_key IS NOT NULL"),
    )

    op.alter_column(
        "correction_dataset_entry",
        "caller_id",
        existing_type=sa.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    op.drop_index("ix_transcription_job_user_idempotency", table_name="transcription_job")

    # Restore NOT NULL — will fail if any NULL caller_id rows exist.
    op.alter_column(
        "transcription_job",
        "caller_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.alter_column(
        "correction_dataset_entry",
        "caller_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
