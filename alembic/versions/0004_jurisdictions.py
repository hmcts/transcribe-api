"""Jurisdictions: wording per jurisdiction, and who may edit each (DIAAT-342).

Until now document_content was a singleton row (id=1) holding the one set of
wording, the First-tier Tribunal Immigration and Asylum Chamber's. This makes the
wording per jurisdiction and ties admins to jurisdictions:

- jurisdiction: the jurisdictions. "iac" is created here, named as the judge's
  form names it.
- document_content.jurisdiction_id: which jurisdiction a row belongs to (unique,
  one set of wording each). The existing row is kept as it is, content and
  updated_at included, and becomes "iac"'s. Nothing is reseeded. On a database
  that has no row yet (a fresh one) there is still none; the API seeds it from the
  bundled JSON on first read as before. its id sequence is moved on so rows for
  further jurisdictions get ids from 2; id 1 is reserved for IAC (also on a fresh database,
  where the API inserts it as id 1), so code and tools that read id=1 still find the IAC
  wording, a downgrade included.
- jurisdiction: unique indexes on lower(id) and lower(name), because a form names a
  jurisdiction ignoring case. The API also serialises creates and checks id against name.
- jurisdiction_admin: the user-to-jurisdiction mapping, managed by a
  SystemAdministrator through the API. Every user whose role is LegalTextManager
  when this runs is mapped to "iac", so the IAC admins keep the access they have.
  (Users who log in later with that role are mapped by a SystemAdministrator.)

The upgrade is additive: no existing value changes. The downgrade drops the mapping
and the jurisdictions, and with them the wording of every jurisdiction except
"iac", whose row it leaves exactly as it was.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Frozen here: a migration must not change if the application's constants do.
IAC_ID = "iac"
IAC_NAME = "First-tier Tribunal Immigration and Asylum Chamber"


def upgrade() -> None:
    op.create_table(
        "jurisdiction",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    # A form names a jurisdiction by id or name ignoring case, so two must never differ only in case.
    op.create_index("uq_jurisdiction_id_lower", "jurisdiction", [sa.text("lower(id)")], unique=True)
    op.create_index("uq_jurisdiction_name_lower", "jurisdiction", [sa.text("lower(name)")], unique=True)
    op.execute(
        sa.text("INSERT INTO jurisdiction (id, name, created_datetime) VALUES (:id, :name, now())").bindparams(
            id=IAC_ID, name=IAC_NAME
        )
    )

    op.add_column("document_content", sa.Column("jurisdiction_id", sa.String(), nullable=True))
    op.execute(sa.text("UPDATE document_content SET jurisdiction_id = :iac WHERE id = 1").bindparams(iac=IAC_ID))
    op.alter_column("document_content", "jurisdiction_id", existing_type=sa.String(), nullable=False)
    op.create_unique_constraint("uq_document_content_jurisdiction_id", "document_content", ["jurisdiction_id"])
    op.create_foreign_key(
        "fk_document_content_jurisdiction_id", "document_content", "jurisdiction", ["jurisdiction_id"], ["id"]
    )
    # id already has a serial default (0001), unused while the one row was inserted as id=1.
    # id 1 is reserved for IAC (the API inserts it explicitly), on a fresh database too, so
    # the sequence gives other jurisdictions ids from 2 up. A downgrade then leaves the IAC
    # wording where the previous version reads it.
    op.execute(
        "SELECT setval(pg_get_serial_sequence('document_content', 'id'), "
        "GREATEST((SELECT COALESCE(MAX(id), 1) FROM document_content), 1) + 1, false)"
    )

    op.create_table(
        "jurisdiction_admin",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("jurisdiction_id", sa.String(length=64), nullable=False),
        sa.Column("created_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["jurisdiction_id"], ["jurisdiction.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "jurisdiction_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO jurisdiction_admin (user_id, jurisdiction_id, created_datetime) "
            "SELECT id, :iac, now() FROM \"user\" WHERE role = 'LegalTextManager'"
        ).bindparams(iac=IAC_ID)
    )


def downgrade() -> None:
    op.drop_table("jurisdiction_admin")

    # Only the IAC wording can exist without jurisdictions.
    op.execute(sa.text("DELETE FROM document_content WHERE jurisdiction_id <> :iac").bindparams(iac=IAC_ID))
    op.drop_constraint("fk_document_content_jurisdiction_id", "document_content", type_="foreignkey")
    op.drop_constraint("uq_document_content_jurisdiction_id", "document_content", type_="unique")
    op.drop_column("document_content", "jurisdiction_id")

    op.drop_table("jurisdiction")
