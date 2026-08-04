"""repair double-encoded text, rehash, and merge the duplicates it exposes

Revision ID: d4a91c6b28f7
Revises: e1f6a2b7c903
Create Date: 2026-08-03 18:24:51.902377

Data migration, no schema change.

Several feeds serve UTF-8 that was decoded as cp1252 somewhere upstream, sometimes twice over:
`…` arrives as `â€¦` or `Ã¢Â€Â¦`, `München` as `MÃ¼nchen`, a Russian location as `Ð Ð°Ð·Ð²Ñ`.
298 of 2853 rows carried it, concentrated in RemoteOK and arbeitnow. It was not cosmetic: the
mangled text went into the embedding as noise, and 47 rows carried it in the company or title,
which are part of the dedup key.

`schemas.NormalizedJob` now repairs the four text columns before anything downstream sees them,
which is also before `content_hash_for` reads company and title. **That is what makes this
migration necessary rather than optional.** Left alone, the next ingest would hash the repaired
`München GmbH` where the table holds `MÃ¼nchen GmbH`, get a different digest, and insert a
second row for a posting already present — mangled and clean twins side by side in the
shortlist, each with its own cover letter. So: repair the stored text, recompute every hash
under the repaired values, and collapse whatever collides.

Which row of a collision survives: the one whose Application is furthest along, so nothing the
human has acted on is lost. `applications.job_id` is `ON DELETE CASCADE`, so the losing rows
take their redundant applications with them.

Irreversible in the strict sense. `downgrade()` restores the pre-repair hashes for rows that
still exist, but it cannot re-mangle text — the original byte damage is not recoverable from
the repaired string, and would not be worth recovering.
"""

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a91c6b28f7"
down_revision: Union[str, Sequence[str], None] = "e1f6a2b7c903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: How much a status is worth keeping, high wins. Anything the human touched outranks anything
#: the pipeline produced: a SENT application must never lose to a machine-written DRAFTED one.
#: Same table as b3c1e7d40a92, which merged duplicates for the same reason.
_STATUS_RANK = {
    "SENT": 6,
    "INTERVIEW": 5,
    "REJECTED": 4,
    "NO_REPLY": 3,
    "DECLINED": 2,
    "DRAFTED": 1,
    "SHORTLISTED": 0,
}

_TEXT_COLUMNS = ("company", "title", "description", "location")


def upgrade() -> None:
    """Repair the stored text, recompute every hash from it, merge what collides."""
    # Imported here, not at module scope: a migration must run against the code as it is now.
    from funnel.models import compute_content_hash
    from funnel.text import repair_mojibake

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT j.id, j.source_id, j.external_id, j.url,
                   j.company, j.title, j.description, j.location,
                   a.status AS app_status
            FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            ORDER BY j.id
            """
        )
    ).all()

    repaired: dict[int, dict[str, str]] = {}
    groups: dict[str, list[Any]] = {}
    for row in rows:
        fixed = {column: repair_mojibake(getattr(row, column) or "") for column in _TEXT_COLUMNS}
        if any(fixed[column] != (getattr(row, column) or "") for column in _TEXT_COLUMNS):
            repaired[row.id] = fixed
        digest = compute_content_hash(
            fixed["company"],
            fixed["title"],
            row.url,
            source_id=row.source_id,
            external_id=row.external_id,
        )
        groups.setdefault(digest, []).append(row)

    keepers: dict[int, str] = {}  # surviving job id -> its hash under the repaired text
    doomed: list[int] = []
    for digest, members in groups.items():
        # Highest-ranked application wins; the oldest row breaks a tie.
        keeper = max(members, key=lambda r: (_STATUS_RANK.get(r.app_status or "", -1), -r.id))
        keepers[keeper.id] = digest
        doomed.extend(row.id for row in members if row.id != keeper.id)

    # Delete before rewriting: while a collision's members are still there, two rows want the
    # same content_hash and the unique index would reject the update.
    if doomed:
        connection.execute(sa.text("DELETE FROM jobs WHERE id = ANY(:ids)"), {"ids": doomed})

    for job_id, fixed in repaired.items():
        if job_id not in keepers:
            continue  # deleted as a duplicate above
        connection.execute(
            sa.text(
                "UPDATE jobs SET company = :c, title = :t, description = :d, location = :l "
                "WHERE id = :i"
            ),
            {
                "c": fixed["company"],
                "t": fixed["title"],
                "d": fixed["description"],
                "l": fixed["location"] or None,
                "i": job_id,
            },
        )

    for job_id, digest in keepers.items():
        connection.execute(
            sa.text("UPDATE jobs SET content_hash = :h WHERE id = :i"),
            {"h": digest, "i": job_id},
        )

    # The repaired rows carry different words now, so their vectors are stale. Clearing the
    # embedding is enough — `match` re-embeds anything without one and rescores the whole
    # corpus on every run, so the next run picks these up by itself.
    if repaired:
        connection.execute(
            sa.text("UPDATE jobs SET embedding = NULL WHERE id = ANY(:ids)"),
            {"ids": [job_id for job_id in repaired if job_id in keepers]},
        )


def downgrade() -> None:
    """Restore each surviving row's hash. The repaired text stays repaired; so do the deletions.

    Re-mangling is not implementable: `repair_mojibake` is lossy in the direction that matters,
    since several broken encodings collapse onto one correct string. The hash is restored from
    the text as it now stands, which is what the previous revision's code would compute for it.
    """
    from funnel.models import compute_content_hash

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, source_id, external_id, url, company, title FROM jobs")
    ).all()
    for row in rows:
        connection.execute(
            sa.text("UPDATE jobs SET content_hash = :h WHERE id = :i"),
            {
                "h": compute_content_hash(
                    row.company,
                    row.title,
                    row.url,
                    source_id=row.source_id,
                    external_id=row.external_id,
                ),
                "i": row.id,
            },
        )
