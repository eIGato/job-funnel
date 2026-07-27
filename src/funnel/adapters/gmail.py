"""Adapter: job alerts parsed out of Gmail (Phase 3).

We do not scrape LinkedIn (invariant 1). Instead we turn on saved-search email alerts
and read those messages through the Gmail API. The same trick works for any board that
can send alerts.

One Gmail source, several senders. `fetch()` pulls the messages matched by the source
query, and each message is dispatched to the parser for its sender's board. The pipeline
never learns which boards these are — that lives here, behind the `gmail-alerts` name.

The parsers are deliberately structural, not textual: they key on the job-posting links
(hh `/vacancy/<id>`, habr `/vacancies/<id>`, LinkedIn `/jobs/view/<id>`, Glassdoor's
`jobListingId`, Indeed's `jk`) and the card body around them, never on a subject line or a
greeting. A board's wording changes between the first "your alert has been created" email and
the later "new jobs" ones; the link shapes and the per-card labels do not. Indeed pushes this
further: its cards are read by counting in from both ends, because the middle lines are
localized to whichever country site sent the alert.

Most boards are parsed from the `text/html` part. Wellfound and Indeed are the exceptions and
read `text/plain`: Wellfound's HTML wraps every posting link in an opaque
`links.wellfound.com/s/c/...` tracking redirect that carries no job id, while the plain-text
alternative keeps the real `wellfound.com/jobs?job_listing_slug=<id>` URLs; Indeed's plain-text
cards carry a snippet the HTML buries in table chrome. So a parser is handed both bodies
(`_Alert`) and reads whichever one carries a stable id.

Several boards (Habr, Landing.Jobs) hide the posting URL behind a per-recipient click
redirect, and Indeed and Glassdoor hang tracking tokens off theirs. In every one of those the
stored URL is rebuilt from the id alone (`_redirect_target`, or a canonical built from the
id), so nothing personal to this mailbox ends up in the database.
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


class _Alert(NamedTuple):
    """The two rendered bodies of one alert email; a parser reads whichever it needs."""

    html: str
    text: str


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


def _parse_hh(alert: _Alert) -> list[NormalizedJob]:
    """hh.ru alerts: each card is a title link, then salary/company/location lines."""
    html = alert.html
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


def _redirect_target(href: str) -> str:
    """The destination behind an email-tracking redirect: its `url` query param, decoded.

    Habr and Landing.Jobs both wrap every link this way, and in both the real posting URL is
    the only place a stable id survives. A link that is not such a redirect yields "".
    """
    return parse_qs(urlparse(href.replace("&amp;", "&")).query).get("url", [""])[0]


def _parse_habr(alert: _Alert) -> list[NormalizedJob]:
    """Habr Career alerts: labelled fields (Компания / Город / Дополнительно / навыки)."""
    html = alert.html
    titles = [
        _Card(text, m.group(1), f"https://career.habr.com/vacancies/{m.group(1)}", [])
        for href, text in _anchors(html)
        if (m := _HABR_VACANCY.search(_redirect_target(href))) and text
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


def _parse_linkedin(alert: _Alert) -> list[NormalizedJob]:
    """LinkedIn job-alert emails: title link, then a "Company · Location" line."""
    html = alert.html
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


def _blocks(text: str) -> list[list[str]]:
    """Blank-line-separated blocks of a plain-text body, each a list of collapsed lines.

    The blank line is the one piece of structure a text/plain alert always has. What a block
    means differs per board: for Indeed it is one whole posting card (so the lines matter,
    below), for Wellfound one field of a card (so the lines are joined back up).
    """
    blocks: list[list[str]] = []
    buffer: list[str] = []
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if stripped:
            buffer.append(stripped)
        elif buffer:
            blocks.append(buffer)
            buffer = []
    if buffer:
        blocks.append(buffer)
    return blocks


def _paragraphs(text: str) -> list[str]:
    """Blank-line-separated paragraphs, each with its soft-wrapped lines joined into one.

    A plain-text alert wraps a field (Wellfound's location can be a 60-city list) across many
    physical lines but separates real fields with a blank line. Collapsing to paragraphs turns
    those wraps back into one logical value per field.
    """
    return [" ".join(block) for block in _blocks(text)]


#: A Wellfound card's "Company / 11-50 Employees" line — the structural anchor of each posting.
_WF_COMPANY = re.compile(r"^(?P<name>.+?) / .+ Employees$")
#: The real posting link, present only in the text/plain body (the HTML link is opaque).
_WF_JOB = re.compile(r"wellfound\.com/jobs\?job_listing_slug=(\d+)-[^\s>|]*")
#: Non-place segments of the "salary | location | exp | type" line.
_WF_JOBTYPE = frozenset({"Full-time", "Part-time", "Contract", "Internship"})


def _parse_wellfound(alert: _Alert) -> list[NormalizedJob]:
    """Wellfound alerts, from the text/plain body: title, "Company / size", "… location …".

    Each posting is a run of paragraphs anchored on its "Company / N Employees" line: the
    paragraph before it is the title, the one after is the "salary | location | exp | type"
    line, and the next `job_listing_slug` URL is the posting's stable link and id.
    """
    paras = _paragraphs(alert.text)
    jobs: list[NormalizedJob] = []
    for i, para in enumerate(paras):
        company_match = _WF_COMPANY.match(para)
        if not company_match or i == 0:
            continue
        job_match = next((m for p in paras[i + 1 :] if (m := _WF_JOB.search(p))), None)
        if job_match is None:
            continue
        location_line = paras[i + 1] if i + 1 < len(paras) else ""
        segments = [seg.strip() for seg in location_line.split("|")]
        remote = any("remote" in seg.lower() for seg in segments)
        places = [
            seg
            for seg in segments
            if seg and seg[0] not in "$€£" and "years of exp" not in seg and seg not in _WF_JOBTYPE
        ]
        location = re.sub(r"^Remote only,?\s*", "", ", ".join(places)).strip() or None
        jobs.append(
            NormalizedJob(
                url="https://" + job_match.group(0),
                company=company_match.group("name").strip(),
                title=paras[i - 1],
                location=location,
                is_remote=remote,
                external_id=job_match.group(1),
            )
        )
    return jobs


#: Each Glassdoor posting is one <a> wrapping the whole card; the id rides in the href.
_GD_ANCHOR = re.compile(r"<a\b[^>]*jobListing\.htm[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)
_GD_HREF = re.compile(r'href="([^"]*)"', re.IGNORECASE)
_GD_JID = re.compile(r"jobListingId=(\d+)")
#: A trailing " 4.2 ★" employer rating hangs off the company name; drop it.
_GD_RATING = re.compile(r"\s+\d(?:\.\d)?\s*★.*$")
#: Chrome lines under the location (salary estimate, apply badge, posting age, footer CTAs).
_GD_NOISE = re.compile(
    r"Employer est\.|Easy Apply|Just posted|^\d+[hdwm]$|See more jobs|Want more", re.IGNORECASE
)


def _parse_glassdoor(alert: _Alert) -> list[NormalizedJob]:
    """Glassdoor alerts: each posting is a single <a> card of company / title / location lines."""
    jobs: list[NormalizedJob] = []
    for anchor in _GD_ANCHOR.finditer(alert.html):
        href_match = _GD_HREF.search(anchor.group(0))
        id_match = _GD_JID.search(href_match.group(1)) if href_match else None
        lines = [line for line in strip_html(anchor.group(1)).splitlines() if line.strip()]
        if id_match is None or len(lines) < 2:
            continue
        company = _GD_RATING.sub("", lines[0]).strip()
        title = lines[1].strip()
        location = next((line.strip() for line in lines[2:] if not _GD_NOISE.search(line)), None)
        if not company or not title:
            continue
        jobs.append(
            NormalizedJob(
                url=f"https://www.glassdoor.com/job-listing/j?jl={id_match.group(1)}",
                company=company,
                title=title,
                location=location,
                is_remote="remote" in f"{title} {location or ''}".lower(),
                external_id=id_match.group(1),
            )
        )
    return jobs


#: The job key on an Indeed posting link, with the country host it was served from. Every
#: link in the mail is a per-recipient tracking URL ("personalisierte, sichere Links" says
#: the footer), so only the key is kept and the stored URL is rebuilt from it.
_INDEED_JK = re.compile(r"https?://([\w.-]*indeed\.com)/\S*[?&]jk=([0-9a-f]+)")
#: Remote wording Indeed puts in the title or the location, across its country sites.
_INDEED_REMOTE = re.compile(r"remote|home\s?office|telearbeit", re.IGNORECASE)


def _parse_indeed(alert: _Alert) -> list[NormalizedJob]:
    """Indeed alerts, from the text/plain body: one blank-line-separated block per posting.

    A card is fixed at both ends — title, then "Company - Location" at the head; snippet,
    posting age, link at the tail — with a variable middle (salary estimate, "Easy Apply",
    employer badges). So the fields are counted in from the ends rather than matched by
    label: the labels are localized to whichever country site sent the alert (this one is
    de.indeed.com, in German), the card shape is not.
    """
    jobs: list[NormalizedJob] = []
    for block in _blocks(alert.text):
        # Under five lines there is no room for the full head+tail, so the count would be
        # reading the company line as the snippet. Skip rather than guess.
        if len(block) < 5 or (m := _INDEED_JK.search(block[-1])) is None:
            continue
        head, separator, tail = block[1].rpartition(" - ")
        company, location = (head, tail) if separator else (tail, "")
        if not company:
            continue
        jobs.append(
            NormalizedJob(
                url=f"https://{m.group(1)}/viewjob?jk={m.group(2)}",
                company=company,
                title=block[0],
                description=clip(block[-3]),
                location=location or None,
                is_remote=bool(_INDEED_REMOTE.search(f"{block[0]} {location}")),
                external_id=m.group(2),
            )
        )
    return jobs


#: A Landing.Jobs posting path, `/at/<company-slug>/<job-slug>`: the only stable id in the
#: alert, since every href is a per-recipient `ahoy` click redirect (see `_redirect_target`).
_LJ_JOB = re.compile(r"^https?://(?:www\.)?landing\.jobs(/at/[^/?#]+/[^/?#]+)")


def _parse_landing_jobs(alert: _Alert) -> list[NormalizedJob]:
    """Landing.Jobs alerts: a bare list of "Title @ Company" links, grouped by subscription.

    There is no card here — the anchor text is the whole posting, so no location or snippet
    is available and the job link carries everything we get. The subscription grouping above
    the links is the human's own filter names, not posting data, and is ignored.
    """
    jobs: list[NormalizedJob] = []
    seen: set[str] = set()
    for href, text in _anchors(alert.html):
        match = _LJ_JOB.match(_redirect_target(href))
        title, separator, company = text.rpartition(" @ ")
        if match is None or not separator or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        jobs.append(
            NormalizedJob(
                url=f"https://landing.jobs{match.group(1)}",
                company=company.strip(),
                title=title.strip(),
                is_remote="remote" in title.lower(),
                external_id=match.group(1).removeprefix("/at/"),
            )
        )
    return jobs


#: Sender-domain substring -> parser. First match wins; unknown senders yield nothing.
_PARSERS: tuple[tuple[str, Callable[[_Alert], list[NormalizedJob]]], ...] = (
    ("hh.ru", _parse_hh),
    ("career.habr.com", _parse_habr),
    ("linkedin.com", _parse_linkedin),
    ("wellfound.com", _parse_wellfound),
    ("glassdoor.com", _parse_glassdoor),
    ("indeed.com", _parse_indeed),
    ("landing.jobs", _parse_landing_jobs),
)


def parse_message(sender: str, html: str, text: str = "") -> list[NormalizedJob]:
    """Dispatch one alert email to the parser for its board. Unknown sender -> []."""
    alert = _Alert(html=html, text=text)
    low = sender.lower()
    for needle, parser in _PARSERS:
        if needle in low:
            return parser(alert)
    return []


def _sender_and_bodies(msg: EmailMessage) -> tuple[str, str, str]:
    """Pull the From header and the text/html and text/plain bodies out of a parsed email."""
    sender = str(msg.get("From", ""))
    html = text = ""
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type == "text/html" and not html:
            html = part.get_content()
        elif content_type == "text/plain" and not text:
            text = part.get_content()
    return sender, html, text


def parse_raw_email(raw: bytes) -> list[NormalizedJob]:
    """Parse one raw RFC-822 message (as Gmail returns with format='raw')."""
    msg = message_from_bytes(raw, policy=policy.default)
    if not isinstance(msg, EmailMessage):  # pragma: no cover - default policy yields EmailMessage
        return []
    sender, html, text = _sender_and_bodies(msg)
    return parse_message(sender, html, text) if (html or text) else []


@register
class GmailAlertsAdapter(BaseAdapter):
    """Reads alert emails and extracts job postings from them.

    Expected config keys (Source.config JSONB):
      query: str, a Gmail search query spanning every board that emails alerts, e.g.
             'newer_than:2d (from:hh.ru OR from:career.habr.com OR from:linkedin.com
              OR from:wellfound.com OR from:glassdoor.com OR from:indeed.com
              OR from:landing.jobs)'
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
