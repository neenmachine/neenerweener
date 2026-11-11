FROM node:22.14.0-slim AS frontend
WORKDIR /app/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.13.9-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY backend/ /app/backend/
RUN cd /app/backend && uv sync --frozen --no-dev
# The site shows Toronto time; fail the build, not the first request, if the zone data is missing.
RUN /app/backend/.venv/bin/python -c 'from zoneinfo import ZoneInfo; ZoneInfo("America/Toronto")'
COPY posts/ /app/posts/
COPY code/ /app/code/
COPY --from=frontend /app/frontend/dist/ /app/frontend/dist/
RUN useradd --create-home app && chown -R app:app /app
USER app
# shortcut: startup migrations assume one replica; add a PostgreSQL advisory lock before scaling out.
CMD ["sh", "-c", "cd /app/backend && /app/backend/.venv/bin/alembic upgrade head && /app/backend/.venv/bin/python -c 'import app; app.require_post_catalog_ready()' && exec /app/backend/.venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --no-proxy-headers"]
