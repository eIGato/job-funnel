"""Adapter: job alerts parsed out of Gmail (Phase 3).

We do not scrape LinkedIn (invariant 1). Instead we turn on saved-search email alerts
and read those messages through the Gmail API. The same trick works for any board that
can send alerts.

One Gmail source, several senders. `fetch()` pulls the messages matched by the source
query, and each message is dispatched to the parser for its sender's board. The pipeline
never learns which boards these are — that lives here, behind the `gmail-alerts` name.

The parsers are deliberately structural, not textual: they key on the job-posting links
(hh `/vacancy/<id>`, habr `/vacancies/<id>`, LinkedIn `/jobs/view/<id>`) and the card body
around them, never on a subject line or a greeting. A board's wording changes between the
first "your alert has been created" email and the later "new jobs" ones; the link shapes
and the per-card labels do not.
"""

from __future__ import annotations

import base64
import re
import stat
from email import message_from_bytes, policy
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import parse_qs, urlparse

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, strip_html
from funnel.schemas import NormalizedJob

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from google.oauth2.credentials import Credentials

#: Read-only on purpose. This system cannot send mail and never will, so a write scope
#: would be strictly more authority than it has any use for.
GMAIL_SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_credentials(*, interactive: bool = False) -> Credentials:
    """Return valid Gmail OAuth credentials, refreshing or minting them as needed.

    - A saved token is loaded and, if expired, refreshed silently and rewritten.
    - With no usable token and ``interactive=True``, run the installed-app browser flow
      once (this is what ``funnel auth-gmail`` does) and persist the result.
    - With no usable token and ``interactive=False`` (the pipeline path), raise with a
      clear pointer to ``funnel auth-gmail`` rather than trying to open a browser from a
      systemd run.

    Imports of the Google libraries are local so that merely importing the adapter
    registry stays cheap.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    from funnel.config import get_settings

    settings = get_settings()
    creds_path: Path = settings.gmail_credentials_path
    token_path: Path = settings.gmail_token_path

    creds: Credentials | None = None
    if token_path.exists():
        # google-auth ships py.typed but leaves these methods unannotated.
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)  # type: ignore[no-untyped-call]

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())  # type: ignore[no-untyped-call]
        _write_token(token_path, creds)
        return creds

    if not interactive:
        raise RuntimeError(
            f"No usable Gmail token at {token_path}. Run `uv run funnel auth-gmail` once "
            "to authorize (it opens a browser); the pipeline stays non-interactive."
        )

    if not creds_path.exists():
        raise FileNotFoundError(
            f"Missing OAuth client secret at {creds_path}. Download a Desktop-app OAuth "
            "client from Google Cloud Console (Gmail API enabled) and save it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)
    _write_token(token_path, creds)
    return creds


def _write_token(token_path: Path, creds: Credentials) -> None:
    """Persist the token with owner-only permissions (it holds a refresh token)."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")  # type: ignore[no-untyped-call]
    token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


# --------------------------------------------------------------------------------------
# Parsing: pure functions over (sender, html). No network, no OAuth — so the tests below
# run against committed .eml fixtures.
# --------------------------------------------------------------------------------------


class _Card(NamedTuple):
    """A single posting pulled from an alert: the title anchor plus its trailing lines."""

    title: str
    external_id: str
    url: str
    body: list[str]


