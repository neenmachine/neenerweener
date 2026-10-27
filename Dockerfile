FROM node:22-slim AS frontend
WORKDIR /app/frontend
RUN corepack enable && corepack prepare pnpm@11 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
WORKDIR /app
COPY backend/ /app/backend/
RUN cd /app/backend && uv sync --frozen --no-dev
COPY posts/ /app/posts/
COPY --from=frontend /app/frontend/dist/ /app/frontend/dist/
CMD ["sh", "-c", "cd /app/backend && uv run uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
