"""Hard filters (Phase 4a). Deterministic code, no LLM, no tokens.

Cheaply discards obvious misses *before* embedding, so only survivors get embedded.

Answered geography rules (PLAN.md section 7):
  - RU / BY work locations: a hard stop. Read from the location field, and — only when a board
    left that field empty — from the first line of the body, which is where a Telegram posting
    puts its office ("OFFICE MINSK | ЛЕСТА ИГРЫ").
  - A *remote* posting locked to a geography we cannot satisfy ("US only", "must be authorized
    to work in the UK"): reject — unless it explicitly welcomes a contractor / B2B arrangement,
    which the human can serve through a Georgian entity.
  - On-site / hybrid that is merely *silent* about sponsorship: KEPT (companies sponsor on
    request without saying so); it merely ranks below remote, and ranking is a sort, not this
    predicate.
  - On-site / hybrid that *refuses* to sponsor in so many words: reject. That is the same
    statement the remote branch already rejects, and the "they might sponsor on request"
    assumption above is exactly the question it has answered (see `_NO_SPONSORSHIP`).
  - Timezone: not filtered at all.
  - Montenegro on-site: deliberately NOT filtered (decided 2026-07-24). The local IT market is
    a fraction of a percent of the input stream, and the human would take a cheap local gig, so
    a hard filter there would cost more in false positives than it could ever save.

Answered seniority / stop-stack (PLAN.md section 7, decided 2026-07-24):
  - Seniority floor is Middle: a posting whose *title* names a level below Middle
    (junior / intern / trainee / entry-level) and no Middle-or-above level is dropped.
  - The only hard stop-stack item is training neural networks *as the primary role*, keyed on
    the title (the title is the "is this the priority?" signal). Working *with* AI/LLMs is
    wanted, not stopped — this targets model-training roles, not AI-orchestration ones. The
    softer preferences (PHP / Node / fullstack as a secondary focus, extra pay) are a judgment
    about a role's *emphasis*, which pure code cannot make well; they are deferred to the
    screening step (`drafting/screen.py`), which every drafting path runs, not forced into a
    regex here.

Junk postings:
  - A row whose title is scraped page furniture ("Job Details", "Couldn't pick up that page") or
    a placeholder ("This is a test job") is not a posting and is dropped.
  - So is a row whose *body* is prose that never mentions hiring at all — a nav menu, a cookie
    notice, a blog post. RemoteOK republishes whatever its crawler found, under a real company
    name and a title no list can anticipate ("UNC", "Danny", "The Ledbury").
  - So is an ad written to collect passports rather than to fill a role: a relocation offer,
    a demand for the applicant's travel document, and a requirements list that names four
    unrelated languages instead of a stack. All three together, never one alone (see
    `_is_relocation_scam`) — real relocation offers say two of these things.
  All three are only the "not a job posting" judgment; deciding that a real posting is the wrong
  *kind* of job belongs to the screening step.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Protocol


class _Filterable(Protocol):
    """The posting fields the filters read — satisfied by both NormalizedJob and the Job ORM."""

    title: str
    description: str
    location: str | None
    is_remote: bool


#: Cities, because that is what a location field actually holds. The boards we read name a city
#: and nothing else — "Томск", "Уфа", "Краснодар, Ростов-на-Дону" — so a rule that knows only the
#: country and its two capitals catches almost nothing. It knew exactly Moscow and St Petersburg
#: until 2026-08-03, and "Старший разработчик C++ / ИнфоТеКС / Томск" passed it at the 99.7th
#: percentile, straight into the head of the shortlist.
#:
#: Latin spellings are the ones a foreign board uses; the Cyrillic ones are stems, so declined
#: forms ("в Москве") match too. Deliberately absent from the Latin half: `Vladimir` (a given
#: name), `Brest` (also in France), `Orel`, `Perm` ("Perm Street, London") and `Tula` (also in
#: Mexico). A location filter must not fire on an ambiguity, and the Cyrillic stems carry those
#: cities anyway — a Russian posting writes them in Russian.
_RU_BY_LATIN = (
    "russia|russian federation|belarus|belarusian",
    "moscow|saint[- ]petersburg|st\\.? petersburg|petersburg|novosibirsk|"
    "y?ekaterinburg|kazan|nizhny novgorod|chelyabinsk|samara|omsk|rostov[- ]on[- ]don|"
    "ufa|krasnoyarsk|voronezh|volgograd|krasnodar|saratov|tyumen|tolyatti|izhevsk|"
    "barnaul|ulyanovsk|irkutsk|khabarovsk|yaroslavl|vladivostok|makhachkala|tomsk|orenburg|"
    "kemerovo|novokuznetsk|ryazan|astrakhan|naberezhnye chelny|penza|lipetsk|kirov|"
    "cheboksary|kaliningrad|kursk|ulan[- ]ude|stavropol|sochi|tver|magnitogorsk|"
    "ivanovo|bryansk|belgorod|surgut|kaluga|smolensk|volzhsky|cherepovets|vologda|saransk|"
    "yakutsk|innopolis|podolsk|kolpino|balashikha|khimki|mytishchi|korolev|lyubertsy",
    "minsk|gomel|homel|mogilev|mahilyow|vitebsk|vitsebsk|grodno|hrodna|bobruisk|babruysk",
)
_RU_BY_CYRILLIC = (
    "росси|беларус|рф\\b",
    "москв|санкт-петербург|петербург|новосибирск|екатеринбург|казан|"
    "нижн(?:ий|ем) новгород|челябинск|самар|омск|ростов|уфа|уфе|красноярск|воронеж|перм|"
    "волгоград|краснодар|саратов|тюмен|тольятти|ижевск|барнаул|ульяновск|иркутск|"
    "хабаровск|ярославл|владивосток|махачкал|томск|оренбург|кемеров|новокузнецк|рязан|"
    "астрахан|набережные челны|пенз|липецк|киров|чебоксар|тула|туле|калининград|курск|"
    "улан-удэ|ставропол|сочи|твер|магнитогорск|иванов|брянск|белгород|сургут|калуг|"
    "смоленск|волжский|череповец|вологд|саранск|якутск|иннополис|подольск|колпино|"
    "балаших|химки|мытищ|королёв|королев|люберц",
    "минск|гомел|могил[её]в|витебск|гродн|бобруйск|брест",
)

#: RU/BY work locations are a hard stop. Keyed on where the posting says the *work* is — the
#: location field, or the body's heading when the board left that field empty (see
#: `_heading_location`). Never the body at large: a remote foreign job that merely names Russia
#: somewhere in its text is the main stream, not a reject.
_RU_BY_LOCATION = re.compile(
    r"\b(?:" + "|".join(_RU_BY_LATIN) + r")\b|\b(?:" + "|".join(_RU_BY_CYRILLIC) + r")",
    re.IGNORECASE,
)

#: An explicit geographic lock we cannot satisfy. Applied to a *remote* posting only — on-site
#: postings normally state an authorization requirement and are kept regardless (sponsor on
#: request). Heuristic seed; extend as real alerts show new phrasings.
_GEO_LOCKED = re.compile(
    r"authorized to work in the|must be authorized to work|"
    r"must be (?:based|located|residing) in|must reside in|"
    r"(?:us|usa|u\.s\.|uk|eu|eea)[-\s]?only\b|only\s+(?:us|usa|uk|eu)\b|"
    r"u\.?s\.? citizens?|citizens? only|citizens? or permanent residents|green card",
    re.IGNORECASE,
)

#: A contractor/B2B welcome overrides the geo lock (the human has a Georgian entity for exactly
#: this — B2B contracts are the natural shape, net ~= gross).
_CONTRACTOR_OK = re.compile(
    r"\b(?:contractor|b2b|c2c|corp[-\s]to[-\s]corp|independent contractor|"
    r"self[-\s]employed|freelanc)",
    re.IGNORECASE,
)

#: A geo lock that still admits Europe (where the human lives — Montenegro/CET) or is globally
#: open is not a reject: it names a region the human *can* satisfy. Without this, a permissive
#: multi-region net like "must be located in the Americas, Europe, or Israel" is a false positive.
_REGION_OK = re.compile(r"europe|\beu\b|\beea\b|\bemea\b|anywhere|world-?wide", re.IGNORECASE)

#: ...but "EU" in a *citizenship* requirement means the opposite of "EU" in an invitation, and
#: `_REGION_OK` cannot tell them apart. "Candidates must be based in Portugal and hold Portuguese
#: or other EU citizenship" is a lock the human cannot open — he lives in Montenegro, outside the
#: EU — and it read as a Europe-welcome waiver, passing the posting through to a shortlist and a
#: letter that answered the requirement by claiming it (application 146, 2026-08-03). These spans
#: are removed before `_REGION_OK` looks, so a posting that says both ("open across Europe; EU
#: passport preferred") still keeps its waiver from the half that is genuinely an invitation.
_REGION_AS_REQUIREMENT = re.compile(
    r"\b(?:eu|eea|european)\b[\w\s,/()-]{0,30}?"
    r"\b(?:citizen\w*|national(?:s|ity)?|passport|work permit|residen\w+)\b"
    r"|\bcitizens?(?:hip)?\s+(?:of|in)\s+(?:the\s+)?(?:eu|eea|europe\w*)\b",
    re.IGNORECASE,
)

#: The posting names a citizenship/residency *preference* and then says it accepts everyone:
#: "U.S. Citizens and Green Card Holders highly preferred, all valid work authorizations may
#: apply". `_GEO_LOCKED` sees only the first half and reads a hard lock; this is the second half
#: saying there is none. Kept tight — "all/any" must sit next to the authorization phrase, not
#: merely somewhere in the posting, because "AWS preferred" elsewhere in a body is not consent.
_AUTHORIZATION_OPEN = re.compile(r"\b(?:all|any)\b[\w\s,]{0,30}?work authoriz", re.IGNORECASE)

#: A posting that says outright it will not sponsor. This is a different statement from
#: `_GEO_LOCKED`'s "must be authorized to work in the US", and it is why on-site postings are no
#: longer kept unconditionally: the standing reason for keeping them is that a company stating a
#: requirement will often sponsor on request, and this sentence has already answered that. The
#: human is a Montenegrin resident on a Russian passport — every workplace but Montenegro needs
#: someone to file the permit, and this posting says nobody will.
#:
#: Job 15486 ("Founding Engineer & Head of Engineering", clera, San Francisco, off the Ashby
#: source) is the measured case: "On-site in San Francisco, CA — remote work is not available for
#: this role. Visa sponsorship: not available", said twice, in a body that also passed the screen
#: — which cannot help here, because it is forbidden to judge geography (`drafting/screen.py`).
#: It ranked at the 90.1st percentile and took a shortlist slot and a cover letter on 2026-08-24
#: (application 773, drafted, never sent).
#:
#: Measured over all 20,752 rows (2026-08-26): 1,257 rows carry one of 47 distinct phrasings,
#: 1,137 of them on-site. 28 sit above the shortlist floor — 24 on-site, and of those two had
#: been drafted for (15486 and 9047, a Berlin SDK role), two had a letter sent and rejected, and
#: the rest the human had declined by hand or had yet to reach.
#:
#: **The gap between the negation and the word cannot cross a sentence.** With `[\s\S]` there,
#: job 10437 (Munich) matched on "remote work is not available\nVisa sponsorship is available" —
#: a posting that offers sponsorship, read as refusing it. A refusal and its negation live in one
#: sentence; `[^\n.;!?]` is what keeps them there.
_NO_SPONSORSHIP = re.compile(
    r"\b(?:no|not|without|cannot|can't|unable|won't)\b[^\n.;!?]{0,40}?\bsponsor\w*"
    r"|\bsponsor\w*\b[^\n.;!?]{0,25}?"
    r"\b(?:not available|not offered|not provided|not possible|unavailable)\b",
    re.IGNORECASE,
)

#: ...unless the workplace is the one the human can take without anyone's permission. Montenegro
#: on-site is a standing keep (see the header), and a Montenegrin posting saying "no visa
#: sponsorship" is talking to somebody else. Read from the location field only — this is a
#: statement about where the work is, the same field `_RU_BY_LOCATION` reads, not about a country
#: named somewhere in the prose.
_LOCAL_WORKPLACE = re.compile(
    r"montenegro|crna gora|podgoric|budva|tivat|kotor|herceg[- ]novi|nik[sš]i[cć]"
    r"|черногор|подгориц|будв|тиват|котор",
    re.IGNORECASE,
)

#: Unconditional stops: clearances a RU citizen cannot obtain.
STOP_PHRASES: frozenset[str] = frozenset({"security clearance"})

#: A level below the Middle floor, read from the TITLE only — level is a title thing, and this
#: keeps "we mentor junior engineers" in a senior role's body from tripping the filter.
_JUNIOR_TITLE = re.compile(
    r"\b(?:junior|jr\.?|intern(?:ship)?|trainee|entry[-\s]?level|new[-\s]?grad(?:uate)?)\b"
    r"|стаж[её]р|младш|джуниор|начинающ",  # noqa: RUF001 (Cyrillic is the point)
    re.IGNORECASE,
)
#: Middle-or-above named in the title. A junior-tagged title that ALSO admits one of these
#: ("Junior/Middle", "Middle/Senior") is a range that reaches the floor, so it is kept.
_MIDDLE_PLUS_TITLE = re.compile(
    r"\b(?:middle|mid[-\s]?level|mid[-\s]?senior|senior|sr\.?|lead|staff|principal|architect)\b"
    r"|миддл|мидл|сеньор|ведущ|старш",
    re.IGNORECASE,
)

#: Training neural networks as the primary role, keyed on the title. Working *with* AI is wanted
#: (the funnel widens toward AI orchestration), so "AI Engineer" is deliberately absent — this
#: targets model-building/training titles, not LLM-application ones.
_ML_TRAINING_TITLE = re.compile(
    r"\b(?:machine[-\s]learning engineer|ml engineer|ml scientist|"
    r"machine[-\s]learning scientist|deep[-\s]learning (?:engineer|scientist))\b",
    re.IGNORECASE,
)
#: ...unless the title marks it as the engineering *around* ML rather than training models. An
#: ML platform / infra / backend role is ordinary backend work and stays.
_ML_ADJACENT_TITLE = re.compile(
    r"platform|infrastructure|\binfra\b|backend|back[-\s]end|devops|mlops|data engineer",
    re.IGNORECASE,
)


#: Titles that are not a role at all: scraped page furniture and placeholder postings. RemoteOK
#: hands these out with an ordinary company name and a plausible teaser body, so nothing
#: downstream can tell them apart — "Job Details" and "Couldn't pick up that page" both embedded
#: at 0.84+ and reached the top of the shortlist, where each cost a drafted letter.
#:
#: Matched against the WHOLE normalized title, never as a substring: a real posting must never
#: be caught here, and "Details" inside "Senior Engineer - Details" is not junk. This list is
#: only for "this is not a job posting"; judging a real posting to be the wrong *kind* of job is
#: the screening step's business (`drafting/screen.py`), not a regex's. Extend as new artifacts
#: show up.
_JUNK_TITLES: frozenset[str] = frozenset(
    {
        "apply now",
        "couldn't pick up that page",
        "create your own role",
        "details",
        "expression of interest",
        "hiring",
        "job",
        "job details",
        "job posting title",
        "job title",
        "join our team",
        "jop posting title",
        "no title",
        "page not found",
        "test job",
        "this is a test job",
        "untitled",
        "vacancy",
        "we are hiring",
    }
)

#: Deliberately NOT paired with a minimum-description rule. The obvious companion filter — drop
#: a posting whose body is too short to use — was measured against the real table and would have
#: caught none of the junk above (those bodies run 371-1503 characters, against a 5th percentile
#: of ~430 for postings that pass). It would only have penalized the terse Telegram postings,
#: which are short *and* real. A filter that cannot be shown to catch the thing it is aimed at
#: does not earn its false positives.
#:
#: There *is* a minimum-body rule downstream (`cli.MIN_DRAFTABLE_BODY`), and it is not this one:
#: it decides who takes a shortlist slot, not who is a posting. A short posting keeps its score
#: and stays one button away from a letter; dropping it here would put it out of reach instead.

#: Words any real posting says somewhere, in the languages we ingest. A body that is prose and
#: still never reaches one of these is not a posting: it is a scraped page. RemoteOK republishes
#: whatever its crawler found on a company site, so 138 of its 467 rows (measured 2026-08-03) are
#: nav menus ("Home Who Are We? How Do We Work? Contact"), cookie notices, blog posts, lorem
#: ipsum, and one row of keyboard mash — each with a real id, a real company name and a title
#: like "UNC" or "Danny". They are unreachable by `_JUNK_TITLES`, which needs the exact title,
#: and five of them held slots in the top 25 and cost a screening call apiece.
_HIRING_VOCABULARY = re.compile(
    r"experience|responsibilit|requirement|qualification|we are looking|you will|your profile"
    r"|skills|salary|apply|benefits|\bteam\b|\brole\b|position|hiring|join us|candidate"
    r"|erfahrung|aufgaben|kenntnisse|bewerb|mitarbeit|stelle"  # de
    r"|stanowisko|wymagania|obowi|poszukujemy|oferujemy|umiej|praca"  # pl
    r"|empleo|puesto|experiencia"  # es
    r"|опыт|обязанност|требован|вакансия|зарплат|команд|разработчик|инженер",  # ru
    re.IGNORECASE,
)

#: The vocabulary test is only fair on a *complete* body. Adzuna serves a teaser cut off
#: mid-sentence — 500 characters of company preamble that often has not reached the
#: requirements yet — and dropping those would cost 39 real postings, several of them the
#: best-scoring rows in the table. The board marks the cut with an ellipsis; the mojibake
#: spellings are there because some feeds are double-encoded UTF-8 (see `Â…`, `â€¦`).
#:
#: Matched anywhere, not just at the end: RemoteOK carries LinkedIn teasers that break off
#: mid-sentence and then append "See this and similar jobs on LinkedIn", so the marker sits in
#: the middle. An ellipsis used rhetorically in a real posting costs nothing — the vocabulary
#: test still has to fail as well, and a real posting's body talks about the job somewhere.
#:
#: `¦` catches the mojibake spellings without enumerating them. U+2026 is `e2 80 a6`, and every
#: round of latin-1-misread-as-UTF-8 keeps that trailing `a6` as a literal `¦`: `â€¦`, then
#: `Ã¢Â€Â¦`. The broken bar is not a character real prose uses — all 67 rows carrying one are
#: mangled ellipses.
_TRUNCATED = re.compile(r"…|\.\.\.|¦")

#: ...and only on prose. A gmail alert's body is a technology tag list ("Python, Golang"), which
#: names no duties by construction and would fail a vocabulary test while being perfectly real.
#: A sentence terminator is what separates the two: 116 of 119 gmail rows have none.
_PROSE = re.compile(r"[.!?]")

#: ...and only on a body long enough to expect a hiring word in it. "Python backend. Remote.
#: Write to @hr." is a whole Telegram posting and says none of the words above; the funnel keeps
#: terse postings on purpose (see the note on the absent minimum-description rule). Measured
#: over the table, this floor gives up 3 of 101 catches — a cheap price for not having to argue
#: about the short ones.
_MIN_JUDGEABLE_BODY = 100


#: A posting that asks to see a passport while promising a visa. The three regexes below are
#: one rule and only fire together: a relocation offer, a demand for the applicant's travel
#: document, and a technical section that names no actual stack. Job 4257 ("Software Developer",
#: "Brahmandnayak Group Of Companies", Berlin, from a Glassdoor alert) had all three, ranked at
#: the 95.8th percentile, and the human sent it a real letter with his CV on 2026-08-11 before
#: recognizing it — which is the cost this filter exists to avoid, and it is not a wasted
#: screening call: it is personal data handed to whoever placed the ad.
#:
#: Each signal alone is ordinary. Measured over all 18,856 rows on 2026-08-23: "visa
#: sponsorship" 1,549 rows, a relocation offer 553, a "or similar technologies" hedge 139,
#: boilerplate duties 71, a run of four unrelated languages 14, a passport demand 3. The
#: conjunction is what is rare — 4257 is the only row in the table that matches, and the three
#: rows that come closest (three signals, none of them the passport) are all one real Munich/SF
#: startup with a genuine relocation offer. Keep the conjunction: a real relocation offer says
#: two of these things too, and the honest ones name the stack they hire for.
#:
#: A salary-spread rule was considered here and is not implementable: `Job` stores no salary
#: (only a few adapters even carry one, inside the body text), and this posting quotes no
#: number at all — it says "Competitive salary", which is the tell it shares with the boilerplate
#: rather than a range to measure.
_PASSPORT_REQUIRED = re.compile(
    r"\bvalid\s+(?:international\s+)?passport\b|\bpassport\s+(?:is\s+)?required\b"
    r"|\binternational\s+passport\b|загранпаспорт|заграничн\w+\s+паспорт",
    re.IGNORECASE,
)
#: The bait. Legitimate on its own — the funnel wants relocation offers, and 1,549 rows mention
#: sponsorship.
_RELOCATION_OFFER = re.compile(
    r"visa sponsorship|sponsor\w*\s+(?:your\s+)?(?:work\s+)?visa|work permit sponsor"
    r"|willing(?:ness)?\s+to\s+relocate|ready to relocate"
    r"|relocation\s+(?:support|package|assistance)|релокац",
    re.IGNORECASE,
)
#: A requirements section that names no team's actual stack: four unrelated languages in a row,
#: usually hedged with "or similar technologies". No real team hires one engineer for Java AND
#: .NET AND C# AND JavaScript; an ad that lists them is not describing work it has.
_LANGUAGE_SOUP = re.compile(
    r"(?:\b(?:java|python|\.\s?net|c#|c\+\+|javascript|typescript|php|ruby|golang|kotlin"
    r"|swift|scala)\b[\s,/]+){3,}(?:(?:and|or)\s+)?"
    r"\b(?:java|python|\.\s?net|c#|c\+\+|javascript|typescript|php|ruby|golang|kotlin"
    r"|swift|scala|similar)\b",
    re.IGNORECASE,
)
#: ...or duties written from the idea of programming rather than from a product.
_BOILERPLATE_DUTIES = re.compile(
    r"write clean(?:,?\s+(?:and\s+)?(?:efficient|readable|maintainable))?\s+code"
    r"|develop,?\s+test,?\s+and maintain software",
    re.IGNORECASE,
)


#: A location field a board never filled in. Telegram/teletype postings routinely have none and
#: put the office in the first line of the body instead ("OFFICE MINSK | ЛЕСТА ИГРЫ"), which is
#: how a Minsk posting reached the drafting step on 2026-08-03 — past a hard stop the human
#: settled long ago, because the only field the rule reads was empty.
#:
#: Read as a *heading*, not as prose: the first line only, and only when it is short enough to be
#: a header rather than a sentence. "We are a Russian-founded company…" opening a real remote
#: posting must not trip this, and the module's standing rule is that a passing mention of Russia
#: in a body is the main stream, not a reject. Measured over the table: catches the one posting
#: it is aimed at, and none of the 15 rows that merely name Russia somewhere in the text.
_HEADING_MAX_CHARS = 80


def _heading_location(job: _Filterable) -> str:
    """The first line of the body, when the board left the location field empty."""
    if (job.location or "").strip():
        return ""
    lines = job.description.strip().splitlines()
    first = lines[0].strip() if lines else ""
    return first if len(first) <= _HEADING_MAX_CHARS else ""


def _normalized_title(title: str) -> str:
    """Fold a title for comparison: entities decoded, whitespace collapsed, case dropped."""
    return " ".join(unescape(title).split()).strip(" -–—:|").casefold()  # noqa: RUF001 (dashes)


def _unwritten_body(job: _Filterable) -> bool:
    """True when the body is prose that never once talks about hiring — a scraped page.

    Four conditions, and every one is load-bearing. Judge only a body that is long enough to
    expect a hiring word in it, that is complete (an Adzuna teaser is cut off before the
    requirements), and that is prose (a gmail alert is a technology tag list). Measured over
    the whole table: 98 rows caught, every one of them RemoteOK page furniture, none with a
    role-like title, and nothing at all from the other seven sources.
    """
    body = job.description
    if len(body) < _MIN_JUDGEABLE_BODY or not _PROSE.search(body) or _TRUNCATED.search(body):
        return False
    return not _HIRING_VOCABULARY.search(f"{job.title}\n{body}")


def _is_junk(job: _Filterable) -> bool:
    """True when the row is not a real posting at all, only scraped page furniture."""
    return _normalized_title(job.title) in _JUNK_TITLES or _unwritten_body(job)


def _is_relocation_scam(job: _Filterable) -> bool:
    """True when the ad wants the applicant's passport more than his code.

    The other kind of "not a real posting": posting-shaped, grammatical, and placed to collect
    documents and fees from people who want to move. `_is_junk` cannot see it — this body says
    every hiring word there is — and the screen must not be asked to, because the tell is made
    of visas and relocation, which is geography and therefore this module's business.
    """
    body = f"{job.title}\n{job.description}"
    return bool(
        _PASSPORT_REQUIRED.search(body)
        and _RELOCATION_OFFER.search(body)
        and (_LANGUAGE_SOUP.search(body) or _BOILERPLATE_DUTIES.search(body))
    )


def _sponsorship_refused(job: _Filterable, haystack: str) -> bool:
    """True when the posting refuses to sponsor and the human would need it to work there.

    Kept out of `_GEO_LOCKED` because the two are applied to different postings: a *requirement*
    is only a lock on a remote posting, while a *refusal* is a lock on any posting the human
    cannot already work in — which, on-site, is everywhere but Montenegro.
    """
    if not _NO_SPONSORSHIP.search(haystack):
        return False
    return not _LOCAL_WORKPLACE.search(job.location or "")


def _below_middle(title: str) -> bool:
    return bool(_JUNIOR_TITLE.search(title)) and not _MIDDLE_PLUS_TITLE.search(title)


def _ml_training_primary(title: str) -> bool:
    return bool(_ML_TRAINING_TITLE.search(title)) and not _ML_ADJACENT_TITLE.search(title)


def passes_hard_filters(job: _Filterable) -> bool:
    """True when the posting is worth embedding. Pure: no I/O, no model calls."""
    if _is_junk(job) or _is_relocation_scam(job):
        return False
    if _RU_BY_LOCATION.search(job.location or "") or _RU_BY_LOCATION.search(_heading_location(job)):
        return False
    haystack = f"{job.title}\n{job.description}\n{job.location or ''}"
    folded = haystack.casefold()
    if any(phrase in folded for phrase in STOP_PHRASES):
        return False
    # Seniority and the one hard stop-stack item read the title only (its priority signal).
    if _below_middle(job.title) or _ml_training_primary(job.title):
        return False
    # An on-site posting that refuses to sponsor is a door with no handle: none of the escapes
    # below can open it, because a B2B contract and a Europe-wide welcome still do not put the
    # human in the office. The remote branch keeps them — there, a refusal to sponsor is one more
    # way of saying "you must already be authorized here", which a contractor arrangement or a
    # genuinely global posting answers.
    if not job.is_remote:
        return not _sponsorship_refused(job, haystack)
    # A remote posting locked to a geography we cannot satisfy, with no contractor door and no
    # Europe/worldwide admission, is out.
    return not (
        (bool(_GEO_LOCKED.search(haystack)) or _sponsorship_refused(job, haystack))
        and not _CONTRACTOR_OK.search(haystack)
        and not _REGION_OK.search(_REGION_AS_REQUIREMENT.sub(" ", haystack))
        and not _AUTHORIZATION_OPEN.search(haystack)
    )
