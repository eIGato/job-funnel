"""Adapter: job alerts parsed out of Gmail (Phase 3).

We do not scrape LinkedIn (invariant 1). Instead we turn on saved-search email alerts
and read those messages through the Gmail API. The same trick works for any board that
can send alerts.

One Gmail source, several senders. `fetch()` pulls the messages matched by the source
query, and each message is dispatched to the parser for its sender's board. The pipeline
never learns which boards these are — that lives here, behind the `gmail-alerts` name.

The parsers are deliberately structural, not textual: they key on the job-posting links
(hh `/vacancy/<id>`, habr `/vacancies/<id>`, LinkedIn `/jobs/view/<id>`, Glassdoor's
`jobListingId`, Indeed's `jk`, justjoin's `/job-offer/<slug>`, pracuj's `,oferta,<id>`) and the
card body around them, never on a subject line or a greeting. A board's wording changes between
the first "your alert has been created" email and the later "new jobs" ones; the link shapes and
the per-card labels do not. Indeed pushes this further: its cards are read by counting in from
both ends, because the middle lines are localized to whichever country site sent the alert;
justjoin needs the same treatment for the same reason, since it mails the identical card layout
in English from one address and in Polish from another.

Most boards are parsed from the `text/html` part. Wellfound and Indeed are the exceptions and
read `text/plain`: Wellfound's HTML wraps every posting link in an opaque
`links.wellfound.com/s/c/...` tracking redirect that carries no job id, while the plain-text
alternative keeps the real `wellfound.com/jobs?job_listing_slug=<id>` URLs; Indeed's plain-text
cards carry a snippet the HTML buries in table chrome. So a parser is handed both bodies
(`_Alert`) and reads whichever one carries a stable id.

Several boards (Habr, Landing.Jobs) hide the posting URL behind a per-recipient click
redirect, and Indeed, Glassdoor, justjoin and pracuj hang tracking tokens off theirs. In every
one of those the stored URL is rebuilt from the id alone (`_redirect_target`, or a canonical
built from the id), so nothing personal to this mailbox ends up in the database.

Boards that were surveyed and deliberately have **no** parser (mailbox sweep, 2026-08-17), for
two different reasons — which is the difference between the two Gmail queries in `seeds.py`:

*Nothing to read.* Reed (23 msgs/yr), Totaljobs (23), 24recruitment (9), Indeed's
`match.indeed.com` (8) and spelljob (2) wrap every posting in an opaque per-recipient redirect
(`clicks.reed.co.uk/f/a/…`, `click.totaljobsmail.com/f/a/…`, `cts.indeed.com/v3/<blob>`) with no
id in it anywhere and no plain-text alternative that carries one. Ingesting them would mint a
fresh row and a fresh cover letter on every run, which is the Adzuna `se=` bug (CLAUDE.md,
"Dedup at the door"). Resolving the redirect means an HTTP call per posting inside the alert
parser; the one Reed link tried landed on a 404 under "Appcast Enterprise", an ad network. These
are **not** duplicates — 34 of 47 Totaljobs postings measured were new — so they stay in the
inbox for a human to glance at, and the honest fix is to unsubscribe, not to have a batch job
sweep them up forever.

*Nothing we do not already have.* These are in `discard_query` and get Trashed unread:

- **Adzuna's own alerts (34/yr)** — `adzuna.com/land/ad/<id>` does carry a stable id, but that
  host is in `matching/apply_route.BLOCKED_HOSTS` (403 from where the human lives) and Adzuna is
  already an API source for eight countries. `adzuna.nl` (5) is marketing with no postings at
  all.
- **WeWorkRemotely's digest (2)** — already an RSS source.
- **getmatch.ru (4)** — a weekly digest of postings the human has already seen during the week
  in the Telegram bot the same subscription feeds, where applying is one click (human, 2026-08-17).
  A parser was written and removed the same day; the postings were real, they were just not new.
- **`info@glassdoor.com` (2)** — Glassdoor's marketing address. `noreply@glassdoor.com` is the
  alert address and is parsed; naming the address rather than the domain is what keeps the two
  apart (CLAUDE.md, "A board is an address, not a domain").
"""

