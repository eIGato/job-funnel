# uv image with Python 3.14 already baked in (see the stack section of CLAUDE.md).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    # `uv run` re-syncs by default, which would reinstall dev tooling and rebuild the
    # project on every timer run, and would need PyPI reachable to do it. The image is
    # already fully installed, so pin it shut: the container stays self-contained offline.
    UV_NO_SYNC=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies in their own layer, so they rebuild only when the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# The entry point is the CLI; the caller supplies the command:
#   docker compose run --rm app uv run funnel run-funnel
CMD ["uv", "run", "funnel", "--help"]
