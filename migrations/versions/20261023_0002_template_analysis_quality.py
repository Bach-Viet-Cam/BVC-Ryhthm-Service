"""Add MAIN template artifacts and student audio quality reports.

Revision ID: 20261023_0002
Revises: 20260926_0001
"""
from alembic import op
import sqlalchemy as sa

revision = "20261023_0002"
down_revision = "20260926_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rhythm_evaluations", sa.Column("student_quality_report", sa.JSON(), nullable=True))
    op.create_table(
        "template_analyses",
        sa.Column("analysis_id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("sheet_music_id", sa.String(36), nullable=False),
        sa.Column("media_file_id", sa.String(36), nullable=False),
        sa.Column("media_url", sa.Text(), nullable=False),
        sa.Column("instrument_code", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analysis_version", sa.String(64), nullable=False),
        sa.Column("instrument_config", sa.JSON(), nullable=False),
        sa.Column("quality_policy", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=True),
        sa.Column("artifact_key", sa.Text(), nullable=True),
        sa.Column("artifact_sha256", sa.String(64), nullable=True),
        sa.Column("artifact_size_bytes", sa.Integer(), nullable=True),
        sa.Column("quality_report", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_template_analyses_status", "template_analyses", ["status"])
    op.create_index("ix_template_analyses_lookup", "template_analyses", [
        "sheet_music_id", "media_file_id", "instrument_code", "analysis_version", "created_at",
    ])


def downgrade() -> None:
    op.drop_index("ix_template_analyses_lookup", table_name="template_analyses")
    op.drop_index("ix_template_analyses_status", table_name="template_analyses")
    op.drop_table("template_analyses")
    op.drop_column("rhythm_evaluations", "student_quality_report")
