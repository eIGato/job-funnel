"""The active matching profile: assembling it and embedding it once (Phase 4b).

One active profile (multi-profile shelved, PLAN.md section 4). The embedded text is the active
role header (`<active_profile>.md`) followed by the shared `_experience.md`, and it becomes a
single `query:`-side vector that every posting is scored against.

Order — header first, then experience — is deliberate. e5 truncates around 512 tokens and the
concatenated profile is longer, so leading with the header keeps the highest-signal lines
(desired position, skills, technologies) inside the window: that is exactly the "wants this job,
not merely knows the tech" distinction PLAN.md section 4 is built around. The experience detail
fills whatever budget remains. (If full-profile fidelity is ever needed, embed the sections and
mean-pool them — noted, not warranted at this scale.)
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from funnel.config import get_settings
from funnel.matching.embed import embed_texts

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

#: The shared block prepended-role aside; files starting with `_` are not profiles themselves.
SHARED_FILE = "_experience.md"
#: Optional gitignored sample of the human's own writing, fed to drafting/ as a style anchor
#: (never embedded — it must not skew match scores). Absent by default; drafting degrades to
#: its plain instructions when missing.
WRITING_STYLE_FILE = "_writing_style.md"


def load_profile_text() -> str:
    """Assemble the active profile: role header first, then the shared experience block."""
    settings = get_settings()
    header = settings.profiles_dir / f"{settings.active_profile}.md"
    if not header.is_file():
        raise FileNotFoundError(
            f"Active profile not found: {header}. Check `active_profile` and `profiles_dir` "
            "(the CVs are converted into data/profiles/, which is gitignored)."
        )
    parts = [header.read_text(encoding="utf-8").strip()]
    shared = settings.profiles_dir / SHARED_FILE
    if shared.is_file():
        parts.append(shared.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts).strip()


def load_writing_style() -> str:
    """Return the human's writing-style sample, or "" if none is provided.

    A `query:`/`passage:`-free plain read: this text is only ever handed to `drafting/` as a
    tone anchor, never embedded, so it cannot influence matching. Optional by design — a fresh
    checkout has no `data/profiles/` at all.
    """
    sample = get_settings().profiles_dir / WRITING_STYLE_FILE
    if not sample.is_file():
        return ""
    return sample.read_text(encoding="utf-8").strip()


@lru_cache
def get_profile_vector() -> NDArray[np.float32]:
    """Embed the active profile once per process (the query side of the e5 pair)."""
    vector: NDArray[np.float32] = embed_texts([load_profile_text()], is_query=True)[0]
    return vector