from __future__ import annotations

import base64
import re
import stat
from email import message_from_bytes, policy
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import parse_qs, urlparse

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, looks_remote, strip_html
from funnel.schemas import NormalizedJob

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from google.oauth2.credentials import Credentials

#: Read-only is the default and covers everything the pipeline reads: alert mail, Sent mail,
#: replies. `gmail.modify` is asked for only when the human turns on trashing parsed alerts
#: (`GMAIL_TRASH_PARSED_ALERTS`), because moving a message to Trash is a write and Gmail has no
#: narrower scope for it.
#:
#: Neither scope can send: `gmail.send`, `gmail.compose` and `gmail.insert` are separate scopes
#: and are never requested. That is what invariant 2 is actually about — the system has no way
#: to put mail into the world, whatever it may do to mail already in the mailbox. Full access
#: (`https://mail.google.com/`) is never requested either, so the worst this system can do to
#: an email is recoverable: `modify` cannot delete permanently.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def gmail_scopes(*, allow_trash: bool | None = None) -> list[str]:
    """The OAuth scopes to ask for, which is the least privilege the settings allow.

    `allow_trash=None` reads the setting; the tests pass it explicitly so both states are
    checked without touching the environment.
    """
    if allow_trash is None:
        from funnel.config import get_settings

        allow_trash = get_settings().gmail_trash_parsed_alerts
    return [GMAIL_MODIFY_SCOPE] if allow_trash else [GMAIL_READONLY_SCOPE]


