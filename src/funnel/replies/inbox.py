"""Reading replies out of the mailbox (Phase 6). Gmail I/O only — no LLM here.

Reuses the alert adapter's OAuth (`adapters.gmail.get_credentials`), so there is one token and
one **read-only** scope for the whole system. Nothing in this module can send: the scope does
not allow it, and invariant 2 says it never will.

Two directions:
- `find_sent_thread` looks *backwards* at Sent mail to learn which thread an application the
  human sent by hand actually lives in. The read-only scope covers Sent, which is what makes
  reliable threading possible without the system ever touching the outbox.
- `fetch_recent` reads incoming mail so it can be matched and classified.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, NamedTuple

from funnel.adapters.gmail import get_credentials

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Replies quote the whole history. Everything from the first quote marker on is the human's
#: own letter coming back, which is noise for the classifier and pure cost in tokens.
_QUOTE_MARKERS = (
    "\n-----Original Message-----",
    "\n________________________________",
    "\nOn ",  # "On Mon, 1 Jan 2026 at 10:00, X wrote:"
    "\nWrote:",
    "\n> ",
)
#: Hard ceiling after quote-stripping. A legitimate reply is short; anything longer is
#: signatures, legal footers and disclaimers.
_MAX_BODY_CHARS = 4000
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t]*")


class IncomingMessage(NamedTuple):
    """One email pulled from the mailbox, normalized down to what matching and the LLM need."""

    gmail_message_id: str
    thread_id: str
    from_address: str
    subject: str
    body: str
    received_at: datetime | None


def build_service() -> Any:
    """A Gmail API client on the shared read-only credentials."""
    from googleapiclient.discovery import build

    return build(
        "gmail", "v1", credentials=get_credentials(interactive=False), cache_discovery=False
    )


def _plain_text(msg: EmailMessage) -> str:
    """Prefer text/plain; fall back to crudely de-tagged HTML."""
    html = ""
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type == "text/plain":
            return str(part.get_content())
        if content_type == "text/html" and not html:
            html = str(part.get_content())
    return _TAG.sub(" ", html) if html else ""


def strip_quoted(body: str) -> str:
    """Cut the quoted history and clamp the length."""
    cut = len(body)
    for marker in _QUOTE_MARKERS:
        found = body.find(marker)
        if found != -1:
            cut = min(cut, found)
    trimmed = _WS.sub("\n", body[:cut]).strip()
    return trimmed[:_MAX_BODY_CHARS]


def parse_message(payload: dict[str, Any]) -> IncomingMessage:
    """Turn one Gmail `format=raw` response into an IncomingMessage."""
    raw = base64.urlsafe_b64decode(payload["raw"])
    msg = message_from_bytes(raw, policy=policy.default)
    received: datetime | None = None
    if internal := payload.get("internalDate"):
        received = datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
    body = _plain_text(msg) if isinstance(msg, EmailMessage) else ""
    return IncomingMessage(
        gmail_message_id=str(payload["id"]),
        thread_id=str(payload.get("threadId", "")),
        from_address=str(msg.get("From", "")),
        subject=str(msg.get("Subject", "")),
        body=strip_quoted(body),
        received_at=received,
    )


def _iter_messages(service: Any, query: str, max_results: int) -> Iterator[dict[str, Any]]:
    messages = service.users().messages()
    listed = messages.list(userId="me", q=query, maxResults=max_results).execute()
    for meta in listed.get("messages", []):
        yield messages.get(userId="me", id=meta["id"], format="raw").execute()


def fetch_recent(service: Any, *, days: int, max_results: int = 100) -> list[IncomingMessage]:
    """Incoming mail from the last `days`, excluding our own alert traffic.

    `-category:promotions` and the alert senders are dropped because job-board alerts arrive in
    the same mailbox and would otherwise be classified as replies to applications.
    """
    query = (
        f"newer_than:{days}d in:inbox -category:promotions "
        "-from:linkedin.com -from:hh.ru -from:career.habr.com "
        "-from:wellfound.com -from:glassdoor.com -from:indeed.com"
    )
    return [parse_message(payload) for payload in _iter_messages(service, query, max_results)]


def find_sent_thread(
    service: Any, *, company: str, sent_at: datetime, window_days: int = 3
) -> str | None:
    """The thread id of the application the human sent to `company`, if it can be pinned down.

    Searches Sent mail in a window around `sent_at` for the company name. Returns None unless
    exactly one thread matches — an ambiguous guess here would mis-route every later reply in
    that thread, so it is better to leave the application unlinked and fall back to domain
    matching.
    """
    start = (sent_at - timedelta(days=1)).strftime("%Y/%m/%d")
    end = (sent_at + timedelta(days=window_days)).strftime("%Y/%m/%d")
    escaped = company.replace('"', "")
    query = f'in:sent after:{start} before:{end} "{escaped}"'
    threads = {str(payload.get("threadId", "")) for payload in _iter_messages(service, query, 10)}
    threads.discard("")
    return threads.pop() if len(threads) == 1 else None
