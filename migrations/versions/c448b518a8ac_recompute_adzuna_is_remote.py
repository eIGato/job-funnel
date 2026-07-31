"""recompute is_remote for Adzuna rows flagged by a bare substring search

Revision ID: c448b518a8ac
Revises: b024c9214e29
Create Date: 2026-07-31 16:53:41.700764

Data migration, no schema change.

Adzuna publishes no remote flag, so the adapter read one off the teaser text — and a teaser is
exactly where a split arrangement gets spelled out. "3 Days onsite, 2 Days work from home"
contains "work from home", so a hybrid Chandler, AZ contract sat on the remote-first shortlist.
`looks_remote` now weighs an outright claim, then a hybrid marker, then a bare mention.

`is_remote` is set at ingest and nothing recomputes it, so this re-derives it from the stored
title/location/description — the same three fields the adapter had. Scoped to Adzuna on
purpose: every other adapter reads a structured field or serves remote work exclusively, and
re-deriving those from prose would replace a fact with a guess.

Measured before writing: of 190 stored rows, 28 were flagged remote and 10 of those flip. Two
of the ten ("Location: Remote / Hybrid", "Austin, TX, USA (Hybrid/Remote)") genuinely offer
both, and are now called hybrid. That is the intended reading — the posting names a city, the
human is not in it — and it demotes rather than drops: `is_remote` orders the shortlist, it
does not filter it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c448b518a8ac"
down_revision: Union[str, Sequence[str], None] = "b024c9214e29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Re-derive is_remote for Adzuna postings from the text the adapter saw."""
    # Imported here, not at module scope: a migration runs against the code as it is now.
    from funnel.adapters.util import looks_remote

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT j.id, j.title, j.location, j.description, j.is_remote FROM jobs j "
            "JOIN sources s ON s.id = j.source_id WHERE s.name = 'adzuna'"
        )
    ).all()

    changed = 0
    for row in rows:
        remote = looks_remote(row.title, row.location, row.description or "")
        if remote != row.is_remote:
            connection.execute(
                sa.text("UPDATE jobs SET is_remote = :r WHERE id = :i"),
                {"r": remote, "i": row.id},
            )
            changed += 1
    print(f"re-read is_remote on {len(rows)} adzuna jobs, {changed} changed")


def downgrade() -> None:
    """No-op: the previous per-row flags were a bug, and were not recorded to restore from."""
