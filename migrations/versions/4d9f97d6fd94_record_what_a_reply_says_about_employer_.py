"""Record what a reply says about employer and role

Two nullable columns on `replies`, filled by the classifier on a call that already happens.
They are a proposal for the admin's "Record as sent application" button and nothing reads
them otherwise, so there is nothing to backfill: rows written before this stay null and the
button simply has nothing to offer for them.

Autogenerate also wanted to retype `ats_boards.provider` from VARCHAR(20) to the enum's own
VARCHAR(15). That is an old drift between the column and the model, unrelated to this change
and not worth a length reduction on a live column here — left alone deliberately.

Revision ID: 4d9f97d6fd94
Revises: a1c7e35f9b04
Create Date: 2026-08-12 16:35:04.462597

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d9f97d6fd94"
down_revision: Union[str, Sequence[str], None] = "a1c7e35f9b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("replies", sa.Column("detected_company", sa.String(length=255), nullable=True))
    op.add_column("replies", sa.Column("detected_role", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("replies", "detected_role")
    op.drop_column("replies", "detected_company")
