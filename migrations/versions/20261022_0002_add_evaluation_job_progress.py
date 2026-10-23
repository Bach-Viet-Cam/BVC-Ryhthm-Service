"""Track the running job, attempt and callback of an evaluation.

Revision ID: 20261022_0002
Revises: 20260926_0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261022_0002"
down_revision: Union[str, Sequence[str], None] = "20260926_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rhythm_evaluations", sa.Column("stage", sa.String(length=32), nullable=True))
    op.add_column("rhythm_evaluations", sa.Column("attempt", sa.Integer(), server_default="1", nullable=False))
    op.add_column("rhythm_evaluations", sa.Column("callback_url", sa.Text(), nullable=True))
    op.add_column("rhythm_evaluations", sa.Column("job_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("rhythm_evaluations", "job_state")
    op.drop_column("rhythm_evaluations", "callback_url")
    op.drop_column("rhythm_evaluations", "attempt")
    op.drop_column("rhythm_evaluations", "stage")
