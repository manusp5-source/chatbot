"""create push_subscription (Web Push a la PWA del operador)

Revision ID: 0029_push_subscription
Revises: 0028_contact_social_handle
Create Date: 2026-06-16

El operador activa las notificaciones en la PWA del móvil; cada dispositivo
registra su endpoint de Web Push + claves de cifrado del cliente. El backend
firma con VAPID y envía cuando una conversación se deriva a humano o el cliente
queda esperando respuesta. `endpoint` es único (upsert por dispositivo); al
borrar el usuario se borran sus suscripciones (CASCADE).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0029_push_subscription"
down_revision = "0028_contact_social_handle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "push_subscription" not in set(inspect(bind).get_table_names()):
        op.create_table(
            "push_subscription",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("endpoint", sa.String(500), nullable=False),
            sa.Column("p256dh", sa.String(255), nullable=False),
            sa.Column("auth", sa.String(255), nullable=False),
            sa.Column("user_agent", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("endpoint", name="uq_push_subscription_endpoint"),
        )
        op.create_index("ix_push_subscription_user_id", "push_subscription", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_push_subscription_user_id", "push_subscription")
    op.drop_table("push_subscription")
