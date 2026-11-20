"""transcription_job.source_blob_path and preparation_attempts: uploads prepared by the worker (DIAAT-408).

With the system flag ``async_upload_preparation`` on, POST /uploads stores the original file and
answers at once, and POST /jobs creates the job PENDING without submitting it: the worker converts
the original and submits the job (stt/preparation_service.py).

source_blob_path: the blob the original waits in (incoming/{user_id}/{upload_id}{ext}) until the
worker has converted it. NULL for every job that was submitted by its request, as every job has been.

preparation_attempts: how many times the worker has started preparing the job; 0 until it does.
Not null, default 0, so a job row written by the previous release (which does not know the column)
is valid and is never claimed (it has no source_blob_path).

Additive only: the previous release's pods keep working while this one rolls out. The downgrade
drops both columns (a job still waiting for preparation then loses its pointer to the original:
turn the flag off and let the worker drain first).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-20
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transcription_job", sa.Column("source_blob_path", sa.String(), nullable=True))
    op.add_column(
        "transcription_job",
        sa.Column("preparation_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("transcription_job", "preparation_attempts")
    op.drop_column("transcription_job", "source_blob_path")
