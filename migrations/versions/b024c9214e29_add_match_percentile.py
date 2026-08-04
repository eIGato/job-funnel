"""add match_percentile, and drop scores computed on the uncentered scale

Revision ID: b024c9214e29
Revises: c7a5f2e91d34
Create Date: 2026-07-31 16:28:37.413652

`match_score` changed meaning: it was a raw cosine against the profile (0.72..0.86 on the real
table, sd 0.023 — a band too narrow to separate a backend posting from a scraped cookie banner)
and is now that cosine with the corpus mean removed (roughly -0.2..0.3, sd 0.093). The two are
not comparable, so every stored value on the old scale is cleared here rather than left to be
read as if it were on the new one.

Nothing is re-embedded: the vectors stay in `jobs.embedding`, and `match` now rescores the whole
shortlist from them on every run. The next `match` refills both columns in one matmul, without a
model call. Between this migration and that run the shortlist reads as empty — `draft` requires
`match_score IS NOT NULL` — which is the intended failure mode: better nothing than a ranking on
a scale that no longer exists.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b024c9214e29"
down_revision: Union[str, Sequence[str], None] = "c7a5f2e91d34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the percentile column and retire scores left on the uncentered scale."""
    op.add_column("jobs", sa.Column("match_percentile", sa.Float(), nullable=True))
    result = op.get_bind().execute(
        sa.text("UPDATE jobs SET match_score = NULL WHERE match_score IS NOT NULL")
    )
    print(f"cleared {result.rowcount} scores computed on the uncentered scale; run `funnel match`")


def downgrade() -> None:
    """Drop the column. The old scores are not restorable — `match` recomputes them."""
    op.drop_column("jobs", "match_percentile")