class _AnchorCollector(HTMLParser):
    """Collect ``(href, inner_text)`` for every <a> in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._buf: list[str] = []
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.anchors.append((self._href, " ".join("".join(self._buf).split())))
            self._href = None


def _anchors(html: str) -> list[tuple[str, str]]:
    collector = _AnchorCollector()
    collector.feed(html)
    return collector.anchors


def _lines(html: str) -> list[str]:
    return [" ".join(line.split()) for line in strip_html(html).splitlines() if line.strip()]


def _build_cards(html: str, titles: list[_Card]) -> list[_Card]:
    """Attach each posting's trailing text to its title.

    ``titles`` carries the (title, id, url) triples found on the job links, in document
    order. The rendered text is walked in the same order: a line equal to the next
    expected title opens a new card, and the lines until the following title become its
    body. A title that never shows up as a line is simply dropped — better than guessing.
    """
    order = iter(titles)
    pending = next(order, None)
    cards: list[_Card] = []
    current: _Card | None = None
    for line in _lines(html):
        if pending is not None and line == pending.title:
            current = _Card(pending.title, pending.external_id, pending.url, [])
            cards.append(current)
            pending = next(order, None)
        elif current is not None:
            current.body.append(line)
    return cards


_HH_VACANCY = re.compile(r"/vacancy/(\d+)")
_HH_SALARY = re.compile(
    r"^(от|до)\s|^[\d\s.,]+[₽$€]?$|^[₽$€]$|^за\s+(месяц|год|час|день|смену)$", re.IGNORECASE
)
_HH_REMOTE = "Можно удал"
_HH_VIEW = "Посмотреть вакансию"


def _parse_hh(html: str) -> list[NormalizedJob]:
    """hh.ru alerts: each card is a title link, then salary/company/location lines."""
    titles = [
        _Card(text, m.group(1), f"https://hh.ru/vacancy/{m.group(1)}", [])
        for href, text in _anchors(html)
        if (m := _HH_VACANCY.search(href)) and text and text != _HH_VIEW
    ]
    jobs: list[NormalizedJob] = []
    for card in _build_cards(html, titles):
        company: str | None = None
        location: str | None = None
        remote = False
        for line in card.body:
            if line == _HH_VIEW:
                break  # the "view" button ends the card
            if _HH_REMOTE in line:
                remote = True
            elif _HH_SALARY.match(line):
                continue
            elif company is None:
                head, _, rest = line.partition(",")
                company = head.strip()
                location = rest.strip() or None
        if company:
            jobs.append(
                NormalizedJob(
                    url=card.url,
                    company=company,
                    title=card.title,
                    location=location,
                    is_remote=remote,
                    external_id=card.external_id,
                )
            )
    return jobs


_HABR_VACANCY = re.compile(r"/vacancies/(\d+)")


def _habr_destination(href: str) -> str:
    """Habr wraps every link in an email-tracking redirect; the target is the `url` param."""
    return parse_qs(urlparse(href.replace("&amp;", "&")).query).get("url", [""])[0]


def _parse_habr(html: str) -> list[NormalizedJob]:
    """Habr Career alerts: labelled fields (Компания / Город / Дополнительно / навыки)."""
    titles = [
        _Card(text, m.group(1), f"https://career.habr.com/vacancies/{m.group(1)}", [])
        for href, text in _anchors(html)
        if (m := _HABR_VACANCY.search(_habr_destination(href))) and text
    ]
    jobs: list[NormalizedJob] = []
    for card in _build_cards(html, titles):
        company: str | None = None
        location: str | None = None
        skills = ""
        remote = False
        for line in card.body:
            label, _, value = line.partition(":")
            value = value.strip()
            if label == "Компания":
                company = value
            elif label in ("Город", "Города"):
                location = value or None
            elif label == "Дополнительно":
                remote = _HH_REMOTE in value
            elif label == "Требуемые навыки":
                skills = value
        if company:
            jobs.append(
                NormalizedJob(
                    url=card.url,
                    company=company,
                    title=card.title,
                    description=clip(skills),
                    location=location,
                    is_remote=remote,
                    external_id=card.external_id,
                )
            )
    return jobs


_LI_VIEW = re.compile(r"/jobs/view/(\d+)")
#: Status/chrome lines LinkedIn drops under a posting; never the "Company · Location" line.
_LI_NOISE = re.compile(
    r"actively recruiting|school alum|be an early applicant|easy apply|"
    r"promoted|viewed|your profile|response",
    re.IGNORECASE,
)


def _parse_linkedin(html: str) -> list[NormalizedJob]:
    """LinkedIn job-alert emails: title link, then a "Company · Location" line."""
    seen: set[str] = set()
    titles: list[_Card] = []
    for href, text in _anchors(html):
        m = _LI_VIEW.search(href)
        if m and text and m.group(1) not in seen:
            seen.add(m.group(1))
            titles.append(
                _Card(text, m.group(1), f"https://www.linkedin.com/jobs/view/{m.group(1)}", [])
            )
    jobs: list[NormalizedJob] = []
    for card in _build_cards(html, titles):
        company: str | None = None
        location: str | None = None
        for line in card.body:
            if _LI_NOISE.search(line):
                continue
            if " · " in line:
                company, _, location = (part.strip() for part in line.partition(" · "))
                break
        if company:
            jobs.append(
                NormalizedJob(
                    url=card.url,
                    company=company,
                    title=card.title,
                    location=location,
                    is_remote=bool(location and "remote" in location.lower()),
                    external_id=card.external_id,
                )
            )
    return jobs


#: Sender-domain substring -> parser. First match wins; unknown senders yield nothing.
_PARSERS: tuple[tuple[str, Callable[[str], list[NormalizedJob]]], ...] = (
    ("hh.ru", _parse_hh),
    ("career.habr.com", _parse_habr),
    ("linkedin.com", _parse_linkedin),
)


def parse_message(sender: str, html: str) -> list[NormalizedJob]:
    """Dispatch one alert email to the parser for its board. Unknown sender -> []."""
    low = sender.lower()
    for needle, parser in _PARSERS:
        if needle in low:
            return parser(html)
    return []


def _sender_and_html(msg: EmailMessage) -> tuple[str, str]:
    """Pull the From header and the text/html body out of a parsed email."""
    sender = str(msg.get("From", ""))
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return sender, part.get_content()
    return sender, ""


def parse_raw_email(raw: bytes) -> list[NormalizedJob]:
    """Parse one raw RFC-822 message (as Gmail returns with format='raw')."""
    msg = message_from_bytes(raw, policy=policy.default)
    if not isinstance(msg, EmailMessage):  # pragma: no cover - default policy yields EmailMessage
        return []
    sender, html = _sender_and_html(msg)
    return parse_message(sender, html) if html else []


@register
class GmailAlertsAdapter(BaseAdapter):
    """Reads alert emails and extracts job postings from them.

    Expected config keys (Source.config JSONB):
      query: str, a Gmail search query spanning every board that emails alerts, e.g.
             'newer_than:2d (from:hh.ru OR from:career.habr.com OR from:linkedin.com)'
      max_results: int, cap on messages pulled per run.
    """

    name = "gmail-alerts"

    async def fetch(self) -> list[NormalizedJob]:
        from googleapiclient.discovery import build

        query = str(self.config.get("query", ""))
        max_results = int(self.config.get("max_results", 50))

        creds = get_credentials(interactive=False)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        messages = service.users().messages()

        listed = messages.list(userId="me", q=query, maxResults=max_results).execute()
        jobs: list[NormalizedJob] = []
        for meta in listed.get("messages", []):
            raw = messages.get(userId="me", id=meta["id"], format="raw").execute()
            jobs.extend(parse_raw_email(base64.urlsafe_b64decode(raw["raw"])))
        return jobs
