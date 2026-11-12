"""add agent canvas storage"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "009_canvas"
down_revision = "008_agent_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canvas_events",
        sa.Column("revision", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("x", sa.SmallInteger(), nullable=True),
        sa.Column("y", sa.SmallInteger(), nullable=True),
        sa.Column("color", sa.SmallInteger(), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("clear_bounds", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("event_kind IN ('place', 'clear')", name="ck_canvas_events_kind"),
        sa.CheckConstraint("(event_kind = 'place' AND x IS NOT NULL AND y IS NOT NULL AND color IS NOT NULL) OR (event_kind = 'clear' AND clear_bounds IS NOT NULL)", name="ck_canvas_events_payload"),
        sa.CheckConstraint("x IS NULL OR x BETWEEN 0 AND 63", name="ck_canvas_events_x"),
        sa.CheckConstraint("y IS NULL OR y BETWEEN 0 AND 63", name="ck_canvas_events_y"),
        sa.CheckConstraint("color IS NULL OR color BETWEEN 0 AND 15", name="ck_canvas_events_color"),
    )
    op.create_table(
        "canvas_cells",
        sa.Column("x", sa.SmallInteger(), primary_key=True), sa.Column("y", sa.SmallInteger(), primary_key=True),
        sa.Column("color", sa.SmallInteger(), nullable=False), sa.Column("revision", sa.BigInteger(), sa.ForeignKey("canvas_events.revision"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("x BETWEEN 0 AND 63", name="ck_canvas_cells_x"), sa.CheckConstraint("y BETWEEN 0 AND 63", name="ck_canvas_cells_y"), sa.CheckConstraint("color BETWEEN 0 AND 15", name="ck_canvas_cells_color"),
    )
    op.create_table("canvas_cooldowns", sa.Column("fingerprint", sa.Text(), primary_key=True), sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("canvas_cooldowns"); op.drop_table("canvas_cells"); op.drop_table("canvas_events")
