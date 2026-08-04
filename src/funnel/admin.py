"""sqladmin: the thin review UI (Phase 2).

Review only: look at the shortlist, read a draft, and manually set the status after the
human has sent an application themselves. It sends nothing.

The one button that *does* something is `JobAdmin.screen_and_draft_action`, and it does the
same thing the timer does — screen a posting and leave a draft. It exists because the workflow
it serves is a browser workflow: a board serves a teaser, the human pastes the real description
into the row, and the letter should follow from the same page. Still nothing sent (invariant 2).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from markupsafe import Markup, escape
from sqladmin import Admin, ModelView, action
from starlette.applications import Starlette
from starlette.responses import RedirectResponse

from funnel.config import get_settings
from funnel.db import get_engine
from funnel.models import Application, Job, Reply, Source

if TYPE_CHECKING:
    from sqlalchemy import Select
    from starlette.requests import Request


def _multiline(model: Any, attribute: str) -> Markup:
    """Detail-view formatter: render long text with newlines preserved.

    The edit form already uses a textarea, but the details table renders every value on
    one line. Wrap the (escaped) value so a cover letter or a job description keeps its
    paragraphs instead of collapsing into an unreadable strip. `pre-wrap` keeps newlines
    and wraps at whitespace; `overflow-wrap: anywhere` breaks the odd very long URL/token.
    """
    value = getattr(model, attribute)
    if value is None:
        return Markup("")
    return Markup(
        '<div style="white-space: pre-wrap; overflow-wrap: anywhere; max-width: 60rem">'
        f"{escape(value)}</div>"
    )


class SourceAdmin(ModelView, model=Source):
    name = "Source"
    name_plural = "Sources"
    icon = "fa-solid fa-rss"
    column_list = [Source.id, Source.name, Source.kind, Source.enabled, Source.last_run_at]
    column_searchable_list = [Source.name]
    column_sortable_list = [Source.name, Source.last_run_at]
    # Source.jobs is cascade="all, delete-orphan": submitting this form with the field empty
    # deletes every job of the source (and their applications). Ingest owns this collection,
    # not the human — keep it off the form entirely.
    form_excluded_columns = [Source.jobs]


def _link(model: Any, attribute: str) -> Markup:
    """Render a URL as a clickable link, opening in a new tab.

    The review loop is "read the posting, then decide", and sqladmin renders a URL as plain
    text — so reviewing meant selecting and copying an Adzuna redirect URL by hand. `noopener`
    because `target=_blank` otherwise hands the opened page a reference back to the admin.
    """
    value = getattr(model, attribute)
    if not value:
        return Markup("")
    return Markup(f'<a href="{escape(value)}" target="_blank" rel="noopener">{escape(value)}</a>')


def _percentile(model: Any, attribute: str) -> str:
    """List formatter: the match percentile as a percentage.

    The raw score is a centered cosine — spread enough to rank on, but ~0.23 for a strong
    match and negative for half the table, which reads like a bug to a human skimming the
    list. The percentile is the reviewable form: "94% of the shortlist scores below this".
    The score itself stays visible in the detail view for anyone debugging the ranking.
    """
    value = getattr(model, attribute)
    return "" if value is None else f"{value:.1f}%"


class JobAdmin(ModelView, model=Job):
    name = "Job"
    name_plural = "Jobs"
    icon = "fa-solid fa-briefcase"
    column_list = [
        Job.id,
        Job.company,
        Job.title,
        Job.is_remote,
        Job.apply_channel,
        Job.hard_filter_passed,
        Job.match_percentile,
        Job.posted_at,
    ]
    column_labels = {Job.match_percentile: "Match"}
    column_formatters = {Job.match_percentile: _percentile, Job.url: _link}
    column_searchable_list = [Job.company, Job.title]
    column_sortable_list = [
        Job.match_percentile,
        Job.match_score,
        Job.is_remote,
        Job.apply_channel,
        Job.posted_at,
        Job.company,
    ]
    # Score order, which is what the shortlist is now (PLAN.md section 7). It used to lead with
    # `is_remote`, matching a `draft` that sorted the same way — and that turned out to be the
    # bug rather than the spec: remoteness is a bonus on the score, not a partition ahead of it
    # (`cli.shortlist_rank`). Sorting on the percentile is the same order as the score, being
    # monotonic in it; the remote bonus is small enough not to reorder what a human skims.
    column_default_sort = [(Job.match_percentile, True)]
    column_details_exclude_list = [Job.embedding]  # raw bytes are noise in the UI
    column_formatters_detail = {
        Job.description: _multiline,
        Job.match_percentile: _percentile,
        Job.url: _link,
    }

    # content_hash is derived on write (models._fill_content_hash) — nobody types a sha256.
    # Job.application is cascade="all, delete-orphan": leaving it off this form is what stops
    # a Job edit from deleting the application and its cover letter.
    form_excluded_columns = [Job.embedding, Job.content_hash, Job.fetched_at, Job.application]
    page_size = 50

    def sort_query(self, stmt: Select[Any], request: Request) -> Select[Any]:
        """Push unscored rows to the end, whatever the sort.

        A rejected posting has no score, and Postgres orders NULLs first on DESC — so the
        shortlist view opened on the ~180 rows the hard filters threw out, every one blank in
        the Match column, with the actual shortlist below the fold. This clause goes in ahead
        of sqladmin's own, and only ever separates scored from unscored: the order the human
        picks still decides everything within each group.
        """
        return super().sort_query(stmt.order_by(Job.match_percentile.is_(None)), request)

    @action(
        name="screen_and_draft",
        label="Screen & draft letter",
        confirmation_message=(
            "Screen these postings and write a cover letter for each one worth it? "
            "This calls the model and overwrites any existing draft. Nothing is sent."
        ),
    )
    async def screen_and_draft_action(self, request: Request) -> RedirectResponse:
        """Run the drafting step on the selected rows, from the page the human is already on.

        The point is the loop the human works in: a board serves a teaser instead of a posting,
        they paste the real description into this row, and the letter should follow without
        leaving the browser for a terminal. `funnel draft --job <id>` does the same thing and
        remains the scriptable way in.

        Review-only (invariant 6) is about not destroying data and not acting on the human's
        behalf outside their intent — this button does neither. It sends nothing (invariant 2);
        it writes a draft and waits, exactly as the timer does. Overwriting the row's own draft
        is the intent: the human asked for this row by selecting it.

        Synchronous by design, one row at a time. Each posting is two model calls and takes
        tens of seconds, so this is for a handful of rows the human is looking at, not a batch
        — that is what `funnel draft` is for.
        """
        from funnel.db import session_scope
        from funnel.drafting.run import screen_and_draft

        settings = get_settings()
        ids = [int(pk) for pk in request.query_params.get("pks", "").split(",") if pk]
        tally: Counter[str] = Counter()
        with session_scope() as session:
            for job_id in ids:
                job = session.get(Job, job_id)
                if job is None:
                    tally["missing"] += 1
                    continue
                outcome = await screen_and_draft(session, job, do_screen=settings.draft_screen)
                tally[outcome.verdict] += 1

        summary = ", ".join(f"{n} {verdict}" for verdict, n in sorted(tally.items())) or "nothing"
        return RedirectResponse(
            request.url_for("admin:list", identity=self.identity).include_query_params(
                # sqladmin has no flash; the query string is what survives the redirect.
                drafted=summary
            )
        )


class ApplicationAdmin(ModelView, model=Application):
    name = "Application"
    name_plural = "Applications"
    icon = "fa-solid fa-paper-plane"
    column_list = [
        Application.id,
        Application.job,
        Application.status,
        Application.sent_at,
        Application.reply_type,
        Application.reply_at,
    ]
    column_sortable_list = [Application.status, Application.sent_at]
    column_formatters_detail = {
        Application.cover_letter: _multiline,
        Application.notes: _multiline,
    }
    # Application.replies is a plain relationship (no delete-orphan), so an empty submit would
    # only detach rows rather than delete them — but detaching silently is still the Phase 2
    # failure mode. check-replies owns this collection; keep it off the form.
    form_excluded_columns = [Application.created_at, Application.updated_at, Application.replies]
    page_size = 50


class ReplyAdmin(ModelView, model=Reply):
    """Incoming mail and what the classifier made of it.

    The review surface for everything `check-replies` refused to decide: rows with no
    Application are unmatched, and a low `confidence` means the Application status was left
    untouched on purpose. Setting `Reply.application` here is how a human links one by hand,
    so that relationship stays ON the form.
    """

    name = "Reply"
    name_plural = "Replies"
    icon = "fa-solid fa-inbox"
    column_list = [
        Reply.id,
        Reply.received_at,
        Reply.from_address,
        Reply.subject,
        Reply.reply_type,
        Reply.confidence,
        Reply.application,
    ]
    column_searchable_list = [Reply.from_address, Reply.subject]
    column_sortable_list = [Reply.received_at, Reply.reply_type, Reply.confidence]
    column_default_sort = [(Reply.received_at, True)]
    column_formatters_detail = {Reply.body: _multiline, Reply.reasoning: _multiline}
    # gmail_message_id is the idempotency key: editing it by hand would let check-replies
    # re-fetch and re-bill a message already processed.
    form_excluded_columns = [Reply.gmail_message_id, Reply.created_at]
    page_size = 50


app = Starlette()
admin = Admin(app, get_engine(), title="Job Funnel")
admin.add_view(SourceAdmin)
admin.add_view(JobAdmin)
admin.add_view(ApplicationAdmin)
admin.add_view(ReplyAdmin)
