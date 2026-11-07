"""create admin sessions and persistent login throttling"""

from alembic import op
import sqlalchemy as sa

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
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("client_ip", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_admin_login_failures_occurred_at", "admin_login_failures", ["occurred_at"])
    op.create_index(
        "ix_admin_login_failures_client_ip_occurred_at",
        "admin_login_failures",
        ["client_ip", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("admin_login_failures")
    op.drop_table("admin_sessions")
