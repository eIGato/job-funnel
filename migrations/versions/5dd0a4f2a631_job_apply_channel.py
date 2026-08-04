"""job apply_channel

Revision ID: 5dd0a4f2a631
Revises: 96f1cdbee2a2
Create Date: 2026-07-23 04:18:36.022297

Autogenerate proposed a single NOT NULL `add_column`, which cannot work on a table that
already has rows. Split into add-nullable -> backfill -> set-not-null, and the backfill
mirrors `models.detect_apply_channel` in SQL so existing postings get a real guess instead
of everything landing on FORM. No server_default is kept: the value is owned by the
application (the before_insert hook) and by the human editing it in the admin.

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "5dd0a4f2a631"
down_revision: Union[str, Sequence[str], None] = "96f1cdbee2a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: SQLAlchemy's Enum(native_enum=False) persists the member NAME, not the value — the existing
#: rows store e.g. 'DRAFTED', so the backfill has to write 'EMAIL'/'TELEGRAM'/'FORM'.
_BACKFILL = sa.text("""
    UPDATE jobs SET apply_channel = CASE
        WHEN lower(url) LIKE 'mailto:%' THEN 'EMAIL'
        WHEN lower(url) ~ '^https?://(www\\.)?(t\\.me|telegram\\.me|telegram\\.dog)(/|$|\\?)'
            THEN 'TELEGRAM'
        ELSE 'FORM'
    END
""")


def upgrade() -> None:
    """Upgrade schema."""
    apply_channel = sa.Enum("EMAIL", "TELEGRAM", "FORM", name="applychannel", native_enum=False)
    op.add_column("jobs", sa.Column("apply_channel", apply_channel, nullable=True))
    op.execute(_BACKFILL)
    op.alter_column("jobs", "apply_channel", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("jobs", "apply_channel")
