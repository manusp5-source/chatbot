"""agents.kind (texto vs voz) + corrección de precios de modelos

Revision ID: 0049_agent_kind_prices
Revises: 0048_agent_correction_attempts
Create Date: 2026-08-11

Dos cosas que iban juntas en la misma auditoría:

1. `agents.kind` — hasta ahora nada distinguía un agente de LLAMADAS de uno de
   TEXTO. La resolución por canal cogía "cualquier agente activo, el más
   antiguo", y en una instalación limpia el único agente que existía era el de
   voz: WhatsApp lo atendía el agente de voz (sin derivar a humano, respuesta en
   un bloque y prompt de "atiendes llamadas telefónicas"). Con esta columna el
   runtime puede exigir que el agente encaje con el canal.

   Backfill conservador para no romper instalaciones ya montadas:
     - agente enlazado a un canal `retell_voice` → 'voice'
     - agente llamado "Agente de Voz" (el que sembraba el seed) → 'voice'
     - el resto → 'text'

2. Precios. La semilla de 0042 tenía los tres modelos GPT-5.4 mal (el modelo por
   defecto, gpt-5.4-mini, infravalorado 5x en entrada y 7,5x en salida: un tope
   de 50 $ dejaba gastar ~300 $). Se corrigen SOLO las filas que siguen en
   `source='seed'`, para respetar ediciones manuales y el refresco de OpenRouter.
   Verificado en developers.openai.com/api/docs/pricing (11-ago-2026).

   Se añaden además los modelos de TRANSCRIPCIÓN, que no estaban en la tabla:
   su consumo no contaba nada contra el tope de gasto.

3. `document_status.indexado_sin_semantica` — sin clave de embeddings, la KB se
   indexaba con vectores de ceros y el documento quedaba "en verde". Ahora ese
   caso tiene su propio estado y se ve en el panel.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0049_agent_kind_prices"
down_revision = "0048_agent_correction_attempts"
branch_labels = None
depends_on = None


# (modelo, input_per_1m, output_per_1m). Precios públicos de OpenAI a 11-ago-2026.
_PRICE_FIX = [
    ("gpt-5.4", 2.50, 15.00),
    ("gpt-5.4-mini", 0.75, 4.50),
    ("gpt-5.4-nano", 0.20, 1.25),
]

# Transcripción: se factura por tokens de audio (el "$/minuto" que publica
# OpenAI es una estimación derivada). Sin estas filas, cada nota de voz salía
# gratis en el contador del presupuesto.
_PRICE_NEW = [
    ("gpt-4o-mini-transcribe", 1.25, 5.00),
    ("gpt-4o-transcribe", 2.50, 10.00),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # ---- 1) agents.kind -----------------------------------------------------
    if "agents" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("agents")}
        if "kind" not in cols:
            op.add_column(
                "agents",
                sa.Column(
                    "kind",
                    sa.String(10),
                    nullable=False,
                    server_default="text",
                ),
            )
            # Backfill: los que ya sirven a un canal de voz, y el sembrado.
            op.execute(
                sa.text(
                    """
                    UPDATE agents SET kind = 'voice'
                    WHERE id IN (
                        SELECT agent_id FROM channels
                        WHERE type = 'retell_voice' AND agent_id IS NOT NULL
                    )
                    """
                )
            )
            op.execute(
                sa.text("UPDATE agents SET kind = 'voice' WHERE name = 'Agente de Voz'")
            )
            op.create_index("ix_agents_kind", "agents", ["kind"])

    # ---- 2) nuevo estado de documento --------------------------------------
    # ALTER TYPE ... ADD VALUE se puede ejecutar dentro de transacción desde
    # PG12 siempre que el valor no se USE en la misma transacción (aquí solo se
    # añade). IF NOT EXISTS lo hace idempotente.
    op.execute(
        sa.text(
            "ALTER TYPE document_status ADD VALUE IF NOT EXISTS 'indexado_sin_semantica'"
        )
    )

    # ---- 3) precios ---------------------------------------------------------
    if "llm_model_price" in set(inspector.get_table_names()):
        for model, inp, out in _PRICE_FIX:
            # Solo las filas que nadie ha tocado ni ha refrescado OpenRouter.
            op.execute(
                sa.text(
                    "UPDATE llm_model_price SET input_per_1m = :i, output_per_1m = :o "
                    "WHERE model = :m AND source = 'seed'"
                ).bindparams(m=model, i=inp, o=out)
            )
        for model, inp, out in _PRICE_FIX + _PRICE_NEW:
            op.execute(
                sa.text(
                    "INSERT INTO llm_model_price (model, input_per_1m, output_per_1m, source) "
                    "VALUES (:m, :i, :o, 'seed') ON CONFLICT (model) DO NOTHING"
                ).bindparams(m=model, i=inp, o=out)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "agents" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("agents")}
        if "kind" in cols:
            op.drop_index("ix_agents_kind", table_name="agents")
            op.drop_column("agents", "kind")
    # Los precios NO se revierten: volver a un precio incorrecto no es un
    # rollback útil.
