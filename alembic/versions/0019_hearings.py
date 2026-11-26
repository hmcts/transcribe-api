"""hearing, and transcription_job.hearing_id and hearing_date: hearings recorded over several days (DIAAT-428).

A hearing is what a Judge sees as one recording; each of its days is a job of its own (one recording,
one transcript), ordered by hearing_date, then upload time. hearing holds the owner, the jurisdiction
and the case details (case_reference, tribunal) the days share.

Every existing job becomes a one-day hearing whose id is the job's, so the links to it and the audit
rows that name it still resolve, dated the day it was uploaded in London.

Additive only: hearing_id and hearing_date are nullable, so the previous release's pods keep writing
jobs while this one rolls out (the API treats a job with no hearing_id as a one-day hearing with its own
id, domain/hearings.py). A later migration backfills those and makes both columns required. The foreign
key is RESTRICT: a hearing is deleted once its last day is, never with its days still there. The
downgrade drops both columns and the table: the days of a multi-day hearing are then separate recordings.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-26
"""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

FOREIGN_KEY = "fk_transcription_job_hearing_id_hearing"
ORDER_INDEX = "ix_transcription_job_hearing_order"


def upgrade() -> None:
    op.create_table(
        "hearing",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("jurisdiction_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdiction.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_hearing_user_id"), "hearing", ["user_id"], unique=False)
    op.create_index(op.f("ix_hearing_jurisdiction_id"), "hearing", ["jurisdiction_id"], unique=False)

    with op.batch_alter_table("transcription_job") as batch:
        batch.add_column(sa.Column("hearing_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("hearing_date", sa.Date(), nullable=True))
        batch.create_foreign_key(FOREIGN_KEY, "hearing", ["hearing_id"], ["id"], ondelete="RESTRICT")
    op.create_index(ORDER_INDEX, "transcription_job", ["hearing_id", "hearing_date", "created_datetime"], unique=False)

    if op.get_context().dialect.name == "sqlite":
        _backfill_on_sqlite()
        return
    op.execute(
        """
        INSERT INTO hearing (id, created_datetime, updated_datetime, user_id, jurisdiction_id, metadata)
        SELECT id, created_datetime, updated_datetime, user_id, jurisdiction_id,
               jsonb_strip_nulls(jsonb_build_object(
                   'case_reference', metadata -> 'case_reference',
                   'tribunal', metadata -> 'tribunal'))
        FROM transcription_job
        WHERE hearing_id IS NULL
        """
    )
    # Only the jobs that got a hearing above: a job the previous release writes meanwhile keeps a NULL
    # hearing_id (a one-day hearing for the API) rather than naming a hearing that does not exist.
    op.execute(
        """
        UPDATE transcription_job AS job
        SET hearing_id = job.id,
            hearing_date = (job.created_datetime AT TIME ZONE 'Europe/London')::date
        FROM hearing
        WHERE hearing.id = job.id AND job.hearing_id IS NULL
        """
    )


def _backfill_on_sqlite() -> None:
    """The unit tests' SQLite: no time zones or JSONB functions in SQL, so done row by row."""
    connection = op.get_bind()
    london = ZoneInfo("Europe/London")
    rows = connection.execute(
        sa.text(
            "SELECT id, created_datetime, updated_datetime, user_id, jurisdiction_id, metadata"
            " FROM transcription_job WHERE hearing_id IS NULL"
        )
    ).all()
    for job_id, created, updated, user_id, jurisdiction_id, metadata in rows:
        details = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
        kept = {k: details[k] for k in ("case_reference", "tribunal") if details.get(k) is not None}
        moment = datetime.fromisoformat(str(created))
        moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
        connection.execute(
            sa.text(
                "INSERT INTO hearing (id, created_datetime, updated_datetime, user_id, jurisdiction_id, metadata)"
                " VALUES (:id, :created, :updated, :user_id, :jurisdiction_id, :metadata)"
            ),
            {
                "id": job_id,
                "created": created,
                "updated": updated,
                "user_id": user_id,
                "jurisdiction_id": jurisdiction_id,
                "metadata": json.dumps(kept),
            },
        )
        connection.execute(
            sa.text("UPDATE transcription_job SET hearing_id = :id, hearing_date = :day WHERE id = :id"),
            {"id": job_id, "day": moment.astimezone(london).date().isoformat()},
        )


def downgrade() -> None:
    op.drop_index(ORDER_INDEX, table_name="transcription_job")
    with op.batch_alter_table("transcription_job") as batch:
        batch.drop_constraint(FOREIGN_KEY, type_="foreignkey")
        batch.drop_column("hearing_date")
        batch.drop_column("hearing_id")
    op.drop_index(op.f("ix_hearing_jurisdiction_id"), table_name="hearing")
    op.drop_index(op.f("ix_hearing_user_id"), table_name="hearing")
    op.drop_table("hearing")
