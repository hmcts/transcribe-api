"""restore_document_content_from_prod

Revision ID: a1b2c3d4e5f6
Revises: 500e1b42c7d9
Create Date: 2026-06-08 16:00:00.000000

"""
import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "500e1b42c7d9"
branch_labels = None
depends_on = None

_BUNDLED_JSON = Path(__file__).resolve().parent.parent.parent / "data" / "document_content.json"


def upgrade() -> None:
    content = json.loads(_BUNDLED_JSON.read_text(encoding="utf-8"))
    op.execute(
        sa.text(
            "INSERT INTO document_content (id, content, updated_at) "
            "VALUES (1, CAST(:content AS jsonb), NOW()) "
            "ON CONFLICT (id) DO UPDATE SET content = CAST(:content AS jsonb), updated_at = NOW()"
        ).bindparams(content=json.dumps(content))
    )


def downgrade() -> None:
    pass
