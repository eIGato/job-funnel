"""Correlating an incoming email with the application it answers (Phase 6).

Pure functions: no network, no LLM, no session. The whole point is that this is auditable and
testable offline, because a wrong match is worse than no match — it would stamp a rejection
onto the wrong application and hide a real interview.

Five strategies, strongest first. Every one of them requires a hit that is *unique*, or —
where the only tie is between two roles at the same company — one that can be broken by when
the letters went out. Anything else stays unmatched and a human looks at it.

A match carries how sure it is (`Match.conclusive`). Evidence that points at one application
(the thread, the company's own domain, a company named nowhere else) may move the
Application's status; evidence that only narrows it to a company (the tie-break, a name found
in the body among footers and signatures) links the Reply so the human sees it next to the
application, and leaves the status alone.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

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

#: The boards among the generic senders above. Held apart from `_GENERIC_DOMAINS` because the
#: two halves of that set deserve opposite treatment: an ATS (greenhouse, lever, ashby) sends
#: the acknowledgements we most want to read, while a board sends bulk alerts that are never an
#: answer to anything. `is_board_sender` is what lets `check-replies` record the second kind
#: without paying a classification for it.
_BOARD_DOMAINS = frozenset(
    {
        "linkedin.com",
        "indeed.com",
        "glassdoor.com",
        "wellfound.com",
        "hh.ru",
        "habr.com",
        "career.habr.com",
        "landing.jobs",
        "reed.co.uk",
    }
)

#: Addresses on a board domain that are **not** bulk mail. A board that sends alerts also sends
#: the human personally, and the personal one is exactly the acknowledgement this module exists
#: to catch: Habr Career mails subscription digests from `subscribe@career.habr.com` and
#: per-application receipts ("you applied to <role> at <company>") from
#: `noreply@career.habr.com` — 4 in a year as of 2026-08-13, every one of them answering an
#: application already on record as `sent`.
#:
#: An exception list rather than moving the alert address into `_BOARD_DOMAINS`: the domain
#: entry keeps catching whatever `career.habr.com` starts mailing next, and an unknown new
#: sender is better treated as bulk. That is the direction this module errs everywhere — a
#: missed match costs a human glance, a wrong match stamps a rejection onto the wrong
#: application. Listing `subscribe@` as the only board would have inverted it.
#:
#: `noreply@aplikacje.pracuj.pl` is the same shape, found in the 2026-08-17 mailbox sweep: the
#: recommendation mail comes from `rekomendacje@wysylka.pracuj.pl`, while this address sends
#: "<role>: pracodawca udziela bezpośrednich informacji" — 4 of them, each naming the employer
#: and the role, one of them answering application 71 (ITDS Polska, sent 2026-08-04). `pracuj`
#: is in `_BOARD_LABELS`, so without this they were all being written off as bulk.
_BOARD_ADDRESS_EXCEPTIONS = frozenset({"noreply@career.habr.com", "noreply@aplikacje.pracuj.pl"})

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
        # Legal forms outside the anglosphere, which the company writes in its own name and
        # never in an email. "MindPal Sp. z o. o." welcomed us to plain "Mindpal", and the
        # slug of "Fundacja Szkoła w Chmurze" could not match a subject saying only
        # "Szkoła w Chmurze" (both measured 2026-08-12).
        "sp",
        "spzoo",
        "zoo",
        "oo",
        "ooo",
        "z",
        "o",
        "spolka",
        "fundacja",
        "doo",
        "dooel",
        "sro",
        "kft",
        "ag",
        "kg",
        "oyj",
        "aps",
        "asa",
    }
)

#: Below this many characters a company slug matches far too much ("Ai" inside "aircall.com").
#: Only the *domain* comparison needs it, because a domain label has no word boundaries to
#: anchor against — `names_company` matches whole words and is safe down to three characters
#: ("Thank you for applying at CGF", which this floor used to drop).
_MIN_SLUG_CHARS = 4
#: The shortest company name `names_company` will look for, joined. Two letters is an initialism
#: that means something else in half the sentences it appears in.
_MIN_COMPANY_CHARS = 3
#: How much of a body is still the letter rather than footers, legal boilerplate and signatures.
_BODY_HEAD_CHARS = 800

#: Letters NFKD does not take apart, because the diacritic is not a combining sequence — the
#: stroke in `ł` is part of the codepoint. Folding them by hand is what lets "Szkoła" and
#: "Szkola" be the same company.
_TRANSLITERATE = str.maketrans(
    {
        "ł": "l",
        "đ": "d",
        "ø": "o",
        "ħ": "h",
        "ı": "i",  # noqa: RUF001 - dotless i is the point: it must fold to a plain i
        "þ": "th",
        "ß": "ss",
        "æ": "ae",
        "œ": "oe",
    }
)


def fold(text: str) -> str:
    """Lowercase and strip diacritics, so 'Szkoła' compares equal to 'Szkola'."""
    lowered = text.lower().translate(_TRANSLITERATE)
    return "".join(
        c for c in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(c)
    )


def _slug(text: str) -> str:
    """Lowercase alphanumerics only, so 'A.Team' and 'a-team' compare equal."""
    return re.sub(r"[^a-z0-9]", "", fold(text))


def words(text: str) -> list[str]:
    """The alphanumeric words of a text, folded. The unit everything here compares on."""
    return [word for word in re.split(r"[^a-z0-9]+", fold(text)) if word]


def sender_domain(address: str) -> str:
    """The domain out of a From header, which may be 'Jane Doe <jane@acme.com>'."""
    found = _ADDRESS.search(address)
    return found.group(1).lower() if found else ""


def sender_mailbox(address: str) -> str:
    """The whole `local@domain` out of a From header, for the rules a domain cannot express."""
    found = _ADDRESS.search(address)
    return found.group(0).lower() if found else ""


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


def company_words(company: str) -> list[str]:
    """The words of a company name that could identify it in an email.

    Legal and generic noise is dropped, unless that would leave nothing: "The Studio Group"
    keeps all three words rather than becoming unmatchable.
    """
    found = words(company)
    return [w for w in found if w not in _COMPANY_NOISE] or found


def company_slug(company: str) -> str:
    """Company name reduced to the part that could plausibly appear in a domain."""
    return "".join(company_words(company))


def names_company(text: str, company: str) -> bool:
    """Does this text name this company, word for word?

    Whole words, not substrings. Matching a slug against the letters of a subject or a body
    run together reads names that are not there: the company slug of "Profil Software" is
    `profil`, which sits inside the word "profile", and a Toptal acknowledgement was duly
    matched to a Profil Software application (2026-08-12, dry run). Every meaningful word of
    the name must be present, so "Client Server" needs both.

    Word boundaries alone are too strict in one direction, though: an ATS board hands us the
    company as one run-together slug (`Chaosindustries`) while its own email writes it out
    ("Thank you for applying to CHAOS Industries!"). So a *run of consecutive words* that
    joins to exactly the company slug counts too, in either direction. Exact equality on the
    run, never containment — that is what keeps `profil` out of "profile".
    """
    want = company_words(company)
    if sum(len(w) for w in want) < _MIN_COMPANY_CHARS:
        return False
    found = words(text)
    if set(want) <= set(found):
        return True

    slug = "".join(want)
    for start in range(len(found)):
        joined = ""
        for word in found[start:]:
            joined += word
            if len(joined) >= len(slug):
                if joined == slug:
                    return True
                break
    return False


def display_name(address: str) -> str:
    """The human-readable part of a From header: 'Moon Active Hiring Team <x@ashbyhq.com>'.

    Where an ATS acknowledgement usually says which company it is on behalf of — the address
    only ever names the ATS.
    """
    found = re.match(r'\s*"?([^"<]+?)"?\s*<', address)
    return found.group(1) if found else ""


def is_generic_sender(domain: str) -> bool:
    """True when the sender's domain names a platform rather than an employer."""
    if any(domain == generic or domain.endswith(f".{generic}") for generic in _GENERIC_DOMAINS):
        return True
    return registrable_domain(domain) in _BOARD_LABELS


