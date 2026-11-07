"""Persistent timeline reads and rendering for untrusted submitted Markdown."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol

from markdown_it import MarkdownIt
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, create_engine, desc, select, text
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
    def schema_revisions(self) -> frozenset[str]: ...
    def close(self) -> None: ...



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

    def schema_revisions(self) -> frozenset[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version"))
            return frozenset(rows.scalars())

    def close(self) -> None:
        self.engine.dispose()


def serialize_entry(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "html": render_timeline(entry["body_markdown"]),
        "created_at": _timestamp(entry["created_at"]),
        "edited_at": _timestamp(entry.get("edited_at")),
    }


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
