"""Text repairs that belong to no single source, and to no single layer.

Kept out of `adapters/util.py` on purpose: importing that module pulls in `funnel.adapters`,
which imports every adapter, which imports `funnel.schemas` — and the schema is exactly where
this has to run. A leaf module with no funnel imports of its own breaks that cycle.
"""

from __future__ import annotations

from ftfy import fix_encoding


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 that a feed decoded as cp1252 before serving it back as UTF-8.

    RemoteOK and arbeitnow both do this, sometimes twice over: `…` leaves as `â€¦` and arrives
    as `Ã¢Â€Â¦`, `München` as `MÃ¼nchen`, a Russian location as `Ð Ð°Ð·Ð²Ñ`. It reached 513 of
    2853 rows (measured 2026-08-03) — 96 of them in the company or title, which are part of the
    dedup key, so the damage was not cosmetic: it decided identity, and the rest went into the
    embedding as noise.

    `fix_encoding` and not `fix_text`: only the encoding round-trip is repaired. The rest of
    what ftfy offers — folding ligatures, straightening quotes, stripping zero-width characters
    — is editing a posting's words, which is not ours to do. A string with no mojibake in it
    comes back unchanged, so this is safe to run over everything: verified against `château`,
    `Ação`, `München`, `naïve`, `Ünal Öz`, `€50,000` and Cyrillic.
    """
    return fix_encoding(text) if text else text
