"""Correlating an incoming email with the application it answers (Phase 6).

Pure functions: no network, no LLM, no session. The whole point is that this is auditable and
testable offline, because a wrong match is worse than no match — it would stamp a rejection
onto the wrong application and hide a real interview.

Three strategies, strongest first. Every one of them requires a *unique* hit: if two
applications are equally plausible the reply stays unmatched and a human looks at it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from funnel.models import Application
    from funnel.replies.inbox import IncomingMessage

_ADDRESS = re.compile(r"[\w.+-]+@([\w.-]+\.[a-z]{2,})", re.IGNORECASE)

#: Mail from these carries no company identity: an ATS or a freemail host sends on behalf of
#: everyone. Domain matching is skipped for them, or every applicant tracking system on earth
#: would collapse onto whichever application happened to sort first.
#:
#: Matched on the domain **or any parent of it** (`is_generic_sender`). Greenhouse mails from
#: `us.greenhouse-mail.io` and `eu.greenhouse-mail.io`, Bamboo from `app.bamboohr.com`, and an
#: exact-membership test let every one of those straight through to company matching.
_GENERIC_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "yahoo.com",
        "yandex.ru",
        "mail.ru",
        "proton.me",
        "protonmail.com",
        "greenhouse.io",
        "greenhouse-mail.io",
        "lever.co",
        "hire.lever.co",
        "ashbyhq.com",
        "myworkday.com",
        "workablemail.com",
        "workable.com",
        "smartrecruiters.com",
        "bamboohr.com",
        "recruitee.com",
        "teamtailor.com",
        "personio.de",
        "jobvite.com",
        "icims.com",
        "taleo.net",
        "successfactors.com",
        "linkedin.com",
        "indeed.com",
        "glassdoor.com",
        "wellfound.com",
        "hh.ru",
        "habr.com",
        "landing.jobs",
    }
)

#: Job boards, matched on the registrable label instead of the full domain: one board mails from
#: as many domains as it has countries (`adzuna.pl`, `adzuna.nl`, `adzuna.com`) and as many
#: subdomains as it has systems (`wysylka.pracuj.pl`, `aplikacje.pracuj.pl`, `konto.pracuj.pl`).
#: A board is never a company answering us — at most it relays one, and then the sender still
#: says nothing about which company. These are the boards this funnel actually ingests from.
_BOARD_LABELS = frozenset(
    {
        "adzuna",
        "arbeitnow",
        "avito",
        "djinni",
        "getmatch",
        "justjoin",
        "nofluffjobs",
        "pracuj",
        "remoteok",
        "remotive",
        "totaljobs",
        "totaljobsmail",
    }
)

#: Dropped before comparing a company name to a domain — they are legal or generic noise that
#: never shows up in the domain ("Acme Technologies Ltd" mails from acme.com).
_COMPANY_NOISE = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "limited",
        "gmbh",
        "bv",
        "nv",
        "ab",
        "oy",
        "as",
        "sa",
        "srl",
        "spa",
        "plc",
        "co",
        "corp",
        "corporation",
        "company",
        "group",
        "holding",
        "holdings",
        "technologies",
        "technology",
        "tech",
        "labs",
        "lab",
        "software",
        "solutions",
        "systems",
        "digital",
        "studio",
        "studios",
        "games",
        "the",
    }
)

#: Below this many characters a company slug matches far too much ("Ai" inside "aircall.com").
_MIN_SLUG_CHARS = 4


def _slug(text: str) -> str:
    """Lowercase alphanumerics only, so 'A.Team' and 'a-team' compare equal."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def sender_domain(address: str) -> str:
    """The domain out of a From header, which may be 'Jane Doe <jane@acme.com>'."""
    found = _ADDRESS.search(address)
    return found.group(1).lower() if found else ""


#: Enough of the public suffix list to strip 'co.uk'-style tails. Deliberately not a PSL
#: dependency: an unknown suffix costs a missed match, never a wrong one.
_SUFFIXES = frozenset({
    "com", "net", "org", "edu", "gov", "biz", "info", "io", "co", "ai", "dev", "app", "me",
    "xyz", "tech", "jobs", "eu", "uk", "us", "ru", "de", "fr", "nl", "se", "no", "fi", "dk",
    "pl", "cz", "es", "it", "pt", "ch", "at", "be", "ie", "ca", "au", "nz", "br", "jp", "cn",
    "in", "sg", "il", "ge", "rs", "ua", "by", "kz", "tr", "gr", "hu", "ro", "bg", "hr", "si",
})  # fmt: skip


def registrable_domain(domain: str) -> str:
    """'careers.acme.co.uk' -> 'acme'. The label to compare a company name against."""
    labels = [label for label in domain.lower().split(".") if label]
    while len(labels) > 1 and labels[-1] in _SUFFIXES:
        labels.pop()
    return labels[-1] if labels else ""


def company_slug(company: str) -> str:
    """Company name reduced to the part that could plausibly appear in a domain."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", company.lower()) if w]
    meaningful = [w for w in words if w not in _COMPANY_NOISE]
    return "".join(meaningful or words)


def is_generic_sender(domain: str) -> bool:
    """True when the sender's domain names a platform rather than an employer."""
    if any(domain == generic or domain.endswith(f".{generic}") for generic in _GENERIC_DOMAINS):
        return True
    return registrable_domain(domain) in _BOARD_LABELS


def _is_company_domain(company: str, domain: str) -> bool:
    """Does this domain belong to this company?

    The containment is **anchored**: one slug must be a prefix of the other, not merely a
    substring. A free substring test read `join` inside `justjoin` and filed a justjoin.it job
    alert as JOIN's answer to an application (2026-08-06). Anchoring still keeps the case
    containment is here for — a legal tail the domain drops, `itds` against
    `itdspolskaspzoo` — because that one is a prefix; it gives up the rarer `getproxify.io`
    shape. That trade is the one this module makes everywhere: a missed match costs a human
    glance, a wrong match stamps a rejection onto the wrong application.
    """
    slug = company_slug(company)
    label = _slug(registrable_domain(domain))
    if len(slug) < _MIN_SLUG_CHARS or len(label) < _MIN_SLUG_CHARS:
        return False
    return label.startswith(slug) or slug.startswith(label)


def _unique(candidates: list[Application]) -> Application | None:
    """One plausible application is a match; two is an ambiguity, and we refuse to guess."""
    return candidates[0] if len(candidates) == 1 else None


def match_reply(
    message: IncomingMessage, applications: Sequence[Application]
) -> Application | None:
    """Find the application this email answers, or None to leave it for a human.

    `applications` is the set worth considering — in practice the ones already marked sent.
    """
    # 1. Same Gmail thread as the application the human sent. Unambiguous when we have it.
    if message.thread_id:
        threaded = [a for a in applications if a.thread_id and a.thread_id == message.thread_id]
        if threaded:
            return threaded[0]

    # 2. The sender's own domain is the company's. Skipped for ATS and freemail hosts, which
    #    is exactly the case a form application produces — hence strategy 3.
    domain = sender_domain(message.from_address)
    if domain and not is_generic_sender(domain):
        matched = _unique([a for a in applications if _is_company_domain(a.job.company, domain)])
        if matched:
            return matched

    # 3. The company's name in the subject — how ATS auto-mail usually identifies itself
    #    ("Your application to Acme").
    subject_slug = _slug(message.subject)
    if subject_slug:
        by_subject = [
            a
            for a in applications
            if len(company_slug(a.job.company)) >= _MIN_SLUG_CHARS
            and company_slug(a.job.company) in subject_slug
        ]
        if (matched := _unique(by_subject)) is not None:
            return matched

    return None
