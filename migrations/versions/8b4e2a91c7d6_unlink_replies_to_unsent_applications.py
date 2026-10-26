"""unlink replies attached to applications that were never sent

Revision ID: 8b4e2a91c7d6
Revises: 3f9c1d7e5b28
Create Date: 2026-08-06 14:00:00.000000

Data migration, no schema change. The counterpart to the matcher fix in the same commit.

A reply is an answer to something we sent. Attached to an application with `sent_at IS NULL`
it is a mismatch by construction, whatever the matcher believed at the time — there is nothing
for it to be an answer to. One such link existed: reply 71, a justjoin.it *job alert*, sitting
on the never-sent JOIN application, because `join` is a substring of `justjoin` and the domain
strategy compared them without an anchor.

The rows themselves stay. A Reply is evidence — that somebody wrote, and what the classifier
made of it — and it survives its Application by design (`ON DELETE SET NULL`). Only the wrong
link goes; the reply reappears as unmatched in the admin, which is where a human decides.

No status is rolled back with it: `check-replies` leaves the Application alone below
`reply_confidence_threshold` and for a `no_reply` verdict, and this reply was classified
`no_reply` at 0.95. Had a wrong link moved a status, that would need undoing by hand — worth
checking the printed list if this ever runs against other data.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b4e2a91c7d6"
down_revision: Union[str, Sequence[str], None] = "3f9c1d7e5b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNSENT_LINK = """
    SELECT r.id, r.from_address, r.reply_type, a.id AS application_id, a.status, j.company
    FROM replies r
    JOIN applications a ON a.id = r.application_id
    JOIN jobs j ON j.id = a.job_id
    WHERE a.sent_at IS NULL
    ORDER BY r.id
"""


def upgrade() -> None:
    """Drop every reply link to an application that was never sent."""
    connection = op.get_bind()
    rows = connection.execute(sa.text(_UNSENT_LINK)).all()
    for row in rows:
        print(
            f"unlinking reply {row.id} ({row.from_address}, {row.reply_type}) from application "
            f"{row.application_id} [{row.status}] — {row.company} was never applied to"
        )
    connection.execute(
        sa.text(
            "UPDATE replies SET application_id = NULL WHERE application_id IN "
            "(SELECT id FROM applications WHERE sent_at IS NULL)"
        )
    )
    print(f"unlinked {len(rows)} replies from applications that were never sent")


def downgrade() -> None:
    """No-op: the links were wrong, and which reply pointed where is not worth restoring."""
