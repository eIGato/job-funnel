"""re-read hand-entered sent_at as local wall clock, not UTC

Revision ID: a1c7e35f9b04
Revises: 8b4e2a91c7d6
Create Date: 2026-08-06 20:45:00.000000

Data migration, no schema change. The counterpart to `admin.LocalDateTimeField`.

The admin form rendered stored UTC and parsed whatever was typed straight back as UTC, so the
human, reading the time off his own watch, recorded an instant `ADMIN_TIMEZONE`'s offset in the
future — two hours through this summer. Every value is shifted, and the fix is not to subtract
two hours but to *reinterpret*: the wall clock that was typed is correct, only the zone it was
read in was wrong. `AT TIME ZONE 'UTC'` takes the stored instant back to that wall clock, and
`AT TIME ZONE :zone` reads it again as local. That is DST-correct by construction, so a value
from December converts by one hour and one from August by two, with no dates to special-case.

**Only `applications.sent_at`, and all of it.** Nothing in `src/` assigns that column — it
exists because a human types it after sending by hand (invariant 2), so every row is a hand
entry and there is no need to guess which by looking for round numbers. Every other timestamp
in the schema is machine-written and already correct: `reply_at` comes from Gmail's own
`received_at` (`cli.py`), as does `Reply.received_at`; `posted_at` comes from the boards;
`fetched_at`, `created_at`, `updated_at`, `last_run_at` from the clock. The 17 hand-entered
`reply_at` values that did exist were nulled by migration 3f9c1d7e5b28 (they were fabricated
noon markers on applications that were never sent) — nothing is left to correct there.

The zone is read from settings rather than hardcoded, so this migration says the same thing as
the admin it repairs. It is safely re-runnable only in the sense that a second run would shift
again — which is what `downgrade` is for.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c7e35f9b04"
down_revision: Union[str, Sequence[str], None] = "8b4e2a91c7d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Re-read the stored wall clock in `:zone` instead of in UTC.
_TO_LOCAL = "sent_at = (sent_at AT TIME ZONE 'UTC') AT TIME ZONE :zone"
#: And back: read it in `:zone` and store the same wall clock as UTC.
_TO_UTC = "sent_at = (sent_at AT TIME ZONE :zone) AT TIME ZONE 'UTC'"


def _shift(clause: str) -> None:
    # Imported here, not at module scope: a migration runs against the code as it is now.
    from funnel.config import get_settings

    zone = get_settings().admin_timezone
    connection = op.get_bind()
    before = connection.execute(
        sa.text("SELECT count(*) FROM applications WHERE sent_at IS NOT NULL")
    ).scalar_one()
    connection.execute(
        sa.text(f"UPDATE applications SET {clause} WHERE sent_at IS NOT NULL"), {"zone": zone}
    )
    print(f"re-read {before} sent_at values against {zone}")


def upgrade() -> None:
    """Every hand-entered send time was local wall clock stored as UTC. Read it as local."""
    _shift(_TO_LOCAL)


def downgrade() -> None:
    """Put the wall clock back where the old form would have written it."""
    _shift(_TO_UTC)
