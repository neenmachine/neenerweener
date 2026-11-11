"""add web-created posts, history, tags, slugs, and media ownership"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "006_web_posts"
down_revision = "005_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_posts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("published_on", sa.Date()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_web_posts_title_length"),
        sa.CheckConstraint("excerpt IS NULL OR char_length(excerpt) <= 2000", name="ck_web_posts_excerpt_length"),
        sa.CheckConstraint("char_length(body_markdown) <= 1000000", name="ck_web_posts_body_length"),
        sa.CheckConstraint("status IN ('draft', 'live')", name="ck_web_posts_status"),
        sa.CheckConstraint("version > 0", name="ck_web_posts_version_positive"),
        sa.CheckConstraint(
            "status = 'draft' OR (published_on IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_web_posts_live_publication",
        ),
    )
    op.create_table(
        "web_post_slugs",
        sa.Column("slug", sa.Text(), primary_key=True),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("web_posts.id"), nullable=False),
        sa.Column("canonical", sa.Boolean(), nullable=False),
        sa.CheckConstraint("char_length(slug) BETWEEN 1 AND 100", name="ck_web_post_slugs_length"),
        sa.CheckConstraint("slug ~ '^[a-z0-9-]+$'", name="ck_web_post_slugs_format"),
    )
    op.create_index(
        "uq_web_post_slugs_canonical_post",
        "web_post_slugs",
        ["post_id"],
        unique=True,
        postgresql_where=sa.text("canonical"),
    )
    op.create_table(
        "web_post_tags",
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("web_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("post_id", "position"),
        sa.UniqueConstraint("post_id", "tag"),
        sa.CheckConstraint("position BETWEEN 0 AND 19", name="ck_web_post_tags_position"),
        sa.CheckConstraint("char_length(tag) BETWEEN 1 AND 50", name="ck_web_post_tags_length"),
    )
    op.create_table(
        "web_post_media",
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("web_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_assets.id"), nullable=False),
        sa.PrimaryKeyConstraint("post_id", "asset_id"),
        sa.UniqueConstraint("asset_id"),
    )
    op.create_table(
        "web_post_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("web_posts.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("published_on", sa.Date()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.UniqueConstraint("post_id", "version"),
        sa.CheckConstraint("version > 0", name="ck_web_post_revisions_version_positive"),
        sa.CheckConstraint(
            "action IN ('save', 'publish', 'unpublish', 'rename', 'delete', 'restore')",
            name="ck_web_post_revisions_action",
        ),
    )


def downgrade() -> None:
    op.drop_table("web_post_revisions")
    op.drop_table("web_post_media")
    op.drop_table("web_post_tags")
    op.drop_index("uq_web_post_slugs_canonical_post", table_name="web_post_slugs")
    op.drop_table("web_post_slugs")
    op.drop_table("web_posts")
