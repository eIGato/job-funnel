"""Reply-channel detection: it decides the shape of every draft, so it gets its own test."""

from __future__ import annotations

import pytest

from funnel.models import ApplyChannel, detect_apply_channel


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("mailto:jobs@acme.com", ApplyChannel.EMAIL),
        ("MAILTO:Jobs@Acme.com?subject=Hi", ApplyChannel.EMAIL),
        ("  mailto:jobs@acme.com  ", ApplyChannel.EMAIL),
        ("https://t.me/some_hr", ApplyChannel.TELEGRAM),
        ("https://www.t.me/some_hr", ApplyChannel.TELEGRAM),
        ("http://telegram.me/some_hr", ApplyChannel.TELEGRAM),
        ("https://remoteok.com/remote-jobs/123", ApplyChannel.FORM),
        ("https://teletype.in/@courierus/EXXvMk6FF8w", ApplyChannel.FORM),
        ("", ApplyChannel.FORM),
    ],
)
def test_detect_apply_channel(url: str, expected: ApplyChannel) -> None:
    assert detect_apply_channel(url) == expected


def test_detect_apply_channel_is_not_fooled_by_lookalike_hosts() -> None:
    """`t.me.evil.com` is not Telegram — match the host, never a substring of the URL."""
    assert detect_apply_channel("https://t.me.evil.com/phish") == ApplyChannel.FORM
    assert detect_apply_channel("https://example.com/?ref=t.me") == ApplyChannel.FORM