def is_board_sender(address: str) -> bool:
    """True when the sender is a job board, i.e. mails alerts rather than answers.

    Takes the whole From header, not just the domain: a board's alert address and its
    per-application address live on the same domain (`_BOARD_ADDRESS_EXCEPTIONS`), and only
    the first one is bulk.

    Narrower than `is_generic_sender`, which also covers the ATSs — those send the
    acknowledgements this whole module exists to catch.

    A board domain is not proof of a robot: one real recruiter has written from `avito.ru`,
    which is on the list. That is why `check-replies` still records the message and only
    withholds the classification from it, and why the check runs *after* matching: a board
    relaying an answer to an application we know about is classified like any other reply.
    """
    if sender_mailbox(address) in _BOARD_ADDRESS_EXCEPTIONS:
        return False
    domain = sender_domain(address)
    if registrable_domain(domain) in _BOARD_LABELS:
        return True
    return any(domain == board or domain.endswith(f".{board}") for board in _BOARD_DOMAINS)


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


class Match(NamedTuple):
    """The application a reply belongs to, which evidence says so, and how far to trust it."""

    application: Application
    strategy: str
    #: True when the evidence identifies this application, false when it only identifies the
    #: company and the rest was inference. Only a conclusive match may move a status.
    conclusive: bool


