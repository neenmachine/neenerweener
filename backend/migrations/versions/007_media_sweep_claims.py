"""serialize media sweeping with attachment"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "007_media_sweep_claims"
down_revision = "006_web_posts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_sweep_claims",
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("media_sweep_claims")
