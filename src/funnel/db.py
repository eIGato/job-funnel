"""Engine and sessions.

The core is synchronous: this is a batch tool driven by a timer, so there is no
concurrency to exploit. Only adapters (httpx) and LLM calls are async, and the CLI
bridges them with asyncio.run() at its boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from funnel.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(str(settings.database_url), pool_pre_ping=True, future=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction: commit on exit, roll back on exception."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
