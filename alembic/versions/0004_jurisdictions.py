"""Jurisdictions: membership, settings, and the jurisdiction of every record (DIAAT-322).

BenchNotes was built for the IAC alone. This lets any number of jurisdictions share
it (domain/jurisdictions.py):

- jurisdiction: one row per jurisdiction, with its screen wording and what it uses
  (settings JSONB), so adding one needs no deployment. The IAC is created here, with
  the wording the web app has shown until now, its decision template and hearing
  recordings, and auto_enrol so new users still get in as before.
- jurisdiction_membership: a user's role (judge, admin) in a jurisdiction, or a
  pending one by email for someone who has not signed in yet.
- user.active_jurisdiction_id: the jurisdiction last chosen.
- upload_jurisdiction: the jurisdiction an upload was started in, for the poller
  that makes its transcription later.
- jurisdiction_id on the records that hold judicial material: transcription (its
  minute versions, jobs and tags hang off it), live_transcript_draft (one draft per
  user per jurisdiction: a unique key on both) and transcription_job (recording).

Every existing user is made an IAC judge, and every existing record an IAC one,
recording jobs of API-key callers included (BenchNotes served the IAC alone). Small
tables: the backfill takes brief locks.

Additive only, so pods still running the previous release keep working while the
new one rolls out: the draft's old one-per-user key stays, and the new columns stay
nullable (the previous release writes rows without them). 0005 completes it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31
"""

import json

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Fixed, so environments agree and the tests can name it.
IAC_ID = "6a1c0f3e-3b7d-4c2a-9e51-1ac000000001"
IAC_CODE = "IAC"
IAC_NAME = "Immigration and Asylum Chamber"
IAC_ENABLED = True
IAC_AUTO_ENROL = True  # new users get in as they did before jurisdictions

# The text the web app showed before DIAAT-322 (transcribe-web 00ff378), unchanged.
IAC_SETTINGS = {
    "wording": {
        "home.intro": (
            "Supporting judicial work through live transcription and enablement of judges to focus on "
            "proceedings and engage with the parties."
        ),
        "help.subtitle": "Getting started with Bench Notes for the First-tier Tribunal Immigration and Asylum Chamber",
        "nav.recordings": "Hearing recordings",
        "records.column_title": "Hearing",
        "records.column_type": "Hearing type",
        "records.search_placeholder": "Search hearings or dates",
        "records.search_hint": "Search recent recordings by hearing title or date.",
        "records.filter_label": "Filter recent recordings by hearing type",
        "records.filter_placeholder": "Filter by hearing type",
        "records.filter_all": "All hearing types",
        "records.no_match": "No hearings matched your current search or filters.",
        "records.open": "Open hearing",
        "records.rename_hint": "Update the title shown in the hearings list.",
        "records.no_transcript": "No saved transcript was found for this hearing.",
        "autosave.record_name": "case record",
        "alerts.speech_lost_guidance": (
            "Pause then restart recording to restart dictation. If this warning comes back, record this "
            "decision in another way and raise with your Support team for investigation."
        ),
    },
    "template": "iac_decision",
    "features": {"hearing_recordings": True},
}

_MATERIAL = ("transcription", "transcription_job")


