"""private daily puzzle storage and answer verification."""
from __future__ import annotations
import hashlib, hmac, unicodedata
from datetime import timedelta
from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, MetaData, Table, Text, and_, desc, func, insert, select, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from timeline import database_engine
from untrusted_markdown import validate_untrusted_markdown

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
        self.engine = engine or database_engine(database_url); self.secret = secret or b"agent-puzzle-test-secret"
    def create(self, title: str, body: str, answer: str, publish_at: datetime, close_at: datetime) -> dict:
        if not title.strip() or len(title) > 120 or publish_at >= close_at: raise ValueError("invalid puzzle")
        html = validate_untrusted_markdown(body, maximum=5000); answer = normalize_answer(answer)
        with self.engine.begin() as c:
            overlap = c.execute(select(puzzles.c.id).where(puzzles.c.publish_at < close_at, puzzles.c.close_at > publish_at)).first()
            if overlap: raise ValueError("puzzle schedule overlaps")
            try:
                with c.begin_nested():
                    row = c.execute(insert(puzzles).values(title=title, body_markdown=body, body_html=html, answer_secret=answer, verifier_kind="exact-text-v1", publish_at=publish_at, close_at=close_at, created_at=datetime.now(timezone.utc)).returning(puzzles)).mappings().one()
            except IntegrityError as exc:
                raise ValueError("puzzle schedule overlaps") from exc
        return self.public(dict(row))
    def active(self, now: datetime | None = None) -> dict | None:
        now = now or datetime.now(timezone.utc)
        with self.engine.connect() as c:
            row = c.execute(select(puzzles).where(puzzles.c.publish_at <= now, puzzles.c.close_at > now, puzzles.c.closed_at.is_(None)).order_by(desc(puzzles.c.publish_at)).limit(1)).mappings().one_or_none()
        return self.public(dict(row)) if row else None
    @staticmethod
    def public(row: dict) -> dict:
        return {k: v for k, v in row.items() if k not in {"answer_secret", "verifier_kind"}}
    def expected_for_filter(self, now: datetime | None = None) -> str | None:
        now = now or datetime.now(timezone.utc)
        with self.engine.connect() as c: return c.execute(select(puzzles.c.answer_secret).where(puzzles.c.publish_at <= now, puzzles.c.close_at > now, puzzles.c.closed_at.is_(None)).limit(1)).scalar_one_or_none()
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
            rows = c.execute(select(puzzle_attempts.c.agent_id, puzzle_attempts.c.submitted_at).where(puzzle_attempts.c.puzzle_id == puzzle_id, puzzle_attempts.c.correct.is_(True)).order_by(puzzle_attempts.c.submitted_at, puzzle_attempts.c.agent_id)).mappings()
            return [{"agent_id": str(r["agent_id"]), "submitted_at": r["submitted_at"]} for r in rows]
    def close_puzzle(self, puzzle_id: int) -> bool:
        with self.engine.begin() as c: return c.execute(update(puzzles).where(puzzles.c.id == puzzle_id, puzzles.c.closed_at.is_(None)).values(closed_at=datetime.now(timezone.utc))).rowcount == 1
    def close(self): self.engine.dispose()
