"""Initial schema: caller and transcription_job tables.

Revision ID: 001
Revises:
Create Date: 2026-01-01 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "caller",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("name", sa.String, nullable=False, index=True),
        sa.Column("hashed_key", sa.String, nullable=False),
        sa.Column("webhook_secret", sa.String, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, default=True),
        sa.Column("azure_app_id", sa.String, nullable=True),
    )

    op.create_table(
        "transcription_job",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "caller_id", UUID(as_uuid=True), sa.ForeignKey("caller.id"), nullable=False, index=True
        ),
        sa.Column("status", sa.String, nullable=False, default="pending"),
        sa.Column("audio_url", sa.String, nullable=False),
        sa.Column("locale", sa.String, nullable=False, default="en-GB"),
        sa.Column("enable_diarization", sa.Boolean, nullable=False, default=True),
        sa.Column("callback_url", sa.String, nullable=True),
        sa.Column("idempotency_key", sa.String, nullable=True, index=True),
        sa.Column("metadata", JSONB, nullable=True),
        sa.Column("dialogue_entries", JSONB, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("batch_job_id", sa.String, nullable=True),
        sa.Column("batch_job_status", sa.String, nullable=True),
        sa.Column("batch_job_url", sa.String, nullable=True),
        sa.Column("audio_duration_seconds", sa.Float, nullable=True),
        sa.Column("audio_blob_path", sa.String, nullable=True),
        sa.Column("needs_cleanup", sa.Boolean, nullable=False, default=False),
        sa.Column("cleanup_failure_reason", sa.String, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("transcription_job")
    op.drop_table("caller")
