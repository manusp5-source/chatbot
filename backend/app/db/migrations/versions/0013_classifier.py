"""classifier config + conversation quarantine

Revision ID: 0013_classifier
Revises: 0012_internal_agent_config
Create Date: 2026-06-02

Agente clasificador pre-bot (anti-spam) con modelo "cuarentena":
- Columnas en `conversations` para retener una conversación (no responde el
  bot) hasta revisión manual, y marcarla como ya revisada.
- Tabla singleton `classifier_config` (activable por canal). Arranca DESACTIVADO.
"""
from alembic import op
import sqlalchemy as sa


revision = "0013_classifier"
down_revision = "0012_internal_agent_config"
branch_labels = None
depends_on = None


DEFAULT_INSTRUCTIONS = """Eres un clasificador anti-spam para la bandeja de entrada del negocio. Decides si un mensaje entrante NO debe ser atendido por el bot de atencion al cliente.

Marca como SPAM (no deseado) solo cuando el mensaje sea claramente:
- Publicidad o venta no solicitada (agencias, marketing, SEO, cripto, "colaboraciones" o ventas por Instagram).
- Spam, estafas o phishing.
- Mensajes sin intencion real de cliente (bots, cadenas, contenido irrelevante).

NO marques como spam a un posible cliente real, aunque el mensaje sea breve, tenga faltas, o solo salude o pregunte por servicios/precios. Ante la duda, NO es spam."""


def upgrade() -> None:
    op.add_column("conversations", sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("quarantine_reason", sa.String(255), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("spam_reviewed", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_conversations_quarantined_at", "conversations", ["quarantined_at"])

    op.create_table(
        "classifier_config",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("channels", sa.JSON, nullable=False),
        sa.Column("instructions", sa.Text, nullable=False),
        sa.Column("model_name", sa.String(80), nullable=False, server_default="gpt-5.4-mini"),
        sa.Column("temperature", sa.Numeric(3, 2), nullable=False, server_default=sa.text("0.0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000001'",
            name="ck_classifier_config_singleton",
        ),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO classifier_config
                (id, enabled, channels, instructions, model_name, temperature, updated_at)
            VALUES
                ('00000000-0000-0000-0000-000000000001', false, '["instagram_dm"]'::json,
                 :instructions, 'gpt-5.4-mini', 0.0, NOW())
            """
        ).bindparams(instructions=DEFAULT_INSTRUCTIONS)
    )


def downgrade() -> None:
    op.drop_table("classifier_config")
    op.drop_index("ix_conversations_quarantined_at", table_name="conversations")
    op.drop_column("conversations", "spam_reviewed")
    op.drop_column("conversations", "quarantine_reason")
    op.drop_column("conversations", "quarantined_at")
