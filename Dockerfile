# rag-real-estate BE — Cloud Run (asia-southeast1) container image.
# Multi-stage: builder installs requirements.txt into an isolated venv; the
# runtime stage ships ONLY that venv plus the runtime code (api/, ingest/).
# Dependency manifest is requirements.txt (pinned; root pyproject.toml is
# tooling-only). No secrets are baked in — Cloud Run injects env vars at deploy.

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Non-root runtime user (created before COPY so --chown can reference it).
RUN useradd --create-home --uid 1000 appuser

COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Runtime code only. api/prompts is required at runtime (settings.prompt_dir =
# <repo>/api/prompts) and ships inside api/. ingest/ is imported by api
# (rag_leg -> ingest.lightrag_init, merge -> ingest.placeholder). db/, data/,
# tests/ and .env are intentionally absent: no runtime import touches them
# (static geo + project registry degrade gracefully without db/seed files).
COPY --chown=appuser:appuser api ./api
COPY --chown=appuser:appuser ingest ./ingest

# LightRAG working_dir (settings.lightrag_workspace default "ragre_mvp",
# relative -> resolved against WORKDIR /app): must exist and be writable by
# the non-root user or LightRAG init fails at runtime.
RUN mkdir -p /app/ragre_mvp \
 && chown appuser:appuser /app/ragre_mvp

USER appuser

# Cloud Run injects PORT (default 8080 here); single uvicorn process per the
# platform contract. No HEALTHCHECK: Cloud Run uses its own HTTP probes.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn api.interfaces.api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
