"""record a requirement the human cannot meet as INELIGIBLE, not DECLINED

Revision ID: d3f6b81c04ae
Revises: b6e4a90c17d2
Create Date: 2026-08-27 10:20:00.000000

Data migration, no schema change. `applications.status` is `Enum(..., native_enum=False)` with
`create_constraint=False`, so the column is a plain VARCHAR(11) with no CHECK, and "INELIGIBLE"
is 10 characters — a new value costs no DDL, as CLOSED, SCAM and UNREACHABLE did before it.

DECLINED means *the screen* judged the role a poor fit. It was also carrying the opposite fact:
the human reading a posting and finding a mandatory requirement he cannot meet — a language he
does not speak, a work authorization nobody will file for him. "We do not want them" and "they
cannot take us" are different measurements. The first grades the screen and the profile; the
second is the backlog of `matching/filters.py`, since every such row is a hard filter that does
not exist yet and cost a screening call and a cover letter to find.

**The criterion is the note, and only a note the human wrote.** The machine writes its own
prefixes — "Screen declined:", "Agent declined:", "Agent refused", "Leans on:" — and a drafter's
"Leans on:" audit trail can quote a posting's visa sentence while the human declined it for
something else entirely. Only hand-written notes are read, and only those naming eligibility.

Measured on 2026-08-27: 547 DECLINED rows, of which 502 are the screen's own verdict and 45 are
the human changing the status by hand. Exactly 4 of those 45 say why — everything else holds the
drafter's audit trail and no reason at all, and is left alone. Guessing at the other 41 is how
the fabricated `reply_at` values that migration a1c7e35f9b04 had to undo came about; the count is
printed instead, because an unreadable backlog is worth knowing the size of.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f6b81c04ae"
down_revision: Union[str, Sequence[str], None] = "b6e4a90c17d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: A note the pipeline wrote about its own reasoning, not the human recording what he found.
_MACHINE_NOTE = (
    "notes ~ '^(Screen declined|Agent declined|Agent refused|Leans on|Reviewer|Company):?'"
)

#: Eligibility in the sense `matching/profile.CONSTRAINT_KEYS` means it: where he may live, what
#: he may sign, what he may be paid under — plus the language case, which is the same shape (a
#: requirement of the person rather than of the work) and is what note 701 records.
_NOTES_AN_ELIGIBILITY_FACT = (
    "notes ~* '(visa|sponsor|work authori[sz]|work permit|citizen|passport|residen|"
    "relocat|clearance|(do(es)? ?n.t|cannot|can.t) speak|speak (german|french|dutch|polish|"
    "spanish|italian|portuguese|swedish|norwegian|danish|czech|hebrew|arabic|japanese|chinese))'"
)


def upgrade() -> None:
    """Move the hand-noted eligibility refusals off DECLINED, keeping every timestamp."""
    connection = op.get_bind()
    moved = connection.execute(
        sa.text(
            "UPDATE applications SET status = 'INELIGIBLE' "
            "WHERE status = 'DECLINED' AND notes IS NOT NULL "
            f"AND NOT ({_MACHINE_NOTE}) AND {_NOTES_AN_ELIGIBILITY_FACT} "
            "RETURNING id"
        )
    ).all()
    print(f"reclassified {len(moved)} applications as INELIGIBLE: {[row.id for row in moved]}")

    unreadable = connection.execute(
        sa.text(
            "SELECT count(*) FROM applications "
            "WHERE status = 'DECLINED' AND notes IS NOT NULL AND notes ~ '^Leans on'"
        )
    ).scalar_one()
    if unreadable:
        # Left alone on purpose: a letter was drafted and the human then declined it by hand,
        # and the row records what the drafter leaned on rather than what he found. Which of
        # these were eligibility is not knowable, and inventing an answer is worse than a gap.
        print(
            f"NOTE: {unreadable} DECLINED applications were drafted for and then declined by "
            "hand with no reason recorded — the backlog this status exists to stop growing"
        )


def downgrade() -> None:
    """Put the rows back to DECLINED. The note in `notes` is what survives either way."""
    op.get_bind().execute(
        sa.text("UPDATE applications SET status = 'DECLINED' WHERE status = 'INELIGIBLE'")
    )
