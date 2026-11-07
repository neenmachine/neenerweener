"""add media assets and timeline entry attachments"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "004_media"
down_revision = "003_machine_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("detached_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("char_length(alt_text) <= 1000", name="ck_media_assets_alt_text_length"),
    )
    op.create_table(
        "timeline_entry_media",
        sa.Column(
            "entry_id",
            sa.BigInteger(),
            sa.ForeignKey("timeline_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_assets.id"), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("entry_id", "position"),
        sa.UniqueConstraint("asset_id"),
        sa.CheckConstraint("position BETWEEN 0 AND 3", name="ck_timeline_entry_media_position"),
    )


def downgrade() -> None:
    op.drop_table("timeline_entry_media")
    op.drop_table("media_assets")
