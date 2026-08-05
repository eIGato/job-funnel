"""Which links are dead ends (`matching/apply_route.py`).

The expensive mistake here is a false positive: every host in `BLOCKED_HOSTS` costs real
postings — 131 rows above the 90th percentile on the day the rule was written — so the
"must not block" cases carry as much weight as the hits.
"""

from __future__ import annotations

import pytest

from funnel.matching.apply_route import BLOCKED_HOSTS, is_blocked


@pytest.mark.parametrize(
    "url",
    [
        "https://www.adzuna.com/details/5827174767?utm_medium=api",
        "https://www.adzuna.com/land/ad/5818643666?se=6NyCRqaM8RGNzO_fQd9OLg",
        "https://adzuna.com/details/1",
        "https://www.adzuna.ca/details/5806100244",
        "https://remoteok.com/remote-jobs/remote-python-developer-yo-it-1134923",
        "https://remoteOK.com/remote-jobs/1136092",  # the feed spells its own host this way
    ],
)
def test_a_dead_end_is_blocked(url: str) -> None:
    assert is_blocked(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # Every other Adzuna country serves the human and takes applications.
        "https://www.adzuna.co.uk/details/1",
        "https://www.adzuna.pl/details/1",
        "https://www.adzuna.de/land/ad/1",
        # One country code away from a blocked host, and a live site: a substring rule would
        # have taken this one down with adzuna.com.
        "https://www.adzuna.com.au/details/1",
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "https://jobs.ashbyhq.com/acme/abc",
        "https://www.arbeitnow.com/view/senior-backend-engineer",
        "mailto:jobs@acme.example",
        "",
    ],
)
def test_a_live_link_is_not_blocked(url: str) -> None:
    assert is_blocked(url) is False


def test_a_host_that_merely_contains_a_blocked_name_is_not_blocked() -> None:
    """The rule is a host suffix, not a substring: a real company keeps its own domain."""
    assert is_blocked("https://notremoteok.com/careers/backend") is False
    assert is_blocked("https://careers.adzuna.com.br/1") is False


def test_a_subdomain_of_a_blocked_host_is_blocked() -> None:
    """The block is the site, not one path on it — a 403 does not stop at `www`."""
    assert is_blocked("https://jobs.remoteok.com/1") is True


def test_the_blocked_set_stays_small_and_lowercase() -> None:
    """A guard, not a style rule: `is_blocked` lowercases the host and compares it to these.

    An entry with a capital or a `www.` on it would silently never match, and the failure is
    invisible — the funnel would go on shortlisting postings nobody can apply to.
    """
    assert all(host == host.lower() and not host.startswith("www.") for host in BLOCKED_HOSTS)
