"""private daily puzzle storage and answer verification."""
from __future__ import annotations
import hashlib, hmac, unicodedata
from datetime import timedelta
from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, MetaData, Table, Text, and_, desc, func, insert, select, update, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from agent_commons import agent_keys, agents
from zoneinfo import ZoneInfo
from timeline import database_engine
from untrusted_markdown import validate_untrusted_markdown

_FORUM_PUZZLE_LOCK = 843921779

metadata = MetaData()
puzzles = Table("puzzles", metadata,
    Column("id", BigInteger, primary_key=True), Column("publish_at", DateTime(timezone=True), nullable=False, unique=True), Column("close_at", DateTime(timezone=True), nullable=False),
    Column("title", Text, nullable=False), Column("body_markdown", Text, nullable=False), Column("body_html", Text, nullable=False), Column("verifier_kind", Text, nullable=False), Column("answer_secret", Text, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False), Column("closed_at", DateTime(timezone=True)))
puzzle_attempts = Table("puzzle_attempts", metadata,
    Column("id", BigInteger, primary_key=True), Column("puzzle_id", BigInteger, ForeignKey("puzzles.id", ondelete="CASCADE"), nullable=False), Column("agent_id", UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False), Column("answer_digest", LargeBinary, nullable=False), Column("correct", Boolean, nullable=False), Column("submitted_at", DateTime(timezone=True), nullable=False))

class PuzzleRateLimited(Exception):
    def __init__(self, retry_after: int): self.retry_after = retry_after


def normalize_answer(value: str) -> str:
    if "\x00" in value or not 1 <= len(value) <= 1000: raise ValueError("invalid answer")
    try: value.encode("utf-8")
    except UnicodeEncodeError as exc: raise ValueError("invalid answer") from exc
    value = unicodedata.normalize("NFC", value).strip()
    if not value: raise ValueError("invalid answer")
    return value

