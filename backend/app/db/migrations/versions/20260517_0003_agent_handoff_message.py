"""add handoff_bridge_message to agent_config

Revision ID: 20260517_0003
Revises: 20260516_0002
Create Date: 2026-05-17

"""
from alembic import op
import sqlalchemy as sa

revision = "0003_agent_handoff_message"
down_revision = "0002_encrypt_pii"
branch_labels = None
depends_on = None

DEFAULT_BRIDGE = "Te paso con el equipo. Te contestaran por aqui en cuanto puedan."


def upgrade() -> None:
    op.add_column(
        "agent_config",
        sa.Column("handoff_bridge_message", sa.Text(), nullable=True),
    )
    # Pre-rellenamos las filas existentes con el default para que la UI no
    # muestre el campo vacio al admin nada mas migrar.
    op.execute(
        sa.text("UPDATE agent_config SET handoff_bridge_message = :msg WHERE handoff_bridge_message IS NULL")
        .bindparams(msg=DEFAULT_BRIDGE)
    )


def downgrade() -> None:
    op.drop_column("agent_config", "handoff_bridge_message")
