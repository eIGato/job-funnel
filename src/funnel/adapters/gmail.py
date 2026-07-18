"""Adapter: job alerts parsed out of Gmail (Phase 3).

We do not scrape LinkedIn (invariant 1). Instead we turn on saved-search email alerts
and read those messages through the Gmail API. The same trick works for any board that
can send alerts.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from funnel.adapters.base import BaseAdapter, register

if TYPE_CHECKING:
    from pathlib import Path

    from google.oauth2.credentials import Credentials

    from funnel.schemas import NormalizedJob

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


@register
class GmailAlertsAdapter(BaseAdapter):
    """Reads alert emails and extracts job postings from them.

    Expected config keys (Source.config JSONB):
      query: str, a Gmail query such as
             'from:jobalerts-noreply@linkedin.com newer_than:7d'
      max_results: int
    """

    name = "gmail-alerts"

    async def fetch(self) -> list[NormalizedJob]:
        # TODO Phase 3:
        #   1. OAuth flow: settings.gmail_credentials_path -> token_path (InstalledAppFlow).
        #   2. users().messages().list(q=self.config["query"]), then get() per message id.
        #   3. Parse the email HTML into NormalizedJob.
        # Every board marks up its alerts differently, so write this parser against real
        # messages rather than from memory: dump a couple into tests/fixtures/ first.
        raise NotImplementedError("Phase 3: Gmail alert parser")
