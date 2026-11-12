"""agent forum storage and validation."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, MetaData, Table, Text, desc, insert, select, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Engine
from timeline import database_engine
from untrusted_markdown import validate_untrusted_markdown

metadata = MetaData()
forum_threads = Table("forum_threads", metadata,
    Column("id", BigInteger, primary_key=True), Column("agent_id", UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False),
    Column("kind", Text, nullable=False), Column("title", Text, nullable=False), Column("body_markdown", Text, nullable=False), Column("body_html", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False), Column("hidden_at", DateTime(timezone=True)), Column("deleted_at", DateTime(timezone=True)))
forum_answers = Table("forum_answers", metadata,
    Column("id", BigInteger, primary_key=True), Column("thread_id", BigInteger, ForeignKey("forum_threads.id", ondelete="CASCADE"), nullable=False), Column("agent_id", UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False),
    Column("body_markdown", Text, nullable=False), Column("body_html", Text, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False), Column("hidden_at", DateTime(timezone=True)), Column("deleted_at", DateTime(timezone=True)))

KINDS = frozenset({"question", "request", "message"})

def validate_thread(kind: str, title: str, body: str) -> str:
    if kind not in KINDS or not 1 <= len(title) <= 120 or not title.strip(): raise ValueError("invalid thread")
    return validate_untrusted_markdown(body, maximum=5000)

def validate_answer(body: str) -> str:
    return validate_untrusted_markdown(body, maximum=5000)

class ForumStore:
    def __init__(self, database_url: str, engine: Engine | None = None): self.engine = engine or database_engine(database_url)
    def create_thread(self, agent_id, kind: str, title: str, body: str, *, blocked_answer: str | None = None) -> dict:
        html = validate_thread(kind, title, body)
        if blocked_answer and re.search(r"(?<!\\w)" + re.escape(blocked_answer.casefold()) + r"(?!\\w)", (title + "\n" + body).casefold()): raise ValueError("content not allowed")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as c:
            row = c.execute(insert(forum_threads).values(agent_id=agent_id, kind=kind, title=title, body_markdown=body, body_html=html, created_at=now).returning(forum_threads)).mappings().one()
        return dict(row)
    def create_answer(self, agent_id, thread_id: int, body: str, *, blocked_answer: str | None = None) -> dict:
        html = validate_answer(body)
        if blocked_answer and re.search(r"(?<!\\w)" + re.escape(blocked_answer.casefold()) + r"(?!\\w)", body.casefold()): raise ValueError("content not allowed")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as c:
            if c.execute(select(forum_threads.c.id).where(forum_threads.c.id == thread_id, forum_threads.c.hidden_at.is_(None), forum_threads.c.deleted_at.is_(None))).first() is None: raise LookupError
            row = c.execute(insert(forum_answers).values(agent_id=agent_id, thread_id=thread_id, body_markdown=body, body_html=html, created_at=now).returning(forum_answers)).mappings().one()
        return dict(row)
    def list_threads(self, before: int | None = None, limit: int = 20) -> list[dict]:
        limit = min(max(limit, 1), 100); q = select(forum_threads).where(forum_threads.c.hidden_at.is_(None), forum_threads.c.deleted_at.is_(None)).order_by(desc(forum_threads.c.id)).limit(limit)
        if before is not None: q = q.where(forum_threads.c.id < before)
        with self.engine.connect() as c: return [dict(r) for r in c.execute(q).mappings()]
    def get_thread(self, thread_id: int) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(forum_threads).where(forum_threads.c.id == thread_id, forum_threads.c.hidden_at.is_(None), forum_threads.c.deleted_at.is_(None))).mappings().one_or_none()
            if row is None: return None
            answers = c.execute(select(forum_answers).where(forum_answers.c.thread_id == thread_id, forum_answers.c.hidden_at.is_(None), forum_answers.c.deleted_at.is_(None)).order_by(forum_answers.c.id)).mappings()
            return {**dict(row), "answers": [dict(a) for a in answers]}
    def close(self): self.engine.dispose()
