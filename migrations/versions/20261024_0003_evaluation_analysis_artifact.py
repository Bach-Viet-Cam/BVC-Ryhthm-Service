"""Store the per-evaluation analysis artifact (teacher/student f0 and alignment).

Revision ID: 20261024_0003
Revises: 20261023_0002
"""
from alembic import op
import sqlalchemy as sa

revision = "20261024_0003"
down_revision = "20261023_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rhythm_evaluations", sa.Column("analysis_artifact_key", sa.Text(), nullable=True))
    op.add_column("rhythm_evaluations", sa.Column("analysis_artifact_sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("rhythm_evaluations", "analysis_artifact_sha256")
    op.drop_column("rhythm_evaluations", "analysis_artifact_key")
