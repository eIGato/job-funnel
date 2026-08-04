"""Screening and drafting one posting: the step, without the caller's plumbing around it.

Three callers reach the same judgment through here — the batch `funnel draft`, the by-hand
`funnel draft --job <id>`, and the admin's per-row button — and that is the whole point. A
posting must not be screened one way by the timer and another way by a button.

Async because the model calls are: `asyncio.run` at a CLI boundary is fine, inside a running
event loop it raises, and the admin is a Starlette app. The CLI wraps this; the admin awaits it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from funnel.models import Job

Verdict = Literal["drafted", "declined", "error"]


@dataclass(frozen=True, slots=True)
class DraftOutcome:
    """What happened to one posting, in a shape both a terminal and a flash message can use."""

    verdict: Verdict
    #: One line for the human. The screen's reasoning, the letter's subject, or the error.
    detail: str

    def __str__(self) -> str:
        return f"{self.verdict}: {self.detail}"


async def screen_and_draft(session: Session, job: Job, *, do_screen: bool) -> DraftOutcome:
    """Screen the posting, write the letter it earns, and record the result on its Application.

    Writes to the session but does not commit — the caller owns the transaction. DOES NOT SEND
    (invariant 2): the letter lands in the database and waits for a human.

    An existing Application is reused and overwritten. Guarding against that belongs to the
    caller: the batch path never offers a decided posting in the first place (`shortlist_select`
    excludes it), while the by-hand paths exist precisely to redo one.
    """
    from funnel.drafting.cover_letter import draft_cover_letter
    from funnel.drafting.screen import screen_job
    from funnel.models import Application, ApplicationStatus

    try:
        # Screen before drafting: the verdict is cheap, the letter is not.
        verdict = await screen_job(job) if do_screen else None
        letter = (
            None if verdict is not None and not verdict.worth_it else await draft_cover_letter(job)
        )
    except Exception as exc:  # one bad posting must not sink a batch of twenty-five
        return DraftOutcome("error", str(exc))

    application = job.application
    if application is None:
        application = Application(job_id=job.id)
        session.add(application)

    if letter is None:
        assert verdict is not None
        application.status = ApplicationStatus.DECLINED
        application.notes = f"Screen declined: {verdict.reasoning}"
        return DraftOutcome("declined", verdict.reasoning)

    application.cover_letter = f"Subject: {letter.subject}\n\n{letter.body}"
    application.status = ApplicationStatus.DRAFTED
    if letter.matched_points:
        application.notes = "Leans on: " + "; ".join(letter.matched_points)
    return DraftOutcome("drafted", letter.subject)
