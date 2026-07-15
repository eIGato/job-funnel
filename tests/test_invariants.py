"""Guards for the invariants in CLAUDE.md.

Invariants that must not be broken deserve a machine checking them, not a reviewer's eyes.
A failure here is not "fix the test", it is "you broke a boundary of the project".
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "funnel"

#: Invariant 4: the LLM lives only here. Everything else is deterministic code.
LLM_ALLOWED = {"drafting", "replies", "orchestration"}


def _modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_llm_calls_confined_to_drafting_and_replies() -> None:
    """pydantic-ai must not leak into ingest, filters or matching."""
    offenders = [
        path.relative_to(SRC)
        for path in _modules()
        if "pydantic_ai" in path.read_text(encoding="utf-8")
        and not LLM_ALLOWED.intersection(path.parts)
    ]
    assert not offenders, f"pydantic-ai used outside {sorted(LLM_ALLOWED)}: {offenders}"


def test_matching_has_no_llm() -> None:
    """Matching must stay free: not a single token."""
    for path in (SRC / "matching").rglob("*.py"):
        assert "pydantic_ai" not in path.read_text(encoding="utf-8"), (
            f"LLM in matching: {path.name}"
        )


def test_no_torch_in_lockfile() -> None:
    """Invariant 3: embeddings run on ONNX. torch must not arrive, even transitively."""
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    for banned in ('name = "torch"', 'name = "nvidia-cublas-cu12"', 'name = "triton"'):
        assert banned not in lock, f"{banned} showed up in the lockfile"


def test_onnxruntime_present() -> None:
    """The flip side: fastembed must still be pulling in the ONNX runtime."""
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "onnxruntime"' in lock


def test_no_heavy_framework() -> None:
    """Invariant 6: no Django."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(pyproject["project"]["dependencies"]).casefold()
    assert "django" not in deps


def test_gmail_scope_is_readonly() -> None:
    """Invariant 2: the system cannot send, so it has no use for a write scope."""
    from funnel.adapters.gmail import GMAIL_SCOPES

    assert GMAIL_SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert not any("send" in scope or "compose" in scope for scope in GMAIL_SCOPES)


def test_admin_forms_never_expose_delete_orphan_relations() -> None:
    """Invariant 6: the admin is for review, so no form field may destroy data.

    sqladmin renders relationship fields by default. On a delete-orphan relationship an empty
    submit deletes the orphans: saving a Source wiped every Job of that source, and saving a
    Job deleted its Application together with the cover letter. Both were real, both were
    reachable with one click.
    """
    from sqlalchemy import inspect as sa_inspect

    from funnel.admin import ApplicationAdmin, JobAdmin, SourceAdmin

    for view in (SourceAdmin, JobAdmin, ApplicationAdmin):
        excluded = {attr.key for attr in getattr(view, "form_excluded_columns", [])}
        for rel in sa_inspect(view.model).relationships:
            if "delete-orphan" in rel.cascade:
                assert rel.key in excluded, (
                    f"{view.model.__name__}.{rel.key} is delete-orphan but reachable from the "
                    f"{view.__name__} form: saving that form would delete rows"
                )


def test_content_hash_has_a_single_definition() -> None:
    """Dedup relies on ingest and the admin agreeing on the hash, byte for byte.

    Two implementations would drift and quietly start creating duplicates.
    """
    from funnel.models import compute_content_hash
    from funnel.schemas import NormalizedJob

    posting = NormalizedJob(
        url="https://example.com/jobs/1",
        company="Acme",
        title="Data Engineer",
    )
    assert posting.content_hash == compute_content_hash(
        "Acme", "Data Engineer", "https://example.com/jobs/1"
    )


def test_content_hash_is_filled_on_write() -> None:
    """content_hash is NOT NULL and kept off the admin form, so a hook must derive it.

    Without the hook, creating a Job anywhere but ingest raises NotNullViolation.
    """
    from sqlalchemy import event

    from funnel.models import Job, _fill_content_hash

    for hook in ("before_insert", "before_update"):
        assert event.contains(Job, hook, _fill_content_hash), f"Job.{hook} hook is missing"


@pytest.mark.parametrize("secret", ["password", "api_key", "secret"])
def test_no_hardcoded_secrets(secret: str) -> None:
    """Invariant 7: secrets live in .env only."""
    for path in _modules():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            lhs, _, rhs = stripped.partition("=")
            if secret in lhs.casefold() and rhs.strip().startswith(('"', "'")):
                # An empty string or None default is not a secret.
                assert rhs.strip().strip("\"'") == "", f"looks like a hardcoded secret: {path}"
