"""hh.ru's per-application notifications: what they say, and which application they are about.

hh mails everything from `noreply@hh.ru` — the saved-search alerts the ingest parser reads, and
the human's own conversations with employers — which is why `inbox.fetch_recent` leaves the
whole domain out and why none of this mail ever reached `check-replies`. On 2026-10-19, 193 of
the 202 hh messages left in the inbox that no parser could read were these:

- **Rejections** ("Работодатель не готов пригласить вас", 67). They name the vacancy by its hh id
  in a link (`hh.ru/vacancy/<id>`), which is the same id the alert parser stores in `Job.url` —
  an exact key, not a company name read out of prose. A match is therefore conclusive.
- **Chat notices** ("Новое сообщение от работодателя" / "Новое сообщение в чате", 108). They
  carry the chat message and a `chat_id`, and neither the employer nor the vacancy, so nothing
  in the table can be matched to them. The human reads hh chats on the site (and means to
  automate that through the browser), so the email is only a doorbell.
- **"Вам написали по вакансии X"** (20 in the inbox on 2026-10-19): the employer's message, the
  company and the vacancy id. A real reply, matched by vacancy id and classified like any other.
  The alert parser read one posting out of each — the vacancy link — so before the query
  excluded these subjects, `GMAIL_TRASH_PARSED_ALERTS` would have Trashed every one of them.
- **"Вакансия X: вам написали из Y"**, the older shape, which names the employer only. Matched
  by company name against hh applications, and kept when one matches.

Pure parsing here, plus the one Gmail read; the decisions are in `replies.scan`.
"""

from __future__ import annotations

import base64
import enum
import re
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, NamedTuple

from funnel.adapters.gmail import execute
from funnel.adapters.util import strip_html

if TYPE_CHECKING:
    from collections.abc import Iterator


class NoticeKind(enum.StrEnum):
    REJECTION = "rejection"
    CHAT = "chat"
    MESSAGE_FROM = "message_from"
    #: "Вам написали по вакансии X": the employer's message *and* the vacancy id. A real reply.
    VACANCY_MESSAGE = "vacancy_message"


class Notice(NamedTuple):
    gmail_message_id: str
    thread_id: str
    kind: NoticeKind
    subject: str
    body: str
    #: The hh vacancy id, from a rejection's link. None for the other kinds.
    vacancy_id: str | None
    #: The employer as hh names it, when it does.
    company: str | None
    received_at: datetime | None


#: The subjects that select these mails, here and — negated — in the `gmail-alerts` query in
#: `seeds.py`, so the alert parser and its Trash never see a personal message. A subject is
#: what tells hh's mail types apart: the sender is one address for all of them. Order matters:
#: "Вам написали по вакансии" must be tried before the looser "вам написали из".
SUBJECTS = {
    "Работодатель не готов пригласить вас": NoticeKind.REJECTION,
    "Новое сообщение от работодателя": NoticeKind.CHAT,
    "Новое сообщение в чате": NoticeKind.CHAT,
    # After the chat subject it is a substring of. Same layout as "Вам написали по вакансии":
    # 3 of them, for applications 1519, 1521 and 1523, were Trashed as parsed alerts before
    # these subjects left the alert query (restored 2026-10-19).
    "Сообщение от работодателя": NoticeKind.VACANCY_MESSAGE,
    "Вам написали по вакансии": NoticeKind.VACANCY_MESSAGE,
    "вам написали из": NoticeKind.MESSAGE_FROM,
}
QUERY = "in:inbox from:noreply@hh.ru (" + " OR ".join(f'subject:"{s}"' for s in SUBJECTS) + ")"
#: The kinds that name their vacancy by id, i.e. that can be matched exactly.
_BY_VACANCY = frozenset({NoticeKind.REJECTION, NoticeKind.VACANCY_MESSAGE})

_VACANCY = re.compile(r"hh\.ru/vacancy/(\d+)")
_COMPANY_LINE = re.compile(r"^Компани[яи]:\s*(.+)$", re.MULTILINE)
_WROTE_FROM = re.compile(r'вам написали из\s+"?([^"]+)"?\s*$')
_STYLE = re.compile(r"(?is)<(style|head)\b.*?</\1>")
_BLANK = re.compile(r"\n\s*\n+")


def kind_of(subject: str) -> NoticeKind | None:
    for marker, kind in SUBJECTS.items():
        if marker.casefold() in subject.casefold():
            return kind
    return None


def _html(msg: EmailMessage) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return str(part.get_content())
    return ""


def parse(
    gmail_message_id: str, thread_id: str, raw: bytes, received_at: datetime | None = None
) -> Notice | None:
    """One hh notification, or None when the subject is none of the kinds above."""
    msg = message_from_bytes(raw, policy=policy.default)
    subject = str(msg.get("Subject", ""))
    kind = kind_of(subject)
    if kind is None or not isinstance(msg, EmailMessage):
        return None
    html = _html(msg)
    body = _BLANK.sub("\n", strip_html(_STYLE.sub(" ", html))).strip()
    vacancy = _VACANCY.search(html) if kind in _BY_VACANCY else None
    company: str | None = None
    if (kind in _BY_VACANCY and (found := _COMPANY_LINE.search(body))) or (
        kind is NoticeKind.MESSAGE_FROM and (found := _WROTE_FROM.search(subject))
    ):
        company = found.group(1).strip().rstrip(".").strip()
    return Notice(
        gmail_message_id=gmail_message_id,
        thread_id=thread_id,
        kind=kind,
        subject=subject,
        body=body[:4000],
        vacancy_id=vacancy.group(1) if vacancy else None,
        company=company,
        received_at=received_at,
    )


def fetch(service: Any, *, max_results: int = 200) -> Iterator[Notice]:
    """hh notifications still in the inbox. Gmail I/O only."""
    messages = service.users().messages()
    listed = execute(messages.list(userId="me", q=QUERY, maxResults=max_results))
    for meta in listed.get("messages", []):
        payload = execute(messages.get(userId="me", id=meta["id"], format="raw"))
        internal = payload.get("internalDate")
        notice = parse(
            str(payload["id"]),
            str(payload.get("threadId", "")),
            base64.urlsafe_b64decode(payload["raw"]),
            datetime.fromtimestamp(int(internal) / 1000, tz=UTC) if internal else None,
        )
        if notice is not None:
            yield notice


__all__ = ["QUERY", "SUBJECTS", "Notice", "NoticeKind", "fetch", "kind_of", "parse"]
