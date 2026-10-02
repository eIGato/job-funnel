"""keep only the id of an email that has nothing to do with the job search

Revision ID: 9d51717f25cb
Revises: d3f6b81c04ae
Create Date: 2026-10-02 18:04:12.158363

`check-replies` wrote every incoming email it read into `replies`, so that the next scan would
know it had seen it and not pay to classify it again. Over half of the table was mail that has
nothing to do with a job search — shop deliveries, telecom bills, Steam sales, product
newsletters — and the matcher kept linking some of it to applications. `skipped_messages` holds
the id of such an email and nothing else: enough for idempotency, nothing a human must page past
and nothing a matcher can read.

Schema only. Moving the existing unrelated rows over is a judgment on each email, made by hand
on 2026-10-02 against the stored text, and is not repeatable as a migration.

Autogenerate also proposed retyping `ats_boards.provider` from VARCHAR(20) to the enum. That is
the model/DDL drift `f8b2c7d19a04` left on purpose (the enum is non-native, so the column stays
a wide VARCHAR) and is deliberately not part of this revision.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d51717f25cb"
down_revision: Union[str, Sequence[str], None] = "d3f6b81c04ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skipped_messages",
        sa.Column("gmail_message_id", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("gmail_message_id"),
    )


def downgrade() -> None:
    op.drop_table("skipped_messages")
