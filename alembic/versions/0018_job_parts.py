"""transcription_job_part: the parts of a recording longer than 240 minutes (DIAAT-408).

Azure batch transcription with diarization fails a file over 240 minutes, so the worker splits a
longer recording into parts (stt/splitter.py) and submits each as its own Azure job. One row per
part records where the part starts in the recording (offset_seconds, measured from the cut files,
the amount later added to its transcript's times), its length, how its start was chosen (cut_kind:
start, silence or forced), the blob it is stored in, its Azure job and status, and how many times
it was submitted. The job stays one row of transcription_job: jobs of 240 minutes or less have no
part rows and keep the single-file path.

New table only: the previous release's pods do not know it and keep working while this one rolls
out. The rows go with their job (ON DELETE CASCADE). The downgrade drops the table: a recording
being prepared in parts then loses its record of which parts were submitted (turn the flags off and
let the worker drain first).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-20
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcription_job_part",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("offset_seconds", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("cut_kind", sa.String(), nullable=False),
        sa.Column("blob_path", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("batch_job_id", sa.String(), nullable=True),
        sa.Column("batch_job_url", sa.String(), nullable=True),
        sa.Column("batch_job_status", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["transcription_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "part_index", name="uq_transcription_job_part_index"),
    )
    op.create_index(op.f("ix_transcription_job_part_job_id"), "transcription_job_part", ["job_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_transcription_job_part_job_id"), table_name="transcription_job_part")
    op.drop_table("transcription_job_part")
