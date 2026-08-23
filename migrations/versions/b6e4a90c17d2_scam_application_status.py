"""record a fraudulent posting as SCAM, not DECLINED

Revision ID: b6e4a90c17d2
Revises: 4d9f97d6fd94
Create Date: 2026-08-23 21:40:00.000000

Data migration, no schema change. `applications.status` is `Enum(..., native_enum=False)` with
SQLAlchemy 2.0's default `create_constraint=False`, so the column is a plain VARCHAR(11) with no
CHECK to alter, and "scam" fits inside the width "shortlisted" already set — a new value costs
no DDL, exactly as CLOSED did in 3f9c1d7e5b28.

DECLINED means *we* judged the fit and never applied. Application 166 was neither half of that:
the posting (job 4257, "Software Developer" at "Brahmandnayak Group Of Companies", Berlin, off a
Glassdoor alert) was an ad placed to collect passports, and the letter and CV really did go out
on 2026-08-11 before the human recognized it. Filed as DECLINED it claimed two untrue things at
once, and the only record of what actually happened was the free-text note "Scam".

The criterion is the note, not the timestamp: a fraudulent posting is a fraudulent posting
whether or not a letter reached it, so any DECLINED row whose notes name it is moved. What the
timestamp gets is a *report*. DECLINED with `sent_at` set is a contradiction — nothing in `src/`
writes that column (invariant 2: the human types it in the admin), so a value there means a
letter went out, which is not what DECLINED describes — and any such row this migration cannot
explain is printed and left alone. Guessing at the rest is how the never-sent rejections got
their fabricated `reply_at` in the first place.

`sent_at` is kept. The letter went out; erasing the timestamp would make the row read as if it
had not, and the whole point of the new status is to be able to count that separately. SCAM is
deliberately absent from `REPLYABLE_STATUSES`, so nothing re-scans the thread — a scam answers,
and `replies/link.py` writes a classifier's verdict straight onto the status.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6e4a90c17d2"
down_revision: Union[str, Sequence[str], None] = "4d9f97d6fd94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NOTED_AS_SCAM = "(notes ILIKE '%scam%' OR notes ILIKE '%скам%' OR notes ILIKE '%мошен%')"


def upgrade() -> None:
    """Move the noted-as-scam applications off DECLINED, keeping every timestamp."""
    connection = op.get_bind()
    moved = connection.execute(
        sa.text(
            "UPDATE applications SET status = 'SCAM' "
            f"WHERE status = 'DECLINED' AND notes IS NOT NULL AND {_NOTED_AS_SCAM} "
            "RETURNING id"
        )
    ).all()
    print(f"reclassified {len(moved)} applications as SCAM: {[row.id for row in moved]}")

    contradictory = connection.execute(
        sa.text(
            "SELECT id, sent_at FROM applications "
            "WHERE status = 'DECLINED' AND sent_at IS NOT NULL ORDER BY id"
        )
    ).all()
    for row in contradictory:
        # Left alone on purpose. DECLINED with a `sent_at` is a contradiction, but this
        # migration only knows why for the rows that say so in their notes.
        print(
            f"NOTE: application {row.id} is still DECLINED with sent_at={row.sent_at} — a letter "
            "went out, so 'we chose not to apply' is not what happened; set the status by hand"
        )


def downgrade() -> None:
    """Put the rows back to DECLINED. The note in `notes` is what survives either way."""
    op.get_bind().execute(
        sa.text("UPDATE applications SET status = 'DECLINED' WHERE status = 'SCAM'")
    )
