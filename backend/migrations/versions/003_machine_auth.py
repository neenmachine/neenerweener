"""add machine challenges, tokens, and persistent throttles"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003_machine_auth"
down_revision = "002_admin_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "machine_auth_challenges",
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key_fingerprint", sa.Text(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=32), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_machine_auth_challenges_expires_at",
        "machine_auth_challenges",
        ["expires_at"],
    )
    op.create_table(
        "machine_auth_tokens",
        sa.Column("token_digest", sa.LargeBinary(length=32), primary_key=True),
        sa.Column("key_fingerprint", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_machine_auth_tokens_expires_at", "machine_auth_tokens", ["expires_at"]
    )
    op.create_index(
        "ix_machine_auth_tokens_key_fingerprint",
        "machine_auth_tokens",
        ["key_fingerprint"],
    )
    op.create_table(
        "machine_auth_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("ip_key", sa.LargeBinary(length=32)),
        sa.Column("fingerprint_key", sa.LargeBinary(length=32)),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_machine_auth_events_event_occurred_at",
        "machine_auth_events",
        ["event", "occurred_at"],
    )
    op.create_index(
        "ix_machine_auth_events_ip_event_occurred_at",
        "machine_auth_events",
        ["ip_key", "event", "occurred_at"],
    )
    op.create_index(
        "ix_machine_auth_events_key_event_occurred_at",
        "machine_auth_events",
        ["fingerprint_key", "event", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("machine_auth_events")
    op.drop_table("machine_auth_tokens")
    op.drop_table("machine_auth_challenges")
