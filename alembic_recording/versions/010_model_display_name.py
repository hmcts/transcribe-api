"""Add model_display_name to transcription_job.

model_display_name: the human-readable name of the Speech model that
  produced the transcript (DIAAT-243). Resolved server-side once a job
  succeeds by dereferencing the Azure `model.self` URL (held in
  model_identifier) via the Speech API, using the backend's existing Speech
  credentials. NULL for historical jobs, jobs whose model_identifier is a
  non-URL fallback label, or when resolution failed — the dashboard falls
  back to model_identifier in those cases. Never contains the subscription
  key: only the resolved, non-sensitive display fields are persisted.

Revision ID: 010
Revises: 009
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transcription_job",
        sa.Column("model_display_name", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcription_job", "model_display_name")
