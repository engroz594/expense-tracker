# syntax=docker/dockerfile:1

# --- Build stage: resolve and install dependencies into /app/.venv with uv ---
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /bin/uv

# Compile .pyc at build time (faster cold start); copy instead of hard-linking from
# the cache mount; and never download a different Python than the base image's.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Only the lock files first, so the dependency layer is cached until they change.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Runtime stage: same base image (the venv's python symlink points at it) ---
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY routes ./routes
COPY main.py ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 8000

# The same image also runs the worker and migrations (later slices) with a different command.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]