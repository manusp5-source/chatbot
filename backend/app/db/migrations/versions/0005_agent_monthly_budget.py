"""add monthly_budget_usd to agent_config

Revision ID: 0005_agent_monthly_budget
Revises: 0004_llm_usage_log
Create Date: 2026-05-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_agent_monthly_budget"
down_revision = "0004_llm_usage_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_config",
        sa.Column("monthly_budget_usd", sa.Numeric(10, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_config", "monthly_budget_usd")
