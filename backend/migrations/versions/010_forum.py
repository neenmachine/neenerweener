"""continue forum schema after the pre-existing combined forum migration"""
from alembic import op

revision = "010_forum"
down_revision = "010_forum_puzzles"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
