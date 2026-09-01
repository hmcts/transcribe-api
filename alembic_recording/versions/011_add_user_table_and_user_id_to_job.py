"""Add user table and user_id FK on transcription_job.

Adds:
- `user` table: Azure AD-backed user identity with id, azure_user_id, email,
  role, created_datetime, updated_datetime.
- `user_id` nullable UUID FK column on `transcription_job` referencing `user.id`.
  NULL for rows created before the auth migration (API-key-only callers).

Revision ID: 011
Revises: 010
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("azure_user_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("created_datetime", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_datetime", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_azure_user_id"), "user", ["azure_user_id"], unique=True)
    op.create_index(op.f("ix_user_email"), "user", ["email"], unique=False)

    op.add_column(
        "transcription_job",
        sa.Column("user_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_transcription_job_user_id_user",
        "transcription_job",
        "user",
        ["user_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_transcription_job_user_id"),
        "transcription_job",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_transcription_job_user_id"), table_name="transcription_job")
    op.drop_constraint("fk_transcription_job_user_id_user", "transcription_job", type_="foreignkey")
    op.drop_column("transcription_job", "user_id")

    op.drop_index(op.f("ix_user_email"), table_name="user")
    op.drop_index(op.f("ix_user_azure_user_id"), table_name="user")
    op.drop_table("user")
