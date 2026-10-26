"""job apply_blocked

Revision ID: a7d3e0c41b62
Revises: f8b2c7d19a04
Create Date: 2026-08-05 11:40:00.000000

Adds the flag that keeps a posting with no apply route off the shortlist
(`matching/apply_route.py`). Add-nullable -> backfill -> set-not-null, because the table has
rows; no server_default survives, since the value is owned by `match`, which rewrites it for
every row on every run.

The backfill mirrors `apply_route.is_blocked` in SQL so the flag is honest before the next
`match` rather than after it. Host-anchored for the same reason the Python is: `adzuna.com` as
a substring would also catch `adzuna.com.au`, which is a live site the human can apply on.

Existing drafts for blocked postings are deliberately left alone. A letter already written is
the human's to use or discard — they can still find the posting by hand, which is how they
applied to a RemoteOK role on 2026-08-05 — and this migration's job is to stop *future* slots
going the same way.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a7d3e0c41b62"
down_revision: Union[str, Sequence[str], None] = "f8b2c7d19a04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BACKFILL = sa.text(r"""
    UPDATE jobs SET apply_blocked =
        lower(url) ~ '^https?://([^/@:?#]*\.)?(adzuna\.(com|ca)|remoteok\.com)([:/?#]|$)'
""")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("jobs", sa.Column("apply_blocked", sa.Boolean(), nullable=True))
    op.execute(_BACKFILL)
    op.alter_column("jobs", "apply_blocked", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("jobs", "apply_blocked")
