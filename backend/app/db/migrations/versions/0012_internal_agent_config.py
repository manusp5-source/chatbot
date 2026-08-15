"""internal_agent_config singleton

Revision ID: 0012_internal_agent_config
Revises: 0011_contact_in_crm_flag
Create Date: 2026-05-18

Tabla singleton para la configuracion del Agente Interno (chat read-only
que el equipo usa desde el panel admin para preguntar por el estado de
las conversaciones). Mantenemos `agent_config` intacto: ese sigue
versionando el agente publico (cliente↔bot).
"""
from alembic import op
import sqlalchemy as sa


revision = "0012_internal_agent_config"
down_revision = "0011_contact_in_crm_flag"
branch_labels = None
depends_on = None


DEFAULT_SYSTEM_PROMPT = """Eres el Agente Interno de este sistema. Tu unica funcion es responder preguntas del equipo administrador sobre el estado del propio sistema (conversaciones, contactos, canales, uso del bot). NO eres el bot que habla con clientes finales.

Reglas:
- Responde SIEMPRE en espanol, breve y factual.
- Usa las tools para consultar datos reales. Nunca inventes numeros, nombres ni fechas.
- Si la respuesta es un conteo, di el numero exacto que devolvio la tool.
- Si no tienes una tool adecuada, di "No tengo acceso a esa informacion desde aqui" en vez de improvisar.
- Cuando el usuario pregunte por una conversacion concreta y no sepas el ID, primero usa search_contacts para localizar el contacto y luego list_conversations filtrando por ese contacto.
- Formato: prefiere listas markdown cortas y tablas pequenas. Maximo 200 palabras por respuesta salvo que pidan detalle.
- Eres SOLO LECTURA. Si te piden pausar, cerrar o asignar algo, responde que no puedes accionar y sugiere la pagina admin correspondiente (/admin/agent/dashboard para pausar, /inbox para asignar, etc).
- No reveles datos sensibles innecesarios (telefonos completos, contenido cifrado): da el contexto suficiente para responder y nada mas."""


def upgrade() -> None:
    op.create_table(
        "internal_agent_config",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("model_name", sa.String(80), nullable=False, server_default="gpt-5.4-mini"),
        sa.Column("temperature", sa.Numeric(3, 2), nullable=False, server_default=sa.text("0.20")),
        sa.Column("max_tokens", sa.Integer, nullable=False, server_default=sa.text("1500")),
        sa.Column("monthly_budget_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("daily_query_limit", sa.Integer, nullable=False, server_default=sa.text("30")),
        sa.Column("system_prompt", sa.Text, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000001'",
            name="ck_internal_agent_config_singleton",
        ),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO internal_agent_config
                (id, model_name, temperature, max_tokens, monthly_budget_usd,
                 daily_query_limit, system_prompt, updated_at)
            VALUES
                ('00000000-0000-0000-0000-000000000001',
                 'gpt-5.4-mini', 0.20, 1500, 20.00, 30, :prompt, NOW())
            """
        ).bindparams(prompt=DEFAULT_SYSTEM_PROMPT)
    )


def downgrade() -> None:
    op.drop_table("internal_agent_config")