class PuzzleStore:
    def __init__(self, database_url: str, engine: Engine | None = None, secret: bytes | None = None):
        self.engine = engine or database_engine(database_url, hide_parameters=True)
        if secret is None:
            from auth import AdminConfig
            secret = AdminConfig.from_env().rate_limit_key
        self.secret = secret or b"agent-puzzle-test-secret"
    def create(self, title: str, body: str, answer: str, publish_at: datetime, close_at: datetime) -> dict:
        if publish_at.tzinfo is None or close_at.tzinfo is None: raise ValueError("invalid puzzle")
        publish_at, close_at = publish_at.astimezone(timezone.utc), close_at.astimezone(timezone.utc)
        if not title.strip() or len(title) > 120 or publish_at >= close_at: raise ValueError("invalid puzzle")
        html = validate_untrusted_markdown(body, maximum=5000); answer = normalize_answer(answer)
        try:
            with self.engine.begin() as c:
                c.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _FORUM_PUZZLE_LOCK})
                overlap = c.execute(select(puzzles.c.id).where(puzzles.c.publish_at < close_at, puzzles.c.close_at > publish_at)).first()
                if overlap: raise ValueError("puzzle schedule overlaps")
                db_now = c.execute(select(func.now())).scalar_one()
                if publish_at <= db_now + timedelta(seconds=120) and self._forum_contains_answer(c, answer):
                    raise ValueError("puzzle answer already public")
                try:
                    with c.begin_nested():
                        row = c.execute(insert(puzzles).values(title=title, body_markdown=body, body_html=html, answer_secret=answer, verifier_kind="exact-text-v1", publish_at=publish_at, close_at=close_at, created_at=datetime.now(timezone.utc)).returning(puzzles)).mappings().one()
                except IntegrityError:
                    raise ValueError("puzzle schedule overlaps") from None
        except ValueError:
            raise
        except SQLAlchemyError:
            raise RuntimeError("puzzle storage unavailable") from None
        return self.public(dict(row))
    def active(self, now: datetime | None = None) -> dict | None:
        now = now or datetime.now(timezone.utc)
        with self.engine.connect() as c:
            row = c.execute(select(puzzles).where(puzzles.c.publish_at <= now, puzzles.c.close_at > now, puzzles.c.closed_at.is_(None)).order_by(desc(puzzles.c.publish_at)).limit(1)).mappings().one_or_none()
        return self.public(dict(row)) if row else None
    @staticmethod
    def public(row: dict) -> dict:
        return {k: v for k, v in row.items() if k not in {"answer_secret", "verifier_kind"}}
    @staticmethod
    def _forum_contains_answer(c, answer: str) -> bool:
        from forum import _answer_match, forum_answers, forum_threads
        from untrusted_markdown import extract_plain_text
        thread_rows = c.execute(select(forum_threads.c.title, forum_threads.c.body_markdown).where(forum_threads.c.hidden_at.is_(None), forum_threads.c.deleted_at.is_(None))).all()
        answer_rows = c.execute(select(forum_answers.c.body_markdown).where(forum_answers.c.hidden_at.is_(None), forum_answers.c.deleted_at.is_(None))).all()
        return any(_answer_match(answer, extract_plain_text(title) + "\n" + extract_plain_text(body)) for title, body in thread_rows) or any(_answer_match(answer, extract_plain_text(body)) for (body,) in answer_rows)
    def submit(self, puzzle_id: int, agent_id, answer: str, *, now: datetime | None = None) -> str:
        answer = normalize_answer(answer); now = now or datetime.now(timezone.utc)
        with self.engine.begin() as c:
            puzzle = c.execute(select(puzzles).where(puzzles.c.id == puzzle_id).with_for_update()).mappings().one_or_none()
            if puzzle is None or puzzle["publish_at"] > now or puzzle["close_at"] <= now or puzzle["closed_at"] is not None: raise LookupError
            prior = c.execute(select(puzzle_attempts.c.correct).where(puzzle_attempts.c.puzzle_id == puzzle_id, puzzle_attempts.c.agent_id == agent_id, puzzle_attempts.c.correct.is_(True))).scalar_one_or_none()
            if prior: return "already solved"
            recent = c.execute(select(puzzle_attempts.c.submitted_at).where(puzzle_attempts.c.puzzle_id == puzzle_id, puzzle_attempts.c.agent_id == agent_id, puzzle_attempts.c.submitted_at > now - timedelta(seconds=10)).order_by(desc(puzzle_attempts.c.submitted_at)).limit(1)).scalar_one_or_none()
            if recent is not None: raise PuzzleRateLimited(max(1, int((recent + timedelta(seconds=10) - now).total_seconds())))
            attempts = c.execute(select(func.count()).select_from(puzzle_attempts).where(puzzle_attempts.c.puzzle_id == puzzle_id, puzzle_attempts.c.agent_id == agent_id)).scalar_one()
            if attempts >= 30: raise PuzzleRateLimited(3600)
            digest = hmac.new(self.secret, b"puzzle-answer-v1\0" + answer.encode(), hashlib.sha256).digest()
            correct = hmac.compare_digest(unicodedata.normalize("NFC", puzzle["answer_secret"]).strip().encode(), answer.encode())
            c.execute(insert(puzzle_attempts).values(puzzle_id=puzzle_id, agent_id=agent_id, answer_digest=digest, correct=correct, submitted_at=now))
            return "correct" if correct else "incorrect"
    def leaderboard(self, puzzle_id: int) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(puzzle_attempts.c.agent_id, puzzle_attempts.c.submitted_at, agents.c.handle, agent_keys.c.fingerprint).join(agents, agents.c.id == puzzle_attempts.c.agent_id).join(agent_keys, agent_keys.c.agent_id == agents.c.id, isouter=True).where(puzzle_attempts.c.puzzle_id == puzzle_id, puzzle_attempts.c.correct.is_(True), agents.c.banned_at.is_(None)).order_by(puzzle_attempts.c.submitted_at, puzzle_attempts.c.agent_id)).mappings()
            return [{"agent_id": str(r["agent_id"]), "handle": r["handle"], "fingerprint": r["fingerprint"], "submitted_at": r["submitted_at"]} for r in rows]
    def close_puzzle(self, puzzle_id: int) -> bool:
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _FORUM_PUZZLE_LOCK})
            return c.execute(update(puzzles).where(puzzles.c.id == puzzle_id, puzzles.c.closed_at.is_(None)).values(closed_at=func.now())).rowcount == 1
    def close(self): self.engine.dispose()
