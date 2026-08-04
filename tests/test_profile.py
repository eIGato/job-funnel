"""The active-profile assembly (Phase 4b). Offline — no embedding model is loaded here."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from funnel.matching import profile

if TYPE_CHECKING:
    from pathlib import Path


class _Settings:
    def __init__(self, profiles_dir: Path, active_profile: str = "backend") -> None:
        self.profiles_dir = profiles_dir
        self.active_profile = active_profile


def _use_profiles(
    monkeypatch: pytest.MonkeyPatch, directory: Path, active: str = "backend"
) -> None:
    monkeypatch.setattr(profile, "get_settings", lambda: _Settings(directory, active))


def test_profile_leads_with_the_header_then_experience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The header must lead so its high-signal lines survive e5's ~512-token truncation.
    (tmp_path / "backend.md").write_text("# Backend\nDesired: Senior Python", encoding="utf-8")
    (tmp_path / "_experience.md").write_text("# Shared\nWorked at LimeCity", encoding="utf-8")
    _use_profiles(monkeypatch, tmp_path)

    text = profile.load_profile_text()
    assert text.startswith("# Backend")
    assert "Worked at LimeCity" in text
    assert text.index("Desired") < text.index("Worked at LimeCity")


def test_missing_shared_block_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "backend.md").write_text("# Backend\nDesired: Senior Python", encoding="utf-8")
    _use_profiles(monkeypatch, tmp_path)
    assert profile.load_profile_text() == "# Backend\nDesired: Senior Python"


def test_missing_active_profile_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_profiles(monkeypatch, tmp_path, active="nonesuch")
    with pytest.raises(FileNotFoundError):
        profile.load_profile_text()
