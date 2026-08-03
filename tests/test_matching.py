"""Hard filters and cosine similarity: the deterministic, free half of the funnel.

These avoid loading the embedding model, so they stay fast and offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from funnel.matching.embed import (
    centered_similarity,
    cosine_similarity,
    from_bytes,
    percentile_ranks,
    to_bytes,
)
from funnel.matching.filters import passes_hard_filters

if TYPE_CHECKING:
    from funnel.schemas import NormalizedJob


def test_clean_job_passes(job: NormalizedJob) -> None:
    assert passes_hard_filters(job) is True


def test_stop_phrase_in_description_rejects(job: NormalizedJob) -> None:
    blocked = job.model_copy(update={"description": "Must hold an active security clearance."})
    assert passes_hard_filters(blocked) is False


def test_stop_phrase_is_case_insensitive(job: NormalizedJob) -> None:
    blocked = job.model_copy(update={"description": "US CITIZENS ONLY"})
    assert passes_hard_filters(blocked) is False


def test_ru_by_location_is_a_hard_stop(job: NormalizedJob) -> None:
    for loc in ("Moscow", "Россия", "Minsk", "Санкт-Петербург"):
        blocked = job.model_copy(update={"location": loc})
        assert passes_hard_filters(blocked) is False, loc


def test_russia_in_description_only_is_not_a_stop(job: NormalizedJob) -> None:
    # The RU/BY stop keys on the work location, not a passing mention in the body.
    fine = job.model_copy(update={"description": "We integrate with a Russia-based payment API."})
    assert passes_hard_filters(fine) is True


def test_remote_geo_locked_posting_is_rejected(job: NormalizedJob) -> None:
    blocked = job.model_copy(
        update={"is_remote": True, "description": "Remote — must be authorized to work in the US."}
    )
    assert passes_hard_filters(blocked) is False


def test_contractor_welcome_overrides_the_geo_lock(job: NormalizedJob) -> None:
    ok = job.model_copy(
        update={"is_remote": True, "description": "US only, but open to B2B contractors worldwide."}
    )
    assert passes_hard_filters(ok) is True


def test_geo_lock_that_admits_europe_is_not_rejected(job: NormalizedJob) -> None:
    # "must be located in the Americas, Europe, or Israel" still admits the human (Montenegro).
    ok = job.model_copy(
        update={
            "is_remote": True,
            "description": "You must be located in the Americas, Europe, or Israel to apply.",
        }
    )
    assert passes_hard_filters(ok) is True


def test_worldwide_remote_geo_phrase_is_not_rejected(job: NormalizedJob) -> None:
    ok = job.model_copy(
        update={
            "is_remote": True,
            "location": "Anywhere in the World",
            "description": "Remote, must be authorized to work — open worldwide.",
        }
    )
    assert passes_hard_filters(ok) is True


def test_onsite_geo_requirement_is_kept_not_rejected(job: NormalizedJob) -> None:
    # On-site postings state an authorization requirement as a matter of course; keep them
    # (they rank below remote via the sort, not via this predicate).
    kept = job.model_copy(
        update={
            "is_remote": False,
            "location": "Berlin",
            "description": "On-site. Must be authorized to work in the EU.",
        }
    )
    assert passes_hard_filters(kept) is True


def test_cosine_of_identical_vectors_is_one() -> None:
    vector = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    scores = cosine_similarity(vector.reshape(1, -1), vector)
    assert scores.shape == (1,)
    # float32 lands a hair under 1.0, so compare with a tolerance.
    assert float(scores[0]) == pytest.approx(1.0, abs=1e-6)


def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    scores = cosine_similarity(matrix, np.array([0.0, 1.0], dtype=np.float32))
    assert abs(float(scores[0])) < 1e-6


def test_cosine_ranks_rows() -> None:
    matrix = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32)
    scores = cosine_similarity(matrix, np.array([1.0, 0.0], dtype=np.float32))
    assert scores[0] > scores[1] > scores[2]


def test_zero_vector_scores_zero_instead_of_nan() -> None:
    """A missing embedding must not poison the ranking with NaN."""
    matrix = np.array([[0.0, 0.0]], dtype=np.float32)
    scores = cosine_similarity(matrix, np.array([1.0, 0.0], dtype=np.float32))
    assert not np.isnan(scores).any()
    assert scores[0] == np.float32(0.0)


def test_empty_matrix_is_handled() -> None:
    scores = cosine_similarity(
        np.empty((0, 0), dtype=np.float32), np.array([1.0], dtype=np.float32)
    )
    assert scores.shape == (0,)


def test_embedding_bytes_roundtrip() -> None:
    vector = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    assert np.array_equal(from_bytes(to_bytes(vector)), vector)


def test_centering_spreads_a_narrow_band() -> None:
    """The point of centering: separate rows that a shared component squeezes together.

    Every row here carries a large common term (10.0 in the first dimension) plus a small
    distinguishing one, which is what job-posting prose does to e5 cosine. Raw, the three rows
    are nearly tied; centered, they are clearly apart.
    """
    matrix = np.array([[10.0, 1.0], [10.0, 0.5], [10.0, 0.0]], dtype=np.float32)
    profile = np.array([10.0, 1.0], dtype=np.float32)

    raw = cosine_similarity(matrix, profile)
    centered = centered_similarity(matrix, profile)

    assert float(raw.max() - raw.min()) < 0.02
    assert float(centered.max() - centered.min()) > 1.0
    # Centering must preserve the ordering, only widen it.
    assert list(np.argsort(-raw)) == list(np.argsort(-centered))


def test_centering_is_a_noop_on_a_single_row() -> None:
    """One posting is its own centre, so it has nothing to be distinguished from."""
    matrix = np.array([[1.0, 2.0]], dtype=np.float32)
    scores = centered_similarity(matrix, np.array([1.0, 2.0], dtype=np.float32))
    assert scores.shape == (1,)
    assert not np.isnan(scores).any()


def test_centered_similarity_handles_an_empty_matrix() -> None:
    scores = centered_similarity(
        np.empty((0, 0), dtype=np.float32), np.array([1.0], dtype=np.float32)
    )
    assert scores.shape == (0,)


def test_percentile_ranks_span_the_population() -> None:
    ranks = percentile_ranks(np.array([0.1, 0.3, 0.2, 0.4], dtype=np.float32))
    assert list(ranks) == [0.0, 50.0, 25.0, 75.0]


def test_tied_scores_share_a_percentile() -> None:
    ranks = percentile_ranks(np.array([0.5, 0.5, 0.1], dtype=np.float32))
    assert ranks[0] == ranks[1] > ranks[2]


def test_percentile_ranks_handle_an_empty_population() -> None:
    assert percentile_ranks(np.empty((0,), dtype=np.float32)).shape == (0,)


def test_junior_title_is_below_the_middle_floor(job: NormalizedJob) -> None:
    for title in (
        "Junior Python Developer",
        "Backend Intern",
        "Trainee Engineer",
        "Младший разработчик",
    ):
        blocked = job.model_copy(update={"title": title})
        assert passes_hard_filters(blocked) is False, title


def test_a_junior_middle_range_is_kept(job: NormalizedJob) -> None:
    # The floor is Middle; a range that reaches it stays.
    ok = job.model_copy(update={"title": "Junior/Middle Python Developer"})
    assert passes_hard_filters(ok) is True


def test_unspecified_and_senior_levels_are_kept(job: NormalizedJob) -> None:
    for title in ("Backend Developer", "Senior Python Engineer", "Lead Backend Engineer"):
        ok = job.model_copy(update={"title": title})
        assert passes_hard_filters(ok) is True, title


def test_mentoring_juniors_in_the_body_does_not_trip_the_floor(job: NormalizedJob) -> None:
    ok = job.model_copy(
        update={"title": "Backend Engineer", "description": "You will mentor junior developers."}
    )
    assert passes_hard_filters(ok) is True


def test_ml_training_role_is_stopped(job: NormalizedJob) -> None:
    for title in ("Machine Learning Engineer", "ML Engineer", "Deep Learning Scientist"):
        blocked = job.model_copy(update={"title": title})
        assert passes_hard_filters(blocked) is False, title


def test_working_with_ai_is_not_stopped(job: NormalizedJob) -> None:
    # Using AI/LLMs is wanted; only training models as the primary role is stopped.
    for title in ("AI Engineer", "Backend Engineer (LLM apps)", "Python Developer, AI tooling"):
        ok = job.model_copy(update={"title": title})
        assert passes_hard_filters(ok) is True, title


def test_ml_platform_engineering_is_kept(job: NormalizedJob) -> None:
    # Engineering around ML is ordinary backend work, not model training.
    ok = job.model_copy(update={"title": "Machine Learning Platform Engineer"})
    assert passes_hard_filters(ok) is True


def test_junk_titles_are_dropped(job: NormalizedJob) -> None:
    """Scraped page furniture reaches us looking like a posting and embeds at 0.84+."""
    for title in (
        "Job Details",
        "Couldn't pick up that page",
        "Jop posting title",
        "This is a test job",
        "  APPLY NOW  ",
        "Untitled",
    ):
        junk = job.model_copy(update={"title": title})
        assert passes_hard_filters(junk) is False, title


def test_a_bare_generic_title_is_junk(job: NormalizedJob) -> None:
    """RemoteOK row 3110: title "Vacancy", body "asdf" / skills "vim" — a placeholder."""
    junk = job.model_copy(update={"title": "Vacancy"})
    assert passes_hard_filters(junk) is False


def test_junk_match_is_whole_title_not_substring(job: NormalizedJob) -> None:
    """A real role that happens to contain a junk word must survive."""
    for title in ("Senior Engineer - Details", "Backend Developer, Apply Now", "Hiring Manager"):
        real = job.model_copy(update={"title": title})
        assert passes_hard_filters(real) is True, title


def test_html_entities_in_a_junk_title_are_decoded(job: NormalizedJob) -> None:
    junk = job.model_copy(update={"title": "Job&nbsp;Details"})
    assert passes_hard_filters(junk) is False


def test_a_terse_posting_is_kept(job: NormalizedJob) -> None:
    """No minimum-description rule: Telegram postings are short and real (see filters.py)."""
    terse = job.model_copy(update={"description": "Python backend. Remote. Write to @hr."})
    assert passes_hard_filters(terse) is True


def test_a_citizenship_preference_that_accepts_everyone_is_not_a_lock(job: NormalizedJob) -> None:
    """The live false positive (job 2672): the posting says the quiet part right after.

    `_GEO_LOCKED` sees "U.S. Citizens" and reads a hard lock; the very next clause says there
    is none. Without the override, a clean remote Python role was dropped before embedding.
    """
    open_auth = job.model_copy(
        update={
            "description": (
                "Work Authorization: U.S. Citizens and Green Card Holders highly preferred, "
                "all valid work authorizations may apply."
            )
        }
    )
    assert passes_hard_filters(open_auth) is True


def test_a_real_authorization_requirement_still_rejects(job: NormalizedJob) -> None:
    """The override must not swallow the rule it qualifies."""
    for text in (
        "Must be authorized to work in the U.S. without current or future sponsorship.",
        "Must reside in the United States and perform all work within the United States.",
        "Must be a U.S. citizen and speak fluent English.",
    ):
        locked = job.model_copy(update={"description": text})
        assert passes_hard_filters(locked) is False, text


def test_preferred_elsewhere_in_the_body_is_not_consent(job: NormalizedJob) -> None:
    """ "AWS preferred" further down a posting says nothing about work authorization."""
    locked = job.model_copy(
        update={
            "description": (
                "Applicants must be authorized to work in the country where the position is "
                "located without employer sponsorship. Terraform and AWS preferred. We support "
                "all levels of experience."
            )
        }
    )
    assert passes_hard_filters(locked) is False


def test_a_scraped_page_body_is_junk(job: NormalizedJob) -> None:
    """RemoteOK republishes whatever its crawler found, under a title no list can anticipate.

    Real ids, real company names, titles like "UNC" and "Danny" — 98 of 467 RemoteOK rows
    (measured 2026-08-03), and five of them held slots in the top 25.
    """
    for title, body in (
        (
            "UNC",
            "Vip section is displayed first, it is only after site navigations such as menus "
            "and core links. Interested in advertising? Feel free to email Vip at us.",
        ),
        ("Danny", "sfvjfoiwupwuwipfuwfpwu. " * 12),
        (
            "The Atlas Project",
            "It all begins with an idea. Maybe you want to launch a business. Maybe you want "
            "to turn a hobby into something more.",
        ),
        (
            "Joe Armstrong",
            "Coded at night under caffeine. No ads, no tracking, open source. Our GitHub. Our "
            "Twitter. Subscribe to the RSS feed. We use cookies to improve UX.",
        ),
        ("test", "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod. " * 3),
    ):
        junk = job.model_copy(update={"title": title, "description": body})
        assert passes_hard_filters(junk) is False, title


def test_a_short_body_is_never_judged_for_vocabulary(job: NormalizedJob) -> None:
    """A whole Telegram posting fits in two sentences and names none of the hiring words.

    The floor is what keeps the vocabulary rule from re-introducing the minimum-description
    filter this module deliberately does not have.
    """
    terse = job.model_copy(
        update={"title": "Backend Developer", "description": "Build ETL pipelines. Remote, CET."}
    )
    assert passes_hard_filters(terse) is True


def test_a_real_posting_body_survives_the_vocabulary_test(job: NormalizedJob) -> None:
    """One hiring word anywhere in title or body is enough — the bar is deliberately low."""
    for body in (
        "We are looking for someone to own our ingestion pipeline.",
        "Wir sind Bertrandt. Deine Aufgaben umfassen die Entwicklung von Backend-Diensten.",
        "Miejsce pracy: Warszawa. Wymagania: Python, PostgreSQL.",
        "Обязанности: разработка сервисов на Python. Требования: опыт от трёх лет.",
    ):
        real = job.model_copy(update={"title": "Backend Developer", "description": body})
        assert passes_hard_filters(real) is True, body


def test_a_tag_list_body_is_not_judged_for_vocabulary(job: NormalizedJob) -> None:
    """A gmail alert's body is a technology list: it names no duties, and it is still real.

    116 of 119 gmail rows carry no hiring word at all. A sentence terminator is what tells
    prose from a tag list, and a tag list has none.
    """
    alert = job.model_copy(
        update={"title": "Tech Lead Python", "description": "KVM, SQL, Python, FastAPI, Linux"}
    )
    assert passes_hard_filters(alert) is True


def test_a_truncated_teaser_is_not_judged_for_vocabulary(job: NormalizedJob) -> None:
    """Adzuna cuts its body off before the requirements; so does a republished LinkedIn teaser.

    Dropping these would have cost 39 real postings, several of them the best-scoring rows in
    the table. The mojibake spellings are double-encoded UTF-8 and all end in a broken bar.
    """
    for body in (
        "At JetBrains, code is our passion. Ever since we started back in 2000 we have…",
        "Wir sind Bertrandt. Ein internationaler Engineering Dienstleister mit langer...",
        "Devoted Studios is a remote game development companyÃ¢Â€Â¦See this on LinkedIn.",
    ):
        teaser = job.model_copy(update={"title": "Backend Developer", "description": body})
        assert passes_hard_filters(teaser) is True, body


def test_ru_by_location_knows_more_than_the_two_capitals(job: NormalizedJob) -> None:
    """Regression (2026-08-03): the rule listed Moscow and St Petersburg and nothing else.

    Boards name a bare city, so "Томск" and "Уфа" walked straight past it — one of them at the
    99.7th percentile, into the head of the shortlist.
    """
    for location in (
        "Томск",
        "Уфа",
        "Краснодар, Ростов-на-Дону",
        "Новосибирск",
        "Владивосток",
        "Ижевск, Казань, Краснодар",
        "Колпино",
        "Нижний Новгород",
        "Novosibirsk",
        "Yekaterinburg",
        "Rostov-on-Don",
        "REMOTE RUSSIA",
        "Минск (Беларусь)",
        "Gomel",
    ):
        ru = job.model_copy(update={"location": location})
        assert passes_hard_filters(ru) is False, location


def test_the_location_rule_does_not_fire_on_an_ambiguity(job: NormalizedJob) -> None:
    """Ambiguous Latin names are left out on purpose: their Cyrillic stems carry the real case."""
    for location in (
        "Perm Street, London",
        "Brest, France",
        "Vladimir Business Park, Texas",
        "Tulare, California",
        "Tbilisi",
        "Belgrade",
        "Podgorica",
        "Berlin",
        "Anywhere in the World",
    ):
        elsewhere = job.model_copy(update={"location": location})
        assert passes_hard_filters(elsewhere) is True, location
