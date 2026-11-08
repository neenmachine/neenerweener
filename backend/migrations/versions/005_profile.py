"""add singleton site profile"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "005_profile"
down_revision = "004_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_profile",
        sa.Column("id", sa.SmallInteger(), primary_key=True, server_default="1"),
        sa.Column("display_name", sa.Text(), nullable=False, server_default="neenerweener"),
        sa.Column("handle", sa.Text()),
        sa.Column("avatar_asset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_assets.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("id = 1", name="ck_site_profile_singleton"),
        sa.CheckConstraint("char_length(btrim(display_name)) BETWEEN 1 AND 80", name="ck_site_profile_display_name"),
        sa.CheckConstraint("handle IS NULL OR handle ~ '^[A-Za-z0-9_]{1,30}$'", name="ck_site_profile_handle"),
        sa.UniqueConstraint("avatar_asset_id"),
    )
    op.execute(sa.text("INSERT INTO site_profile (id) VALUES (1)"))


def downgrade() -> None:
    op.drop_table("site_profile")
