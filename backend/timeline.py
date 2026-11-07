"""Persistent timeline reads and rendering for untrusted submitted Markdown."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Protocol

from markdown_it import MarkdownIt
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, create_engine, delete, desc, func, insert, select, text, update
from sqlalchemy.engine import Engine

metadata = MetaData()
timeline_entries = Table(
    "timeline_entries",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("body_markdown", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("edited_at", DateTime(timezone=True), nullable=True),
)

_timeline_md = MarkdownIt("commonmark", {"html": False}).disable("image")
_UNSAFE_SCHEME = re.compile(r"^(?:javascript|vbscript|data):", re.IGNORECASE)
_UNSAFE_MARKDOWN_LINK = re.compile(r"\]\(\s*(?:javascript|vbscript|data):", re.IGNORECASE)


CONNECT_TIMEOUT_SECONDS = 2
POOL_TIMEOUT_SECONDS = 1
STATEMENT_TIMEOUT_MS = 2000


def database_engine(database_url: str, **kwargs) -> Engine:
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return create_engine(database_url, **kwargs)


class TimelineStoreProtocol(Protocol):
    def list(self, before: int | None, limit: int) -> list[dict]: ...
    def get(self, entry_id: int) -> dict | None: ...
    def create(self, body_markdown: str) -> dict: ...
    def update(self, entry_id: int, body_markdown: str) -> dict | None: ...
    def delete(self, entry_id: int) -> bool: ...
    def schema_revisions(self) -> frozenset[str]: ...
    def ping(self) -> None: ...
    def close(self) -> None: ...



def validate_timeline_body(body: str) -> None:
    if not body.strip() or len(body) > 10_000:
        raise ValueError("invalid body")
    render_timeline(body)


def render_timeline(body: str) -> str:
    """Render submitted Markdown without HTML, images, or unsafe links."""
    if _UNSAFE_MARKDOWN_LINK.search(body):
        raise ValueError("unsafe link")
    tokens = _timeline_md.parse(body)
    for token in tokens:
        if token.type == "inline" and token.children:
            for child in token.children:
                if child.type == "link_open":
                    href = child.attrGet("href") or ""
                    if _UNSAFE_SCHEME.match(href.strip()) or href.startswith("\\"):
                        raise ValueError("unsafe link")
    return _timeline_md.renderer.render(tokens, _timeline_md.options, {})


class TimelineStore:
    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(
            database_url,
            pool_pre_ping=True,
            pool_timeout=POOL_TIMEOUT_SECONDS,
            connect_args={
                "connect_timeout": CONNECT_TIMEOUT_SECONDS,
                "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            },
        )

    def list(self, before: int | None, limit: int) -> list[dict]:
        query = select(timeline_entries).order_by(desc(timeline_entries.c.id)).limit(limit + 1)
        if before is not None:
            query = query.where(timeline_entries.c.id < before)
        with self.engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return [dict(row) for row in rows]

    def get(self, entry_id: int) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(timeline_entries).where(timeline_entries.c.id == entry_id)
            ).mappings().one_or_none()
        return dict(row) if row else None

    def create(self, body_markdown: str) -> dict:
        with self.engine.begin() as connection:
            row = connection.execute(
                insert(timeline_entries).values(body_markdown=body_markdown).returning(timeline_entries)
            ).mappings().one()
        return dict(row)

    def update(self, entry_id: int, body_markdown: str) -> dict | None:
        with self.engine.begin() as connection:
            current = connection.execute(
                select(timeline_entries).where(timeline_entries.c.id == entry_id).with_for_update()
            ).mappings().one_or_none()
            if current is None:
                return None
            if current["body_markdown"] == body_markdown:
                return dict(current)
            row = connection.execute(
                update(timeline_entries)
                .where(timeline_entries.c.id == entry_id)
                .values(body_markdown=body_markdown, edited_at=func.now())
                .returning(timeline_entries)
            ).mappings().one()
        return dict(row)

    def delete(self, entry_id: int) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(delete(timeline_entries).where(timeline_entries.c.id == entry_id))
        return result.rowcount == 1

    def schema_revisions(self) -> frozenset[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version"))
            return frozenset(rows.scalars())

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def close(self) -> None:
        self.engine.dispose()


def serialize_entry(entry: dict, *, admin: bool = False) -> dict:
    result = {
        "id": entry["id"],
        "html": render_timeline(entry["body_markdown"]),
        "created_at": _timestamp(entry["created_at"]),
        "edited_at": _timestamp(entry.get("edited_at")),
    }
    if admin:
        result["body_markdown"] = entry["body_markdown"]
    return result


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None
