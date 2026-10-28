# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

ARG RADIO247_UID=1000
ARG RADIO247_GID=1000
RUN groupadd --gid "$RADIO247_GID" radio247 \
    && useradd --uid "$RADIO247_UID" --gid "$RADIO247_GID" --create-home radio247 \
    && mkdir -p /data /cache /media /images /output /run/radio247 \
    && chown radio247:radio247 /data /cache /media /images /output /run/radio247

WORKDIR /app

FROM base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM builder AS test
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --dev \
    && python -c "import av; av.Codec('aac', 'w'); av.Codec('libx264', 'w')" \
    && chown -R radio247:radio247 /app
USER radio247
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
COPY --from=builder /app /app
RUN python -c "import av; av.Codec('aac', 'w'); av.Codec('libx264', 'w')"
USER radio247
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["python", "-m", "radio247", "serve"]
