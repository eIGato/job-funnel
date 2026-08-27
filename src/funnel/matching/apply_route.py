"""Apply routes: postings whose link leads nowhere the human can apply (decided 2026-08-05).

A posting only earns a shortlist slot if the human can reach an application form from it.
Three hosts fail that test, and between them they held **131 of the ~640 rows above the 90th
percentile** — a fifth of every shortlist, each slot costing a screening call and a cover
letter for something nobody could ever send:

- **`adzuna.com` and `adzuna.ca`** answer 403 from the human's region. Not the apply button —
  the whole site: `curl` from the same machine gets 403 on `/details/<id>` and on `/land/ad/<id>`
  alike (measured 2026-08-05). The other Adzuna countries (gb/de/nl/pl/es/at) serve and accept
  applications normally and are deliberately untouched; this is a per-site block, not a verdict
  on the source.
- **`remoteok.com`** puts the apply link behind its paid tier. Its API is no way round that:
  every row's `apply_url` is a verbatim copy of its own `url` (all 100 rows of the live feed,
  2026-08-05). Reading the real link out of the page HTML would be circumventing a paywall,
  which is not something this project does (invariant 9).

**A blocked posting is kept out of the shortlist, not out of the funnel.** It keeps its score
and its place in the corpus, for two reasons:

1. The centre every score is measured against is a property of the whole corpus
   (`matching/embed.centered_similarity`), so dropping rows out of it moves everyone else's
   number.
2. A blocked posting is the best input ATS discovery has. `adapters/ats.py` guesses a company's
   own board from its name, and the employer's own board *is* the direct link — so a company
   whose only link is a dead end is precisely the one worth spending a guess on, and it is
   ranked ahead of the others there.

Both are why this is not a rule in `matching/filters.py`: a hard filter strips the score, which
would move the centre and hide those companies from the probe. It is a selection rule, applied
where the shortlist is chosen (`cli.shortlist_select`).

Finding a direct link automatically has one measured route and one that does not exist. The
route: the ATS name probe above (7 boards confirmed over ~79 companies tried, ~9%). The one
that does not: what the human does by hand — google the company plus the title and apply on the
employer's own site — needs a general web search, which this funnel has none of. Adding one is
the human's decision, not ours (PLAN.md §7).

Twin lookup was tried and rejected on measurement: of the 131 blocked rows above the 90th
percentile, exactly **1** had a row from another source under the same folded company+title,
and 4 shared even a company. Aggregators do not overlap the way the idea assumes.
"""

from __future__ import annotations

from urllib.parse import urlparse

#: Hosts where a posting cannot be applied to, whatever it says. Registrable domains: the test
#: below covers their subdomains too. Extend as the human hits another wall — and delete an
#: entry the moment a site lets them in again, since every entry costs real postings.
#:
#: "As the human hits another wall" now has a record instead of a memory:
#: `ApplicationStatus.UNREACHABLE` is what he marks the row, and `funnel doctor` groups those
#: rows by host. One row is a dead posting; a host that keeps coming back is the next entry
#: here. The report never edits this list — deciding that a whole site is closed to him is a
#: judgment about his region and his accounts, which is his to make (invariant 8).
BLOCKED_HOSTS: frozenset[str] = frozenset({"adzuna.com", "adzuna.ca", "remoteok.com"})


def is_blocked(url: str) -> bool:
    """True when nothing behind this URL leads to an application form. Pure: no I/O.

    Matched on the host and its subdomains, never as a substring of the URL. `adzuna.com` as a
    substring would swallow `adzuna.com.au` — a live Adzuna the human can use, and one country
    away from the config — and a host suffix rule keeps `remoteok.com` off a company that
    happens to be called `notremoteok.com`.
    """
    host = (urlparse(url.strip()).hostname or "").lower().removeprefix("www.")
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS)