def upgrade() -> None:
    op.create_table(
        "jurisdiction",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_enrol", sa.Boolean(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jurisdiction_code", "jurisdiction", ["code"], unique=True)

    op.create_table(
        "jurisdiction_membership",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("jurisdiction_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdiction.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jurisdiction_id", "user_id", name="uq_jurisdiction_membership_user"),
        sa.CheckConstraint("role IN ('judge', 'admin')", name="ck_jurisdiction_membership_role"),
        sa.CheckConstraint("user_id IS NOT NULL OR email IS NOT NULL", name="ck_jurisdiction_membership_who"),
    )
    op.create_index("ix_jurisdiction_membership_jurisdiction_id", "jurisdiction_membership", ["jurisdiction_id"])
    op.create_index("ix_jurisdiction_membership_user_id", "jurisdiction_membership", ["user_id"])
    op.create_index(
        "uq_jurisdiction_membership_pending_email",
        "jurisdiction_membership",
        ["jurisdiction_id", "email"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )

    op.execute(
        sa.text(
            "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime, updated_datetime)"
            " VALUES (:id, :code, :name, :enabled, :auto_enrol, CAST(:settings AS jsonb), now(), now())"
        ).bindparams(
            id=IAC_ID,
            code=IAC_CODE,
            name=IAC_NAME,
            enabled=IAC_ENABLED,
            auto_enrol=IAC_AUTO_ENROL,
            settings=json.dumps(IAC_SETTINGS),
        )
    )
    op.execute(
        sa.text(
            'INSERT INTO jurisdiction_membership (id, jurisdiction_id, user_id, role, created_datetime)'
            ' SELECT gen_random_uuid(), :iac, id, \'judge\', now() FROM "user"'
        ).bindparams(iac=IAC_ID)
    )

    op.create_table(
        "upload_jurisdiction",
        sa.Column("transcription_id", sa.Uuid(), nullable=False),
        sa.Column("jurisdiction_id", sa.Uuid(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdiction.id"]),
        sa.PrimaryKeyConstraint("transcription_id"),
    )

    op.add_column("user", sa.Column("active_jurisdiction_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(None, "user", "jurisdiction", ["active_jurisdiction_id"], ["id"])

    for table in _MATERIAL:
        op.add_column(table, sa.Column("jurisdiction_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(None, table, "jurisdiction", ["jurisdiction_id"], ["id"])
        op.create_index(f"ix_{table}_jurisdiction_id", table, ["jurisdiction_id"])
    op.execute(sa.text("UPDATE transcription SET jurisdiction_id = :iac").bindparams(iac=IAC_ID))
    op.execute(sa.text("UPDATE transcription_job SET jurisdiction_id = :iac").bindparams(iac=IAC_ID))

    op.add_column("live_transcript_draft", sa.Column("jurisdiction_id", sa.Uuid(), nullable=True))
    op.execute(sa.text("UPDATE live_transcript_draft SET jurisdiction_id = :iac").bindparams(iac=IAC_ID))
    op.create_foreign_key(None, "live_transcript_draft", "jurisdiction", ["jurisdiction_id"], ["id"])
    # The new code's upsert key. The old one-per-user key stays until 0005.
    op.create_unique_constraint(
        "uq_live_transcript_draft_user_jurisdiction", "live_transcript_draft", ["user_id", "jurisdiction_id"]
    )


def downgrade() -> None:
    # Records of other jurisdictions are kept, without their jurisdiction.
    op.drop_constraint("uq_live_transcript_draft_user_jurisdiction", "live_transcript_draft", type_="unique")
    op.drop_constraint("live_transcript_draft_jurisdiction_id_fkey", "live_transcript_draft", type_="foreignkey")
    op.drop_column("live_transcript_draft", "jurisdiction_id")

    for table in reversed(_MATERIAL):
        op.drop_index(f"ix_{table}_jurisdiction_id", table_name=table)
        op.drop_constraint(f"{table}_jurisdiction_id_fkey", table, type_="foreignkey")
        op.drop_column(table, "jurisdiction_id")

    op.drop_constraint("user_active_jurisdiction_id_fkey", "user", type_="foreignkey")
    op.drop_column("user", "active_jurisdiction_id")
    op.drop_table("upload_jurisdiction")

    op.drop_index("uq_jurisdiction_membership_pending_email", table_name="jurisdiction_membership")
    op.drop_index("ix_jurisdiction_membership_user_id", table_name="jurisdiction_membership")
    op.drop_index("ix_jurisdiction_membership_jurisdiction_id", table_name="jurisdiction_membership")
    op.drop_table("jurisdiction_membership")
    op.drop_index("ix_jurisdiction_code", table_name="jurisdiction")
    op.drop_table("jurisdiction")
