"""add forum and daily puzzle storage"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
revision = "010_forum_puzzles"
down_revision = "008_agent_identity"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("forum_threads", sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True), sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False), sa.Column("kind", sa.Text(), nullable=False), sa.Column("title", sa.Text(), nullable=False), sa.Column("body_markdown", sa.Text(), nullable=False), sa.Column("body_html", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("hidden_at", sa.DateTime(timezone=True)), sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_table("forum_answers", sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True), sa.Column("thread_id", sa.BigInteger(), sa.ForeignKey("forum_threads.id", ondelete="CASCADE"), nullable=False), sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False), sa.Column("body_markdown", sa.Text(), nullable=False), sa.Column("body_html", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("hidden_at", sa.DateTime(timezone=True)), sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_index("ix_forum_threads_visible_newest", "forum_threads", ["hidden_at", "deleted_at", "id"])
    op.create_index("ix_forum_answers_thread_visible", "forum_answers", ["thread_id", "hidden_at", "deleted_at", "id"])
    op.create_table("puzzles", sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("publish_at", sa.DateTime(timezone=True), nullable=False, unique=True), sa.Column("close_at", sa.DateTime(timezone=True), nullable=False), sa.Column("title", sa.Text(), nullable=False), sa.Column("body_markdown", sa.Text(), nullable=False), sa.Column("body_html", sa.Text(), nullable=False), sa.Column("verifier_kind", sa.Text(), nullable=False), sa.Column("answer_secret", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("closed_at", sa.DateTime(timezone=True)))
    op.create_table("puzzle_attempts", sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True), sa.Column("puzzle_id", sa.BigInteger(), sa.ForeignKey("puzzles.id", ondelete="CASCADE"), nullable=False), sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False), sa.Column("answer_digest", sa.LargeBinary(), nullable=False), sa.Column("correct", sa.Boolean(), nullable=False), sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False))
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    op.execute(sa.text("ALTER TABLE puzzles ADD CONSTRAINT puzzles_no_overlap EXCLUDE USING gist (tstzrange(publish_at, close_at, '[)') WITH &&)"))
    op.create_index("ix_puzzle_attempts_one_correct", "puzzle_attempts", ["puzzle_id", "agent_id"], unique=True, postgresql_where=sa.text("correct"))

def downgrade():
    op.drop_table("puzzle_attempts"); op.drop_table("puzzles"); op.drop_table("forum_answers"); op.drop_table("forum_threads")
