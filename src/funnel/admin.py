"""sqladmin: the thin review UI (Phase 2).

Review only: look at the shortlist, read a draft, and manually set the status after the
human has sent an application themselves. It sends nothing.
"""

from __future__ import annotations

from sqladmin import Admin, ModelView
from starlette.applications import Starlette

from funnel.db import get_engine
from funnel.models import Application, Job, Source


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
        Job.hard_filter_passed,
        Job.match_score,
        Job.posted_at,
    ]
    column_searchable_list = [Job.company, Job.title]
    column_sortable_list = [Job.match_score, Job.posted_at, Job.company]
    column_default_sort = [(Job.match_score, True)]  # best matches on top
    column_details_exclude_list = [Job.embedding]  # raw bytes are noise in the UI
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
    form_excluded_columns = [Application.created_at, Application.updated_at]
    page_size = 50


app = Starlette()
admin = Admin(app, get_engine(), title="Job Funnel")
admin.add_view(SourceAdmin)
admin.add_view(JobAdmin)
admin.add_view(ApplicationAdmin)
