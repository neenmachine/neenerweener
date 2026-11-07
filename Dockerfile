FROM node:22.14.0-slim AS frontend
WORKDIR /app/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.13.9-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY backend/ /app/backend/
RUN cd /app/backend && uv sync --frozen --no-dev
COPY posts/ /app/posts/
COPY pages/ /app/pages/
COPY code/ /app/code/
COPY --from=frontend /app/frontend/dist/ /app/frontend/dist/
RUN useradd --create-home app && chown -R app:app /app
USER app
CMD ["sh", "-c", "cd /app/backend && /app/backend/.venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