def get_credentials(*, interactive: bool = False) -> Credentials:
    """Return valid Gmail OAuth credentials, refreshing or minting them as needed.

    - A saved token is loaded and, if expired, refreshed silently and rewritten.
    - A token minted for narrower scopes than the settings now call for is discarded the
      same way, because the alternative is an opaque 403 at the first call that needs the
      missing scope.
    - A refresh token Google has *revoked* is discarded, not fatal: re-authorizing is the
      whole point of ``funnel auth-gmail``, and a dead token file must not be what stops it.
    - With no usable token and ``interactive=True``, run the installed-app browser flow
      once (this is what ``funnel auth-gmail`` does) and persist the result.
    - With no usable token and ``interactive=False`` (the pipeline path), raise with a
      clear pointer to ``funnel auth-gmail`` rather than trying to open a browser from a
      systemd run.

    Imports of the Google libraries are local so that merely importing the adapter
    registry stays cheap.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    from funnel.config import get_settings

    settings = get_settings()
    creds_path: Path = settings.gmail_credentials_path
    token_path: Path = settings.gmail_token_path
    scopes = gmail_scopes(allow_trash=settings.gmail_trash_parsed_alerts)

    creds: Credentials | None = None
    if token_path.exists():
        try:
            # google-auth ships py.typed but leaves these methods unannotated.
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)  # type: ignore[no-untyped-call]
        except ValueError:
            # Empty, truncated or hand-edited: `from_authorized_user_file` raises
            # "Authorized user info was not in the expected format". Same stance as the revoked
            # refresh token below — a broken token file must never block the command whose one
            # job is to replace it. Discard and re-authorize.
            creds = None

    if creds and not creds.has_scopes(scopes):  # type: ignore[no-untyped-call]
        # Minted before the settings widened (turning on trashing is the case that does it).
        # Google does not upgrade a token in place, and the missing scope surfaces as a bare
        # 403 insufficientPermissions at whichever call needs it. Same stance as a revoked
        # token: discard and send the human to `auth-gmail`.
        creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())  # type: ignore[no-untyped-call]
        except RefreshError:
            # Google revokes a refresh token on a password change, a "remove access" in the
            # account console, or six months of disuse. Before this was caught, the dead token
            # file made `auth-gmail` itself fail here — it raised out of the refresh branch and
            # never reached the browser flow below, so the one command that exists to fix a
            # broken token could not run while the broken token was on disk. Drop it and
            # re-authorize instead.
            creds = None
        else:
            _write_token(token_path, creds)
            return creds

    if not interactive:
        raise RuntimeError(
            f"No usable Gmail token at {token_path} (missing, revoked, or minted for "
            f"narrower scopes than {', '.join(scopes)}). Run `uv run funnel auth-gmail` "
            "once to re-authorize; the pipeline stays non-interactive."
        )

    if not creds_path.exists():
        raise FileNotFoundError(
            f"Missing OAuth client secret at {creds_path}. Download a Desktop-app OAuth "
            "client from Google Cloud Console (Gmail API enabled) and save it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes)
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


#: The href of an anchor already isolated by a card regex. Used by the boards whose card is one
#: <a> wrapping the whole posting (Glassdoor, justjoin), where `_anchors` would flatten away the
#: line structure the parser needs.
_HREF = re.compile(r'href="([^"]*)"', re.IGNORECASE)


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
        href_match = _HREF.search(anchor.group(0))
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


#: Each justjoin.it posting is one <a> wrapping the whole card, like Glassdoor's.
_JJ_ANCHOR = re.compile(
    r"<a\b[^>]*justjoin\.it/job-offer/[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE
)
#: The slug in the path is the posting's id; everything after `?` is per-recipient tracking
#: (and justjoin concatenates two query strings onto one href, so the cut matters).
_JJ_SLUG = re.compile(r"justjoin\.it/job-offer/([^?\"'\s]+)")
#: Head (company, city, title) plus tail (work mode, contract, seniority, days left, apply).
#: A card with no salary line has exactly this many; one with a salary has nine.
_JJ_MIN_LINES = 8
#: The work-mode chip, in both languages justjoin mails: Remote / Praca w pełni zdalna.
_JJ_REMOTE = re.compile(r"remote|zdaln", re.IGNORECASE)
#: Hybrid / Praca hybrydowa. Read off the same chip and decisive against, so a wording like
#: "praca zdalna hybrydowa" cannot be counted as remote.
_JJ_HYBRID = re.compile(r"hybr", re.IGNORECASE)


def _parse_justjoin(alert: _Alert) -> list[NormalizedJob]:
    """justjoin.it alerts: a fixed card read in from both ends, past a localized middle.

    Two addresses mail the identical layout — `no-reply@` in English, `jobs@hello.` in Polish —
    so the fields are taken by position, never by label (the Indeed lesson). Head: company,
    city, title. Tail: work mode, contract, seniority, days remaining, apply. The salary line
    between them is present or absent, which is exactly what counting from both ends absorbs.
    """
    jobs: list[NormalizedJob] = []
    seen: set[str] = set()
    for anchor in _JJ_ANCHOR.finditer(alert.html):
        href_match = _HREF.search(anchor.group(0))
        slug_match = _JJ_SLUG.search(href_match.group(1)) if href_match else None
        lines = [line.strip() for line in strip_html(anchor.group(1)).splitlines() if line.strip()]
        if slug_match is None or len(lines) < _JJ_MIN_LINES or slug_match.group(1) in seen:
            continue
        seen.add(slug_match.group(1))
        company, location, title = lines[0], lines[1], lines[2]
        work_mode = lines[-5]
        if not company or not title:
            continue
        jobs.append(
            NormalizedJob(
                url=f"https://justjoin.it/job-offer/{slug_match.group(1)}",
                company=company,
                title=title,
                location=location or None,
                is_remote=bool(_JJ_REMOTE.search(work_mode)) and not _JJ_HYBRID.search(work_mode),
                external_id=slug_match.group(1),
            )
        )
    return jobs


#: A pracuj.pl posting path, `/praca/<slug>,oferta,<id>`. The slug holds no comma, so the group
#: stops at `,oferta,` on its own and the tracking query after `?` never enters the URL.
_PRACUJ_OFFER = re.compile(r"pracuj\.pl/praca/([^,\"'\s]+,oferta,\d+)")
#: The yellow "!" chip pracuj renders *inside* the title anchor, ahead of the title itself.
_PRACUJ_BADGE = re.compile(r"^!\s+")
#: A pay line. It sits between the title anchor and the company anchor and carries the same
#: href as both, so without this it would be read as the employer's name.
#: The dash is escaped, not typed: pracuj writes the range with an en dash (U+2013) in some
#: mails and a plain hyphen in others, and the two are indistinguishable in source.
_PRACUJ_PAY = re.compile("\\d[\\d\\s\\u2013-]*(?:zł|pln|eur|usd)", re.IGNORECASE)


def _parse_pracuj(alert: _Alert) -> list[NormalizedJob]:
    """pracuj.pl recommendation mails: several anchors per card, all on the same offer link.

    There is no card container to key on — a posting is a title anchor, an optional pay anchor
    and a company anchor, each pointing at the same `,oferta,<id>` URL. So the anchors are
    grouped by that id in document order: the first text is the title, the last one that is not
    a pay line is the employer.

    The city is not in any anchor: pracuj appends it to the company name in the rendered text
    ("Integral Solutions Warszawa"). It is recovered by finding that line and subtracting the
    company off the front, scanning forward only so two cards from the same employer keep their
    own line rather than both taking the first.
    """
    html = alert.html
    cards: dict[str, list[str]] = {}
    for href, text in _anchors(html):
        match = _PRACUJ_OFFER.search(href)
        if match and text:
            cards.setdefault(match.group(1), []).append(text)

    lines = _lines(html)
    cursor = 0
    jobs: list[NormalizedJob] = []
    for path, texts in cards.items():
        title = _PRACUJ_BADGE.sub("", texts[0]).strip()
        company = next((t for t in reversed(texts[1:]) if not _PRACUJ_PAY.search(t)), "").strip()
        if not title or not company:
            continue
        location: str | None = None
        for index in range(cursor, len(lines)):
            line = lines[index]
            if line.startswith(company) and len(line) > len(company):
                location = line[len(company) :].strip() or None
                cursor = index + 1
                break
        jobs.append(
            NormalizedJob(
                url=f"https://pracuj.pl/praca/{path}",
                company=company,
                title=title,
                location=location,
                # The alert states no work mode at all, so this reads the title and the city and
                # nothing else. False is the safe answer here: an on-site EU posting is kept by
                # the hard filters either way, it simply ranks below a remote one.
                is_remote=looks_remote(title, location, ""),
                external_id=path.rpartition(",")[2],
            )
        )
    return jobs


#: Sender-domain substring -> parser. First match wins; unknown senders yield nothing.
#:
#: `wysylka.pracuj.pl`, not `pracuj.pl`: the receipts from `noreply@aplikacje.pracuj.pl` ("the
#: employer answers you directly") carry a "more employers waiting for you" block in the very
#: same card markup, so the domain reading would parse a per-application message into a dozen
#: postings — and, with `GMAIL_TRASH_PARSED_ALERTS` on, then Trash it. justjoin cannot be split
#: this way (`no-reply@justjoin.it` sends both the alerts and the "You applied for X" receipts),
#: so its receipts are held out by the Gmail query instead; see `seeds.py`.
_PARSERS: tuple[tuple[str, Callable[[_Alert], list[NormalizedJob]]], ...] = (
    ("hh.ru", _parse_hh),
    ("career.habr.com", _parse_habr),
    ("linkedin.com", _parse_linkedin),
    ("wellfound.com", _parse_wellfound),
    ("glassdoor.com", _parse_glassdoor),
    ("indeed.com", _parse_indeed),
    ("landing.jobs", _parse_landing_jobs),
    ("justjoin.it", _parse_justjoin),
    ("wysylka.pracuj.pl", _parse_pracuj),
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
             'newer_than:2d (from:hh.ru OR from:subscribe@career.habr.com
              OR from:jobalerts-noreply@linkedin.com OR from:wellfound.com
              OR from:glassdoor.com OR from:jobalert.indeed.com OR from:landing.jobs)'
             Name the board's alert address wherever it has one: whatever this query
             matches is what `on_committed` may put in the Trash, and a board's other
             addresses write to the human about specific applications (see `seeds.py`).
      max_results: int, cap on messages pulled per run. Caps the discard sweep too.
      discard_query: str, optional. Senders whose mail this funnel has decided it will
             never read, because everything in it arrives through a source we already
             have. Matching messages are Trashed **unread and unparsed** by
             `on_committed`, under the same setting and the same scope as the parsed
             alerts. Empty by default, and empty is the only safe default: this is the
             one query that discards mail without having got anything out of it, so
             every address in it has to be a decision somebody wrote down (they are,
             in the module docstring above).
    """

    name = "gmail-alerts"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        #: Gmail ids of the messages this fetch actually got postings out of. Only these are
        #: eligible for the Trash (see `on_committed`) — a message that parsed into nothing
        #: is as likely to be a board that changed its markup as it is to be junk, and
        #: deleting it would throw away the one copy a new parser could be written against.
        self.parsed_message_ids: list[str] = []

    async def fetch(self) -> list[NormalizedJob]:
        from googleapiclient.discovery import build

        query = str(self.config.get("query", ""))
        max_results = int(self.config.get("max_results", 50))

        creds = get_credentials(interactive=False)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        messages = service.users().messages()

        listed = messages.list(userId="me", q=query, maxResults=max_results).execute()
        jobs: list[NormalizedJob] = []
        self.parsed_message_ids = []
        for meta in listed.get("messages", []):
            raw = messages.get(userId="me", id=meta["id"], format="raw").execute()
            parsed = parse_raw_email(base64.urlsafe_b64decode(raw["raw"]))
            if parsed:
                self.parsed_message_ids.append(str(meta["id"]))
            jobs.extend(parsed)
        return jobs

    def _discard_ids(self, messages: Any) -> list[str]:
        """Gmail ids matching `discard_query` — mail we have decided never to read.

        Held apart from `parsed_message_ids` because the justification is the opposite one.
        A parsed alert is Trashed *because* we got its postings; these are Trashed because
        everything in them reaches the funnel by another route already, so there is nothing to
        get. That is a standing editorial decision about a handful of named addresses, not a
        judgement this code makes per message — which is why it is a query in the source config
        and not a rule here, and why an empty query means "discard nothing".
        """
        query = str(self.config.get("discard_query", "")).strip()
        if not query:
            return []
        max_results = int(self.config.get("max_results", 50))
        listed = messages.list(userId="me", q=query, maxResults=max_results).execute()
        return [str(meta["id"]) for meta in listed.get("messages", [])]

    async def on_committed(self) -> str | None:
        """Move alerts we read, and mail we never will, to Trash. Off unless turned on.

        Three deliberate limits, because this is the only thing the system does *to* the
        mailbox:

        - **Only after the commit.** The hook runs outside the transaction, so a posting is in
          the database before its email stops being.
        - **Only messages that yielded a posting, or that `discard_query` names.** A parse that
          found nothing keeps its email (see `parsed_message_ids`): it is as likely to be a
          board that changed its markup as it is to be junk. The discard list is the one
          exception, and it is an explicit list of addresses rather than an inference.
        - **Trash, never delete.** Gmail keeps a trashed message recoverable for 30 days,
          which is the window in which a parser bug is actually noticed, and the `gmail.modify`
          scope cannot permanently delete anything even if this code asked it to.

        One failure does not stop the rest: the ids are independent, and a message that stays
        in the inbox costs nothing but a re-parse that dedups to zero new rows.
        """
        from funnel.config import get_settings

        if not get_settings().gmail_trash_parsed_alerts:
            return None

        from googleapiclient.discovery import build

        creds = get_credentials(interactive=False)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        messages = service.users().messages()

        discarded_ids = self._discard_ids(messages)
        if not self.parsed_message_ids and not discarded_ids:
            return None

        trashed = failed = 0
        for message_id in [*self.parsed_message_ids, *discarded_ids]:
            try:
                messages.trash(userId="me", id=message_id).execute()
            except Exception:
                failed += 1
            else:
                trashed += 1
        # Reported apart: "we read these" and "we will never read these" are different claims,
        # and a run where the second number is not zero is one to look at.
        summary = f"trashed {trashed} alerts ({len(self.parsed_message_ids)} parsed"
        if discarded_ids:
            summary += f", {len(discarded_ids)} discarded unread"
        self.parsed_message_ids = []
        return summary + ")" + (f", {failed} failed" if failed else "")
