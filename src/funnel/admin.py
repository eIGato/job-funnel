"""sqladmin: the thin review UI (Phase 2).

Review only: look at the shortlist, read a draft, and manually set the status after the
human has sent an application themselves. It sends nothing.

The one button that *does* something is `JobAdmin.screen_and_draft_action`, and it does the
same thing the timer does — screen a posting and leave a draft. It exists because the workflow
it serves is a browser workflow: a board serves a teaser, the human pastes the real description
into the row, and the letter should follow from the same page. Still nothing sent (invariant 2).

That button is also why the batch no longer drafts from a body it cannot write from
(`cli.MIN_DRAFTABLE_BODY`): those postings keep their rank and wait here for a human to fill
the body in, instead of spending a shortlist slot on a letter about a title.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape
from sqladmin import Admin, ModelView, action
from sqladmin.fields import DateTimeField
from sqladmin.filters import BooleanFilter, ForeignKeyFilter, StaticValuesFilter
from sqladmin.formatters import BASE_FORMATTERS
from sqladmin.forms import ModelConverter, ModelConverterBase, converts
from starlette.applications import Starlette
from starlette.responses import RedirectResponse

from funnel.config import get_settings
from funnel.db import get_engine
from funnel.models import (
    Application,
    ApplicationStatus,
    ApplyChannel,
    Job,
    Reply,
    ReplyType,
    Source,
    SourceKind,
)

if TYPE_CHECKING:
    from enum import StrEnum

    from sqlalchemy import Select
    from sqlalchemy.orm import ColumnProperty, InstrumentedAttribute
    from starlette.requests import Request
    from wtforms.fields.core import UnboundField


def admin_zone() -> ZoneInfo:
    """The wall clock the admin speaks (`ADMIN_TIMEZONE`). Storage is UTC regardless."""
    return ZoneInfo(get_settings().admin_timezone)


def _to_local(value: datetime) -> datetime:
    """A stored instant as the human's wall clock. A naive value is read as UTC, as stored."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(admin_zone())


class LocalDateTimeField(DateTimeField):
    """A datetime field that reads and writes the human's local wall clock.

    Every timestamp column is `DateTime(timezone=True)` and everything the pipeline writes is
    UTC — which is right, and stays. The problem was only ever the form: it rendered the UTC
    value and parsed whatever was typed straight back as UTC, so entering the time off your own
    watch recorded an instant two hours in the future. Every one of the 22 hand-entered
    `sent_at` values was wrong by exactly the offset (migration a1c7e35f9b04).

    Conversion lives in the field rather than in the view, so it cannot be forgotten on a
    column added later: `LocalTimeConverter` maps *every* DateTime column onto this field.

    Subclassed from sqladmin's DateTimeField rather than wtforms' own: that is the one carrying
    `DateTimePickerWidget`, and inheriting from the plain wtforms field silently swapped the
    calendar picker for a bare text box.

    At the autumn changeover an hour repeats, and a bare wall clock inside it is genuinely
    ambiguous; `replace(tzinfo=...)` resolves it to the first pass (`fold=0`). One hour a year,
    on a field a human can correct — not worth a disambiguation UI.
    """

    def process_data(self, value: Any) -> None:
        if isinstance(value, datetime):
            value = _to_local(value).replace(tzinfo=None)
        super().process_data(value)

    def process_formdata(self, valuelist: Any) -> None:
        super().process_formdata(valuelist)
        if isinstance(self.data, datetime) and self.data.tzinfo is None:
            self.data = self.data.replace(tzinfo=admin_zone()).astimezone(UTC)


class LocalTimeConverter(ModelConverter):
    """Route every DateTime column to `LocalDateTimeField`, and say so on the label."""

    @converts("DateTime")  # type: ignore[untyped-decorator]  # sqladmin ships no types
    def conv_datetime(
        self, model: type, prop: ColumnProperty[Any], kwargs: dict[str, Any]
    ) -> UnboundField[Any]:
        # Matching wtforms' own default when the view names no label, so the zone is appended
        # to "Sent At" rather than replacing it with the raw column key.
        label = kwargs.get("label") or prop.key.replace("_", " ").title()
        kwargs["label"] = f"{label} ({get_settings().admin_timezone})"
        # Constructing a wtforms field outside a form yields an UnboundField, which is what a
        # converter is expected to return; the stubs describe the bound case only.
        return cast("UnboundField[Any]", LocalDateTimeField(**kwargs))


def _local_datetime(value: datetime) -> str:
    """List and detail rendering: the same wall clock the form takes, with the zone spelled out.

    The zone abbreviation is the point. A bare "21:00" next to a UTC one elsewhere is how this
    bug started; "21:00 CEST" cannot be misread, and it comes from the value's own offset, so it
    stays honest across the changeover.
    """
    return _to_local(value).strftime("%Y-%m-%d %H:%M %Z")


class LocalTimeView:
    """Mixin: local wall clock in the forms, in the list and in the detail view.

    A mixin rather than a base ModelView because sqladmin's metaclass registers a view by its
    `model=` keyword; this carries only the two settings and each view keeps its own model.
    """

    form_converter: ClassVar[type[ModelConverterBase]] = LocalTimeConverter
    column_type_formatters: ClassVar[dict[type, Any]] = {
        **BASE_FORMATTERS,
        datetime: _local_datetime,
    }


