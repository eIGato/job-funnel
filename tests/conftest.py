from __future__ import annotations

import pytest

from funnel.schemas import NormalizedJob


@pytest.fixture
def job() -> NormalizedJob:
    return NormalizedJob(
        url="https://example.com/jobs/1",
        company="Acme",
        title="Data Engineer",
        description="Build ETL pipelines. Remote, EU timezones.",
        location="Remote",
        is_remote=True,
    )
