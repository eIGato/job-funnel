"""rehash jobs on the stable dedup key and merge the duplicates it exposes

Revision ID: b3c1e7d40a92
Revises: 9857afe483ee
Create Date: 2026-07-30 11:42:03.118204

Data migration, no schema change.

`content_hash` used to be company+title+**url**, and a URL is not a stable identity on every
board: Adzuna mints a fresh `se=` search token on every API call, so the same ad arrived under
a new URL — and therefore a new hash — on every ingest run. The table held 548 Adzuna rows for
165 real ads; the admin showed ten identical postings in a row, several of them with their own
separately-drafted cover letter.

`models.compute_content_hash` now keys on the board's own `external_id`, scoped by source, and
falls back to a normalized URL. This recomputes every row's hash under that definition and
collapses the groups it merges.

Which row of a group survives: the one whose Application is furthest along, so nothing the
human has acted on is lost. `applications.job_id` is `ON DELETE CASCADE`, so the losing rows
take their (redundant, duplicate) applications with them.

Irreversible in the strict sense — `downgrade()` restores the old hashes, but the rows deleted
here are gone. That is the point of the migration, and they are duplicates of rows that remain.
"""

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c1e7d40a92"
down_revision: Union[str, Sequence[str], None] = "9857afe483ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: How much a status is worth keeping, high wins. Anything the human touched outranks anything
#: the pipeline produced: a SENT application must never lose to a machine-written DRAFTED one.
_STATUS_RANK = {
    "SENT": 6,
    "INTERVIEW": 5,
    "REJECTED": 4,
    "NO_REPLY": 3,
    "DECLINED": 2,
    "DRAFTED": 1,
    "SHORTLISTED": 0,
}


def upgrade() -> None:
    """Recompute every content_hash, then delete the rows that collide under the new key."""
    # Imported here, not at module scope: a migration must run against the code as it is now.
    from funnel.models import compute_content_hash

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT j.id, j.source_id, j.external_id, j.company, j.title, j.url,
                   a.status AS app_status
            FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            ORDER BY j.id
            """
        )
    ).all()

    groups: dict[str, list[Any]] = {}
    for row in rows:
        digest = compute_content_hash(
            row.company,
            row.title,
            row.url,
            source_id=row.source_id,
            external_id=row.external_id,
        )
        groups.setdefault(digest, []).append(row)

    keepers: dict[int, str] = {}  # surviving job id -> its new hash
    doomed: list[int] = []
    for digest, members in groups.items():
        # Highest-ranked application wins; the oldest row breaks a tie.
        keeper = max(members, key=lambda r: (_STATUS_RANK.get(r.app_status or "", -1), -r.id))
        keepers[keeper.id] = digest
        doomed.extend(row.id for row in members if row.id != keeper.id)

    # Delete before rewriting: while the duplicates are still there, two rows want the same
    # content_hash and the unique index would reject the update.
    if doomed:
        connection.execute(sa.text("DELETE FROM jobs WHERE id = ANY(:ids)"), {"ids": doomed})

    for job_id, digest in keepers.items():
        connection.execute(
            sa.text("UPDATE jobs SET content_hash = :h WHERE id = :i"),
            {"h": digest, "i": job_id},
        )


def downgrade() -> None:
    """Restore the old company+title+url hashes. The deleted duplicate rows do not come back."""
    import hashlib

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, company, title, url FROM jobs")).all()
    for row in rows:
        raw = "|".join(
            (
                row.company.strip().casefold(),
                row.title.strip().casefold(),
                row.url.strip().casefold(),
            )
        )
        connection.execute(
            sa.text("UPDATE jobs SET content_hash = :h WHERE id = :i"),
            {"h": hashlib.sha256(raw.encode()).hexdigest(), "i": row.id},
        )