def _enum_filter(
    column: InstrumentedAttribute[Any], enum_cls: type[StrEnum], title: str | None = None
) -> StaticValuesFilter:
    """A dropdown of every member of an enum column.

    `StaticValuesFilter` rather than `AllUniqueStringValuesFilter`: the values are known from
    the type, so there is no reason to spend a `SELECT DISTINCT` on every page load, and a
    status nothing currently holds still appears in the list instead of vanishing from it.
    The stored form is the member *name*, but SQLAlchemy's Enum accepts either side of the
    lookup, so the URL carries the readable lowercase value.
    """
    return StaticValuesFilter(
        column,
        values=[(member.value, member.value.replace("_", " ").capitalize()) for member in enum_cls],
        title=title,
    )


class UnmatchedRepliesFilter:
    """ "Which replies is nobody looking at?" — the one question this table is for.

    Not expressible with the built-in filters: `application_id` is a foreign key, so
    `ForeignKeyFilter` offers "which application" and never "none of them". Yet an unmatched
    reply is precisely the review queue — a recruiter writing out of the blue, or an answer to
    a Telegram application, which by construction has no thread and usually no company domain
    to match on either (PLAN.md section 7).

    `default_value = None` rather than sqladmin's private `_UNSET` sentinel — the protocol
    requires the attribute, and the only consequence is that the filter is consulted on an
    unfiltered page too, where it hands the query straight back.
    """

    has_operator = False
    template = "sqladmin/filters/lookup_filter.html"
    title = "Matched to an application"
    parameter_name = "matched"
    default_value: Any = None

    async def lookups(self, request: Request, model: Any, run_query: Any) -> list[tuple[str, str]]:
        return [("__all", "All"), ("no", "Unmatched only"), ("yes", "Matched only")]

    async def get_filtered_query(self, query: Select[Any], value: Any, model: Any) -> Select[Any]:
        if value == "no":
            return query.filter(Reply.application_id.is_(None))
        if value == "yes":
            return query.filter(Reply.application_id.isnot(None))
        return query


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


class SourceAdmin(LocalTimeView, ModelView, model=Source):
    name = "Source"
    name_plural = "Sources"
    icon = "fa-solid fa-rss"
    column_list = [Source.id, Source.name, Source.kind, Source.enabled, Source.last_run_at]
    column_searchable_list = [Source.name]
    column_sortable_list = [Source.name, Source.last_run_at]
    column_filters = [BooleanFilter(Source.enabled), _enum_filter(Source.kind, SourceKind)]
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


class JobAdmin(LocalTimeView, ModelView, model=Job):
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
        Job.apply_blocked,
        Job.match_percentile,
        Job.posted_at,
    ]
    # "No apply route" rather than the column name: the human's question in front of a
    # high-scoring row that never gets drafted is "why is this being skipped?", and the answer
    # is that the link is a dead end from here (`matching/apply_route.py`), not that the posting
    # is bad. Sortable, so the blocked rows can be read as a batch — those are the ones worth
    # chasing by hand.
    column_labels = {Job.match_percentile: "Match", Job.apply_blocked: "No apply route"}
    column_formatters = {Job.match_percentile: _percentile, Job.url: _link}
    column_searchable_list = [Job.company, Job.title]
    column_sortable_list = [
        Job.match_percentile,
        Job.match_score,
        Job.is_remote,
        Job.apply_channel,
        Job.apply_blocked,
        Job.posted_at,
        Job.company,
    ]
    # The shortlist questions a human actually arrives with: which board did this come from,
    # is it remote, and why is that high-scoring row never drafted (no apply route, or it never
    # cleared the hard filters). Search covers company and title above.
    column_filters = [
        ForeignKeyFilter(Job.source_id, Source.name, foreign_model=Source, title="Source"),
        BooleanFilter(Job.is_remote, title="Remote"),
        BooleanFilter(Job.apply_blocked, title="No apply route"),
        BooleanFilter(Job.hard_filter_passed, title="Passed hard filters"),
        _enum_filter(Job.apply_channel, ApplyChannel, title="Apply channel"),
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


class ApplicationAdmin(LocalTimeView, ModelView, model=Application):
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
    column_sortable_list = [Application.status, Application.sent_at, Application.reply_at]
    # Sorting by status groups them; filtering is what "show me only the drafted ones" needs,
    # and that is the normal way into this table.
    column_filters = [
        _enum_filter(Application.status, ApplicationStatus, title="Status"),
        _enum_filter(Application.reply_type, ReplyType, title="Reply"),
    ]
    # Dotted paths: an application is identified by its posting, so searching it means searching
    # the job. sqladmin joins the relationship for the search itself (`search_query`).
    column_searchable_list = ["job.company", "job.title"]
    column_formatters_detail = {
        Application.cover_letter: _multiline,
        Application.notes: _multiline,
    }
    # Application.replies is a plain relationship (no delete-orphan), so an empty submit would
    # only detach rows rather than delete them — but detaching silently is still the Phase 2
    # failure mode. check-replies owns this collection; keep it off the form.
    form_excluded_columns = [Application.created_at, Application.updated_at, Application.replies]
    page_size = 50


class ReplyAdmin(LocalTimeView, ModelView, model=Reply):
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
    column_filters = [
        _enum_filter(Reply.reply_type, ReplyType, title="Reply type"),
        UnmatchedRepliesFilter(),
    ]
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
