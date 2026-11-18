"""Create the encrypted LLM API key pool table.

Revision ID: 20261118_0004
Revises: 20261024_0003
"""
from alembic import op
import sqlalchemy as sa

revision = "20261118_0004"
down_revision = "20261024_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="gemini"),
        sa.Column("key_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_hmac", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("key_hmac"),
    )
    op.create_index("ix_llm_api_keys_status", "llm_api_keys", ["status"])


def downgrade() -> None:
    op.drop_index("ix_llm_api_keys_status", table_name="llm_api_keys")
    op.drop_table("llm_api_keys")
