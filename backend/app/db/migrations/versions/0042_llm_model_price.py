"""Precios de modelos LLM editables/actualizables

Revision ID: 0042_llm_model_price
Revises: 0041_agent_tokens
Create Date: 2026-07-07

Mueve la tabla de precios (USD/1M tokens) de código a BD para poder
auto-actualizarla desde OpenRouter y editarla desde el panel. Siembra la tabla
con los precios que estaban hardcodeados en services/llm_pricing.py.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision = "0042_llm_model_price"
down_revision = "0041_agent_tokens"
branch_labels = None
depends_on = None


# Semilla = precios que vivían en services/llm_pricing.PRICE_PER_1M_USD.
_SEED = [
    ("gpt-5.4", 2.50, 10.00),
    ("gpt-5.4-mini", 0.15, 0.60),
    ("gpt-5.4-nano", 0.05, 0.20),
    ("text-embedding-3-small", 0.02, 0.0),
    ("text-embedding-3-large", 0.13, 0.0),
    ("omni-moderation-latest", 0.0, 0.0),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "llm_model_price" not in set(inspector.get_table_names()):
        op.create_table(
            "llm_model_price",
            sa.Column("model", sa.String(80), primary_key=True),
            sa.Column("input_per_1m", sa.Numeric(12, 6), nullable=False, server_default="0"),
            sa.Column("output_per_1m", sa.Numeric(12, 6), nullable=False, server_default="0"),
            sa.Column("source", sa.String(16), nullable=False, server_default="seed"),
            sa.Column("openrouter_id", sa.String(120), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("updated_by", UUID(as_uuid=True), nullable=True),
        )
        # Semilla (idempotente por si se re-ejecuta: ON CONFLICT DO NOTHING).
        for model, inp, out in _SEED:
            op.execute(
                sa.text(
                    "INSERT INTO llm_model_price (model, input_per_1m, output_per_1m, source) "
                    "VALUES (:m, :i, :o, 'seed') ON CONFLICT (model) DO NOTHING"
                ).bindparams(m=model, i=inp, o=out)
            )


def downgrade() -> None:
    op.drop_table("llm_model_price")
