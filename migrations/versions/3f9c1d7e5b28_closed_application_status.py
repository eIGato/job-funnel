"""record a posting that closed before we applied as CLOSED, not REJECTED

Revision ID: 3f9c1d7e5b28
Revises: a7d3e0c41b62
Create Date: 2026-08-06 12:00:00.000000

Data migration, no schema change. `applications.status` is `Enum(..., native_enum=False)` with
SQLAlchemy 2.0's default `create_constraint=False`, so the column is a plain VARCHAR with no
CHECK to alter — a new value costs no DDL, exactly as DECLINED did when it was added.

Until now, a posting that had stopped taking applications by the time the human went to apply
was recorded as REJECTED with `reply_type=REJECTION` and `reply_at` set to noon of the day it
was noticed. Three things were wrong with that:

- REJECTED means *they* declined *us*. No letter was ever sent for these rows (`sent_at IS
  NULL`), so every one of them counted a refusal against an application that does not exist —
  in both halves of any sent-to-reply rate.
- `reply_at` was fiction. Nobody replied; the noon timestamp existed only to make the row look
  consistent, and would have gone straight into any time-to-answer figure.
- `check-replies` scans SENT/INTERVIEW/REJECTED every run. Thread linking skips a row with no
  `sent_at`, but the row still sits in the candidate list for domain matching — application 104
  had already collected a justjoin.it *job alert* as its "reply" this way.

So these rows become CLOSED with the reply fields cleared. The day the human noticed is not
thrown away: it is appended to `notes`, as a date, without the invented hour. Going forward
`updated_at` carries it, and `notes` takes anything more precise.

`sent_at IS NULL` is the criterion, and it is the honest one: no application went out, so
"they declined us" cannot be what happened. The two REJECTED rows that *do* have a `sent_at`
are real rejections and are left alone.

It is deliberately not `status = 'REJECTED'` alone. One row (Xact Placements Ltd) had the same
never-sent noon `reply_type=REJECTION` marker while its status still read DRAFTED — the status
edit had simply been missed — and left as it was, `draft` would have kept it in its
`{SHORTLISTED, DRAFTED}` working set and written the letter again for a posting that is closed.
Any never-sent row carrying a reply record is the same case, whatever its status says.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f9c1d7e5b28"
down_revision: Union[str, Sequence[str], None] = "a7d3e0c41b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Reclassify never-sent rejections as CLOSED, keeping the discovery date in notes."""
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, reply_at, notes FROM applications WHERE sent_at IS NULL "
            "AND (status = 'REJECTED' OR reply_at IS NOT NULL OR reply_type IS NOT NULL) "
            "ORDER BY id"
        )
    ).all()

    for row in rows:
        # A date, not the fabricated noon. NULL reply_at is possible in principle (the practice
        # was manual); then there is simply nothing to record and the notes are left as they are.
        notes = row.notes
        if row.reply_at is not None:
            note = (
                f"Found closed on {row.reply_at.date().isoformat()} "
                "(recorded as a rejection before the CLOSED status existed)."
            )
            notes = f"{notes}\n\n{note}" if notes else note
        connection.execute(
            sa.text(
                "UPDATE applications SET status = 'CLOSED', reply_at = NULL, reply_type = NULL, "
                "notes = :notes WHERE id = :i"
            ),
            {"i": row.id, "notes": notes},
        )
    print(f"reclassified {len(rows)} never-sent rejections as CLOSED")

    stray = connection.execute(
        sa.text(
            "SELECT r.id, a.id AS application_id, r.from_address FROM replies r "
            "JOIN applications a ON a.id = r.application_id WHERE a.status = 'CLOSED'"
        )
    ).all()
    for row in stray:
        # Left linked on purpose — a Reply is evidence and deleting it is not this migration's
        # call. Flagged because a reply matched to an application that was never sent is a
        # mismatch by construction, and now that the row is CLOSED nothing will revisit it.
        print(
            f"NOTE: reply {row.id} ({row.from_address}) is still linked to CLOSED application "
            f"{row.application_id} — it cannot be an answer to it; unlink it by hand if so"
        )


def downgrade() -> None:
    """Put the rows back to REJECTED, but do not recreate the fictional reply times.

    The noon timestamps this migration removed were never data. Restoring them to satisfy a
    downgrade would be inventing them a second time; the note in `notes` is the honest record.
    """
    op.get_bind().execute(
        sa.text("UPDATE applications SET status = 'REJECTED' WHERE status = 'CLOSED'")
    )
