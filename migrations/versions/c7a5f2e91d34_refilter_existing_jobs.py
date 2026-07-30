"""re-apply the hard filters to rows scored before the junk-title rule existed

Revision ID: c7a5f2e91d34
Revises: b3c1e7d40a92
Create Date: 2026-07-30 12:05:41.226610

Data migration, no schema change.

`match` is incremental by design: it only looks at rows with `match_score IS NULL`, so a
posting scored under the old filters is never re-examined. That is right for the steady state
and wrong the moment a filter changes — the scraped page furniture RemoteOK handed us ("Job
Details", "Couldn't pick up that page") was already scored at 0.84+ and would have sat at the
top of the shortlist forever, invisible to the new rule.

This re-runs `passes_hard_filters` over every row and rewrites `hard_filter_passed`. Scores are
left alone: the shortlist and `draft` both require `hard_filter_passed`, so clearing the flag is
enough to retire a row, and keeping the score means nothing has to be re-embedded if a filter is
later relaxed.
"""

from dataclasses import dataclass
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a5f2e91d34"
down_revision: Union[str, Sequence[str], None] = "b3c1e7d40a92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


@dataclass
class _Posting:
    """Satisfies the `_Filterable` protocol the filters read, straight off a result row."""

    title: str
    description: str
    location: str | None
    is_remote: bool


def upgrade() -> None:
    """Recompute hard_filter_passed for every existing row."""
    # Imported here, not at module scope: a migration runs against the code as it is now.
    from funnel.matching.filters import passes_hard_filters

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, title, description, location, is_remote, hard_filter_passed FROM jobs")
    ).all()

    retired = 0
    for row in rows:
        passed = passes_hard_filters(
            _Posting(
                title=row.title,
                description=row.description or "",
                location=row.location,
                is_remote=row.is_remote,
            )
        )
        if passed != row.hard_filter_passed:
            connection.execute(
                sa.text("UPDATE jobs SET hard_filter_passed = :p WHERE id = :i"),
                {"p": passed, "i": row.id},
            )
            retired += 1
    print(f"re-filtered {len(rows)} jobs, {retired} changed")


def downgrade() -> None:
    """No-op: the previous per-row verdicts were not recorded anywhere to restore from."""
