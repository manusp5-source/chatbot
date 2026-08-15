"""Difusiones: baja permanente (opt-out) + cabecera y variables con nombre.

Dos cosas:

1. Tabla `outbound_optout`. La baja de una difusión vivía SOLO en la blocklist
   de Redis, que caduca (24 h / 30 días) y se va con un reinicio sin
   persistencia: quien pedía la baja volvía a entrar en la campaña siguiente.
   Una baja se guarda en Postgres y no caduca.

2. Columnas nuevas en `outbound_job`: `header` (cabecera de la plantilla, con
   el fichero ya subido al proveedor), `param_format` ("positional" o "named")
   y `variable_names`. Sin esto, las plantillas con cabecera o con parámetros
   con nombre (`{{nombre}}`, lo que ofrece hoy el asistente de Meta) fallaban
   en todos los destinatarios.

Idempotente: se puede aplicar dos veces sin romper.

Revision ID: 0050_outbound_optout
Revises: 0049_agent_kind_prices
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050_outbound_optout"
down_revision = "0049_agent_kind_prices"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table):
        return False
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_table("outbound_optout"):
        op.create_table(
            "outbound_optout",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("phone", sa.String(320), nullable=False),
            sa.Column(
                "source", sa.String(20), nullable=False, server_default="manual"
            ),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column(
                "created_by",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        # UNIQUE sobre el identificador ya normalizado: las tres formas de
        # escribir el mismo número son una sola baja.
        op.create_index(
            "ix_outbound_optout_phone", "outbound_optout", ["phone"], unique=True
        )

    if not _has_column("outbound_job", "header"):
        op.add_column(
            "outbound_job",
            sa.Column("header", postgresql.JSONB(), nullable=True),
        )
    if not _has_column("outbound_job", "param_format"):
        op.add_column(
            "outbound_job",
            sa.Column(
                "param_format",
                sa.String(16),
                nullable=False,
                server_default="positional",
            ),
        )
    if not _has_column("outbound_job", "variable_names"):
        op.add_column(
            "outbound_job",
            sa.Column("variable_names", postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    for col in ("variable_names", "param_format", "header"):
        if _has_column("outbound_job", col):
            op.drop_column("outbound_job", col)
    if _has_table("outbound_optout"):
        op.drop_index("ix_outbound_optout_phone", table_name="outbound_optout")
        op.drop_table("outbound_optout")
