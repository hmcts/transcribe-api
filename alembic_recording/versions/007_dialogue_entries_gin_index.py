"""Add a GIN index on transcription_job.dialogue_entries.

DIAAT-232: dialogue_entries is stored schemaless as JSONB (see 001), so no
column addition is needed to start persisting the new per-phrase
`alternatives` field (Azure's nBest array) added to the DialogueEntry model
in this same change — it's just another key inside the existing JSON blob,
and every existing row is unaffected (NULL/absent reads back as Python
`None` via the model's default).

What genuinely does need a migration is indexing: each entry's JSON payload
is now meaningfully larger (a full nBest array per phrase instead of just
the top choice), so a GIN index is added ahead of DIAAT-233/234 needing to
query into it (e.g. "jobs with a low-confidence word that has a
higher-confidence alternative") without a full-table JSON scan.

Revision ID: 007
Revises: 006
Create Date: 2026-07-15
"""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: environments that deployed DIAAT-232 *before* the duplicate-006
    # revisions were linearised (#46) already created this index under the old
    # revision id "006". After the renumber their alembic_version still reads
    # "006", so `alembic upgrade head` re-runs this (now "007") step against a DB
    # that already has the index. A plain CREATE INDEX would raise
    # "relation already exists" and crash the container on startup, so guard with
    # IF NOT EXISTS. On a fresh database this behaves exactly like create_index.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_transcription_job_dialogue_entries_gin "
        "ON transcription_job USING gin (dialogue_entries)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transcription_job_dialogue_entries_gin")
