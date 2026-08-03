"""widen ats_boards.provider for the longer vendor names

Revision ID: f8b2c7d19a04
Revises: d4a91c6b28f7
Create Date: 2026-08-03 20:11:47.336012

`AtsProvider` gained recruitee and smartrecruiters. `Enum(..., native_enum=False)`
sizes its VARCHAR from the longest member at create time, so the column is VARCHAR(10) — sized
for "greenhouse" — and "smartrecruiters" is fifteen characters. Without this, the first board
of that vendor fails on insert with a StringDataRightTruncation and takes the ingest run's
transaction with it.

Widened to 20 rather than exactly 15, so the next vendor with a long name is a code change
alone. No data changes: every existing value already fits.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8b2c7d19a04"
down_revision: Union[str, Sequence[str], None] = "d4a91c6b28f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "ats_boards",
        "provider",
        existing_type=sa.String(length=10),
        type_=sa.String(length=20),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Narrowing back only works while no long-named vendor has a board; drop those first."""
    op.execute(
        "DELETE FROM ats_boards WHERE provider IN "
        "('recruitee', 'smartrecruiters')"
    )
    op.alter_column(
        "ats_boards",
        "provider",
        existing_type=sa.String(length=20),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
