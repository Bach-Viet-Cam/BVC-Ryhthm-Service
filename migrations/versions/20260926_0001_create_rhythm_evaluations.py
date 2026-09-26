"""Create the rhythm evaluations table.

Revision ID: 20260926_0001
Revises:
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rhythm_evaluations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("evaluation_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("template_url", sa.Text(), nullable=False),
        sa.Column("student_url", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_id"),
    )
    op.create_index("ix_rhythm_evaluations_evaluation_id", "rhythm_evaluations", ["evaluation_id"], unique=True)
    op.create_index("ix_rhythm_evaluations_status", "rhythm_evaluations", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_rhythm_evaluations_status", table_name="rhythm_evaluations")
    op.drop_index("ix_rhythm_evaluations_evaluation_id", table_name="rhythm_evaluations")
    op.drop_table("rhythm_evaluations")
