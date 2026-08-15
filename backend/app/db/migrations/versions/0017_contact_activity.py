"""create contact_activity (timeline de actividad / auditoría)

Revision ID: 0017_contact_activity
Revises: 0016_contact_note
Create Date: 2026-06-03

Tabla de eventos de auditoría de la ficha de contacto. Registra acciones del
equipo/sistema sobre un contacto DE AQUÍ EN ADELANTE.

Decisión del dueño — EMPEZAR DE CERO:
  - NO se reconstruye el histórico previo. La tabla arranca vacía y se va
    rellenando según ocurren las acciones (crear contacto, cambiar estado,
    etiquetar, añadir nota, iniciar conversación).

`meta` es JSONB y NUNCA contiene PII (solo estados, nombre de etiqueta, canal,
ids). Índices por `contact_id` (filtrar por ficha) y por `created_at` (ordenar
la línea de tiempo, más recientes primero).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0017_contact_activity"
down_revision = "0016_contact_note"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotente por si una corrida previa creó la tabla a medias.
    bind = op.get_bind()
    if "contact_activity" not in inspect(bind).get_table_names():
        op.create_table(
            "contact_activity",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "contact_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("contacts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "actor_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("tipo", sa.String(length=40), nullable=False),
            # Payload pequeño de contexto, SIN PII (estados / etiqueta / canal / ids).
            sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_contact_activity_contact_id", "contact_activity", ["contact_id"]
        )
        op.create_index(
            "ix_contact_activity_created_at", "contact_activity", ["created_at"]
        )


def downgrade() -> None:
    op.drop_index("ix_contact_activity_created_at", table_name="contact_activity")
    op.drop_index("ix_contact_activity_contact_id", table_name="contact_activity")
    op.drop_table("contact_activity")
