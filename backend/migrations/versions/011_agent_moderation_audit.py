"""add agent moderation audit"""
import sqlalchemy as sa
from alembic import op
revision = "011_agent_moderation_audit"
down_revision = "010_forum"
branch_labels = None
depends_on = None
def upgrade():
    op.create_table("agent_moderation_audit", sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True), sa.Column("who", sa.Text(), nullable=False), sa.Column("action", sa.Text(), nullable=False), sa.Column("target", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
def downgrade(): op.drop_table("agent_moderation_audit")
