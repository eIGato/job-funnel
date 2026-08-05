"""job apply_url and apply_resolved_at

Revision ID: b5e8c0a71d43
Revises: a7d3e0c41b62
Create Date: 2026-08-05 14:20:00.000000

The other half of the apply-route work. `apply_blocked` says the posting's own link is a dead
end; these two say what was done about it: `apply_url` is a verified link to the employer's own
page (`orchestration/resolve_link.py`), and `apply_resolved_at` records that a search was made,
whether or not it found anything.

Both nullable, no backfill: nothing has been resolved yet, and NULL means exactly that. A row
with a timestamp and no URL is a remembered miss and is never searched again — clearing the
timestamp in the admin is how a human asks for a retry.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b5e8c0a71d43"
down_revision: Union[str, Sequence[str], None] = "a7d3e0c41b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("jobs", sa.Column("apply_url", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("apply_resolved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("jobs", "apply_resolved_at")
    op.drop_column("jobs", "apply_url")
