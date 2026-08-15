"""agent_corrections.promoted_to — corrección convertida en aprendizaje a mano

Revision ID: 0033_agent_correction_promoted
Revises: 0032_user_last_login
Create Date: 2026-06-23

Añade agent_corrections.promoted_to (varchar, nullable): "style" (→ regla en el
prompt) o "content" (→ base de conocimiento) cuando la operadora promociona una
corrección suelta desde "Aprendizajes" sin esperar a que se repita.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0033_agent_correction_promoted"
down_revision = "0032_user_last_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "agent_corrections" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("agent_corrections")}
        if "promoted_to" not in cols:
            op.add_column(
                "agent_corrections",
                sa.Column("promoted_to", sa.String(length=20), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "agent_corrections" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("agent_corrections")}
        if "promoted_to" in cols:
            op.drop_column("agent_corrections", "promoted_to")
