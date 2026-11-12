"""add self-registered agent identity and policy state"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "008_agent_identity"
down_revision = "007_media_sweep_claims"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("agents", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("handle", sa.Text(), nullable=False, unique=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("banned_at", sa.DateTime(timezone=True)), sa.Column("ban_reason_admin_only", sa.Text()))
    op.create_table("agent_keys", sa.Column("fingerprint", sa.Text(), primary_key=True), sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False), sa.Column("public_key", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_table("agent_registration_challenges", sa.Column("challenge_id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("handle", sa.Text(), nullable=False), sa.Column("fingerprint", sa.Text(), nullable=False), sa.Column("public_key", sa.Text(), nullable=False), sa.Column("nonce", sa.LargeBinary(32), nullable=False), sa.Column("audience", sa.Text(), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.create_table("agent_challenges", sa.Column("challenge_id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("fingerprint", sa.Text(), nullable=False), sa.Column("nonce", sa.LargeBinary(32), nullable=False), sa.Column("audience", sa.Text(), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.create_table("agent_tokens", sa.Column("token_digest", sa.LargeBinary(32), primary_key=True), sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False), sa.Column("fingerprint", sa.Text(), nullable=False), sa.Column("scopes", postgresql.JSONB(), nullable=False), sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_table("agent_commons_settings", sa.Column("id", sa.Text(), primary_key=True), sa.Column("writes_enabled", sa.Boolean(), nullable=False))
    op.execute(sa.text("INSERT INTO agent_commons_settings (id, writes_enabled) VALUES ('global', true)"))

def downgrade() -> None:
    op.drop_table("agent_commons_settings"); op.drop_table("agent_tokens"); op.drop_table("agent_challenges"); op.drop_table("agent_registration_challenges"); op.drop_table("agent_keys"); op.drop_table("agents")