def _sent_order(application: Application, received_at: datetime | None) -> tuple[int, float]:
    """Rank applications by how well their send time explains an email arriving when it did."""
    sent = application.sent_at
    if sent is None:
        return (0, 0.0)  # never actually sent: the worst explanation of an answer
    if received_at is not None and sent > received_at:
        return (1, -sent.timestamp())  # sent after the email arrived: cannot be its cause
    return (2, sent.timestamp())  # sent before it, most recently first


def _pick(candidates: list[Application], received_at: datetime | None) -> Match | None:
    """Reduce the candidates for one strategy to a match, or to nothing.

    One candidate is a match. Several at the *same* company — two roles at Reddit, both
    applied for — is a tie the send times can break, but only into an inconclusive match:
    the company is certainly right and the role is a guess. Several companies is a real
    ambiguity and stays unmatched, because there is nothing to break it with.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return Match(candidates[0], "", conclusive=True)
    if len({a.job.company.casefold() for a in candidates}) > 1:
        return None
    best = max(candidates, key=lambda a: _sent_order(a, received_at))
    return Match(best, "", conclusive=False)


def match_reply(message: IncomingMessage, applications: Sequence[Application]) -> Match | None:
    """Find the application this email answers, or None to leave it for a human.

    `applications` is the set worth considering — in practice the ones already marked sent.
    """
    strategies: list[tuple[str, list[Application], bool]] = []

    # 1. Same Gmail thread. Unambiguous when we have it, and we have it for every application
    #    whose acknowledgement was matched (check-replies writes the thread back).
    if message.thread_id:
        threaded = [a for a in applications if a.thread_id and a.thread_id == message.thread_id]
        if threaded:
            return Match(threaded[0], "thread", conclusive=True)

    domain = sender_domain(message.from_address)

    # Past the thread, a job board can say nothing about which application anything belongs
    # to. Its alerts *list* companies by the dozen: a justjoin digest naming EuroCert among
    # its recommendations was matched to the EuroCert application by the body rule below
    # (2026-08-12, dry run). Everything a board relays that is genuinely an answer arrives in
    # a thread, and everything else it sends is an advertisement.
    if is_board_sender(message.from_address):
        return None

    # 2. The sender's own domain is the company's. Skipped for ATS and freemail hosts, which
    #    is exactly the case a form application produces — hence everything below.
    if domain and not is_generic_sender(domain):
        by_domain = [a for a in applications if _is_company_domain(a.job.company, domain)]
        strategies.append(("domain", by_domain, True))

    # 3. The company in the From display name. An ATS mails as itself and signs as the
    #    employer: "Moon Active Hiring Team <no-reply@ashbyhq.com>".
    if name := display_name(message.from_address):
        strategies.append(("display-name", _named_in(name, applications), True))

    # 4. The company in the subject ("Your application to Acme").
    if message.subject:
        strategies.append(("subject", _named_in(message.subject, applications), True))

    # 5. The company in the head of the body, for the acknowledgement that names it nowhere
    #    else ("Dziękujemy za Twoją aplikację", signed EuroCert three paragraphs down).
    #    Never conclusive: this is the surface where footers, disclaimers and "powered by"
    #    lines live.
    if head := message.body[:_BODY_HEAD_CHARS]:
        strategies.append(("body", _named_in(head, applications), False))

    for strategy, candidates, conclusive in strategies:
        if (found := _pick(candidates, message.received_at)) is not None:
            return found._replace(strategy=strategy, conclusive=conclusive and found.conclusive)

    return None


def _named_in(text: str, applications: Sequence[Application]) -> list[Application]:
    return [a for a in applications if names_company(text, a.job.company)]
