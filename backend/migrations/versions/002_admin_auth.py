"""create admin sessions and persistent login throttling"""

import sqlalchemy as sa
from alembic import op

revision = "002_admin_auth"
down_revision = "001_timeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("token_digest", sa.LargeBinary(length=32), primary_key=True),
        sa.Column("csrf_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("password_hash_identity", sa.LargeBinary(length=32), nullable=False),
    )
    op.create_index("ix_admin_sessions_expires_at", "admin_sessions", ["expires_at"])
    op.create_table(
        "admin_login_failures",
        sa.Column("bucket_key", sa.LargeBinary(length=32), nullable=False),
        sa.Column("bucket_type", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("bucket_key", "bucket_type"),
        sa.CheckConstraint("octet_length(bucket_key) = 32", name="ck_admin_login_failures_bucket_key_length"),
        sa.CheckConstraint("bucket_type IN ('ip', 'account', 'global')", name="ck_admin_login_failures_bucket_type"),
        sa.CheckConstraint("failure_count > 0", name="ck_admin_login_failures_count_positive"),
    )


def downgrade() -> None:
    op.drop_table("admin_login_failures")
    op.drop_table("admin_sessions")
