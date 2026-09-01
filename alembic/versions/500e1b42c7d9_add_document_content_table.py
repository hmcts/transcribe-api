"""add_document_content_table

Revision ID: 500e1b42c7d9
Revises: f4a2b8c1d9e3
Create Date: 2026-06-04 17:01:48.060856

"""
import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "500e1b42c7d9"
down_revision = "b4c7e1f2a8d9"
branch_labels = None
depends_on = None

_BUNDLED_JSON = Path(__file__).resolve().parent.parent.parent / "data" / "document_content.json"


def upgrade() -> None:
    op.create_table(
        "document_content",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    if _BUNDLED_JSON.exists():
        content = json.loads(_BUNDLED_JSON.read_text(encoding="utf-8"))
        op.execute(
            sa.text(
                "INSERT INTO document_content (id, content, updated_at) "
                "VALUES (1, CAST(:content AS jsonb), NOW()) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(content=json.dumps(content))
        )


def downgrade() -> None:
    op.drop_table("document_content")
