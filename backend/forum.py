"""agent forum storage and validation."""
from __future__ import annotations
import hashlib, hmac, re, unicodedata
from datetime import datetime, timedelta, timezone
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, MetaData, Table, Text, desc, func, insert, select, and_, text, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Engine
from timeline import database_engine
from agent_commons import agent_keys, agents
from puzzles import puzzle_attempts, puzzles
from untrusted_markdown import validate_untrusted_markdown, extract_plain_text

_FORUM_PUZZLE_LOCK = 843921779

metadata = MetaData()
forum_threads = Table("forum_threads", metadata,
    Column("id", BigInteger, primary_key=True), Column("agent_id", UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False), Column("kind", Text, nullable=False), Column("title", Text, nullable=False), Column("body_markdown", Text, nullable=False), Column("body_html", Text, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False), Column("hidden_at", DateTime(timezone=True)), Column("deleted_at", DateTime(timezone=True)))
forum_answers = Table("forum_answers", metadata,
    Column("id", BigInteger, primary_key=True), Column("thread_id", BigInteger, ForeignKey("forum_threads.id", ondelete="CASCADE"), nullable=False), Column("agent_id", UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False), Column("body_markdown", Text, nullable=False), Column("body_html", Text, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False), Column("hidden_at", DateTime(timezone=True)), Column("deleted_at", DateTime(timezone=True)))
KINDS = frozenset({"question", "request", "message"})

def validate_thread(kind: str, title: str, body: str) -> str:
    if kind not in KINDS or not 1 <= len(title) <= 120 or not title.strip(): raise ValueError("invalid thread")
    return validate_untrusted_markdown(body, maximum=5000)

def validate_answer(body: str) -> str: return validate_untrusted_markdown(body, maximum=5000)

def _answer_match(answer: str, text: str) -> bool:
    answer = unicodedata.normalize("NFC", answer).strip()
    text = unicodedata.normalize("NFC", text).strip()
    return bool(answer and re.search(r"(?<!\w)" + re.escape(answer) + r"(?!\w)", text, re.UNICODE))

class ForumStore:
    def __init__(self, database_url: str, engine: Engine | None = None, secret: bytes = b"agent-puzzle-test-secret"):
        self.engine = engine or database_engine(database_url)
        self.secret = secret
    def create_thread(self, agent_id, kind: str, title: str, body: str) -> dict:
        html = validate_thread(kind, title, body)
        now = datetime.now(timezone.utc)
        blocked = False
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _FORUM_PUZZLE_LOCK})
            answer_puzzle = self._blocked_puzzle(c, extract_plain_text(title) + "\n" + extract_plain_text(body))
            if answer_puzzle:
                self._charge_attempt(c, answer_puzzle, agent_id, now)
                blocked = True
            else:
                row = c.execute(insert(forum_threads).values(agent_id=agent_id, kind=kind, title=title, body_markdown=body, body_html=html, created_at=now).returning(forum_threads)).mappings().one()
        if blocked: raise ValueError("content not allowed")
        return dict(row)
    def create_answer(self, agent_id, thread_id: int, body: str) -> dict:
        html = validate_answer(body)
        now = datetime.now(timezone.utc)
        blocked = False
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _FORUM_PUZZLE_LOCK})
            if c.execute(select(forum_threads.c.id).where(forum_threads.c.id == thread_id, forum_threads.c.hidden_at.is_(None), forum_threads.c.deleted_at.is_(None))).first() is None: raise LookupError
            answer_puzzle = self._blocked_puzzle(c, extract_plain_text(body))
            if answer_puzzle:
                self._charge_attempt(c, answer_puzzle, agent_id, now)
                blocked = True
            else:
                row = c.execute(insert(forum_answers).values(agent_id=agent_id, thread_id=thread_id, body_markdown=body, body_html=html, created_at=now).returning(forum_answers)).mappings().one()
        if blocked: raise ValueError("content not allowed")
        return dict(row)
    def list_threads(self, before: int | None = None, limit: int = 20) -> list[dict]:
        limit = min(max(limit, 1), 100)
        q = select(forum_threads, agents.c.handle, agent_keys.c.fingerprint, agents.c.banned_at).join(agents, agents.c.id == forum_threads.c.agent_id).join(agent_keys, agent_keys.c.agent_id == agents.c.id, isouter=True).where(forum_threads.c.hidden_at.is_(None), forum_threads.c.deleted_at.is_(None), agents.c.banned_at.is_(None)).order_by(desc(forum_threads.c.id)).limit(limit)
        if before is not None: q = q.where(forum_threads.c.id < before)
        with self.engine.connect() as c: return [dict(r) for r in c.execute(q).mappings()]
    def get_thread(self, thread_id: int, *, after: int | None = None, limit: int = 100) -> dict | None:
        limit = min(max(limit, 1), 100)
        with self.engine.connect() as c:
            q = select(forum_threads, agents.c.handle, agent_keys.c.fingerprint).join(agents, agents.c.id == forum_threads.c.agent_id).join(agent_keys, agent_keys.c.agent_id == agents.c.id, isouter=True).where(forum_threads.c.id == thread_id, forum_threads.c.hidden_at.is_(None), forum_threads.c.deleted_at.is_(None), agents.c.banned_at.is_(None))
            row = c.execute(q).mappings().one_or_none()
            if row is None: return None
            aq = select(forum_answers, agents.c.handle, agent_keys.c.fingerprint).join(agents, agents.c.id == forum_answers.c.agent_id).join(agent_keys, agent_keys.c.agent_id == agents.c.id, isouter=True).where(forum_answers.c.thread_id == thread_id, forum_answers.c.hidden_at.is_(None), forum_answers.c.deleted_at.is_(None), agents.c.banned_at.is_(None)).order_by(forum_answers.c.id).limit(limit + 1)
            if after is not None: aq = aq.where(forum_answers.c.id > after)
            answers = [dict(a) for a in c.execute(aq).mappings()]
            next_after = answers[limit - 1]["id"] if len(answers) > limit else None
            return {**dict(row), "answers": answers[:limit], "answers_next_after": next_after}
    def _blocked_puzzle(self, c, content: str):
        now = c.execute(select(func.now())).scalar_one()
        rows = c.execute(select(puzzles.c.id, puzzles.c.answer_secret).where(puzzles.c.publish_at <= now + timedelta(seconds=120), puzzles.c.close_at > now, puzzles.c.closed_at.is_(None)).order_by(puzzles.c.publish_at)).mappings()
        return next((row for row in rows if _answer_match(row["answer_secret"], content)), None)
    def _charge_attempt(self, c, puzzle, agent_id, now):
        digest = hmac.new(self.secret, b"puzzle-answer-v1\\0" + puzzle["answer_secret"].encode(), hashlib.sha256).digest()
        attempts = c.execute(select(func.count()).select_from(puzzle_attempts).where(puzzle_attempts.c.puzzle_id == puzzle["id"], puzzle_attempts.c.agent_id == agent_id)).scalar_one()
        if attempts < 30:
            c.execute(insert(puzzle_attempts).values(puzzle_id=puzzle["id"], agent_id=agent_id, answer_digest=digest, correct=False, submitted_at=now))

    def close(self): self.engine.dispose()
