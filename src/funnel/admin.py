"""sqladmin: the thin review UI (Phase 2).

Review only: look at the shortlist, read a draft, and manually set the status after the
human has sent an application themselves. It sends nothing.
"""

from __future__ import annotations

from typing import Any

from markupsafe import Markup, escape
from sqladmin import Admin, ModelView
from starlette.applications import Starlette

from funnel.db import get_engine
from funnel.models import Application, Job, Reply, Source


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
        Job.match_score,
        Job.posted_at,
    ]
    column_searchable_list = [Job.company, Job.title]
    column_sortable_list = [
        Job.match_score,
        Job.is_remote,
        Job.apply_channel,
        Job.posted_at,
        Job.company,
    ]
    # The shortlist order the human reviews and drafting picks top-N from (PLAN.md section 7):
    # remote first, then by score. "Rank below remote" is this sort, not a score penalty.
    column_default_sort = [(Job.is_remote, True), (Job.match_score, True)]
    column_details_exclude_list = [Job.embedding]  # raw bytes are noise in the UI
    column_formatters_detail = {Job.description: _multiline}
    # content_hash is derived on write (models._fill_content_hash) — nobody types a sha256.
    # Job.application is cascade="all, delete-orphan": leaving it off this form is what stops
    # a Job edit from deleting the application and its cover letter.
    form_excluded_columns = [Job.embedding, Job.content_hash, Job.fetched_at, Job.application]
    page_size = 50


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
