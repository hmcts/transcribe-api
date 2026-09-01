"""Reconcile transcription-run-metadata drift left by the duplicate-006 incident.

Background (DIAAT-225): DIAAT-227 (run metadata) and DIAAT-232 (GIN index) each
merged an Alembic migration numbered revision "006" within minutes of each other.
#46 linearised them by renumbering the GIN-index migration 006 -> 007. That is
correct for any database that had NOT yet applied either "006".

But the dev environment auto-deploys on every push to main, and had already
deployed DIAAT-232 (commit f9bfb34) *before* the linearisation. So dev:
  * ran the GIN-index migration under its OLD revision id "006", and
  * has alembic_version = "006", with the GIN index physically present, but
  * never got DIAAT-227's metadata columns (that migration didn't exist yet).

After #46, revision "006" now means the metadata migration. Alembic on dev
therefore believes "006" (metadata) is already applied and will never run it, so
`transcription_duration_seconds` / `model_identifier` stay missing and every
TranscriptionJob query 500s ("column ... does not exist"). It also tries to run
"007" (the GIN index) which already physically exists — handled by making 007
idempotent.

This migration closes the gap: it idempotently ensures the metadata columns (and
the GIN index) exist, regardless of how a given environment got here. It is a
no-op on any environment that already applied 006 + 007 normally (fresh installs,
local dev), and it repairs the drifted dev database.

Revision ID: 008
Revises: 007
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Column types match 006 (sa.Float -> double precision, sa.String -> varchar)
    # so IF NOT EXISTS cleanly skips on environments where 006 already added them.
    op.execute(
        "ALTER TABLE transcription_job "
        "ADD COLUMN IF NOT EXISTS transcription_duration_seconds double precision"
    )
    op.execute(
        "ALTER TABLE transcription_job ADD COLUMN IF NOT EXISTS model_identifier varchar"
    )
    # Belt-and-suspenders: ensure the GIN index exists too, for any environment
    # that reached this point without it.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_transcription_job_dialogue_entries_gin "
        "ON transcription_job USING gin (dialogue_entries)"
    )


def downgrade() -> None:
    # Reconciliation-only migration: its schema objects are owned by 006/007, so
    # downgrading a level should not drop them here (that would double-drop when
    # 006/007 themselves are downgraded). Intentionally a no-op.
    pass
