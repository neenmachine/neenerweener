"""create timeline entries"""

from alembic import op
import sqlalchemy as sa

revision = "001_timeline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "timeline_entries",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("timeline_entries")
