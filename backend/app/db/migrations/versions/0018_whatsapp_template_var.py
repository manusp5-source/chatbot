"""create whatsapp_template_var (metadatos amigables de variables de plantilla)

Revision ID: 0018_whatsapp_template_var
Revises: 0017_contact_activity
Create Date: 2026-06-03

Capa PURAMENTE ADITIVA encima del envío de plantillas de WhatsApp. Las
plantillas viven en YCloud/Meta (no hay tabla local) y su cuerpo usa
placeholders numéricos `{{1}}`, `{{2}}`… El envío real (ycloud.send_template)
mapea un array ORDENADO de valores a esos placeholders y NO se toca.

Esta tabla guarda, por cada (template_name, language, idx):
  - `nombre`: etiqueta amigable que ve el operador (p.ej. "Nombre del contacto").
  - `contact_field`: enlace opcional a un campo del contacto para autorrellenar.
    `null` = relleno manual.

`idx` es 1-based y mapea a `{{idx}}`. UNIQUE (template_name, language, idx) =
una sola fila por posición de variable en una plantilla/idioma concretos.
Índice por `template_name` para filtrar rápido la config de una plantilla.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0018_whatsapp_template_var"
down_revision = "0017_contact_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotente por si una corrida previa creó la tabla a medias.
    bind = op.get_bind()
    if "whatsapp_template_var" not in inspect(bind).get_table_names():
        op.create_table(
            "whatsapp_template_var",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("template_name", sa.String(length=255), nullable=False),
            sa.Column("language", sa.String(length=20), nullable=False),
            # 1-based: mapea a `{{idx}}` del cuerpo de la plantilla.
            sa.Column("idx", sa.Integer(), nullable=False),
            sa.Column("nombre", sa.String(length=60), nullable=False, server_default=""),
            # Enlace opcional a un campo del contacto. null = relleno manual.
            sa.Column("contact_field", sa.String(length=40), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "template_name",
                "language",
                "idx",
                name="uq_whatsapp_template_var_name_lang_idx",
            ),
        )
        op.create_index(
            "ix_whatsapp_template_var_template_name",
            "whatsapp_template_var",
            ["template_name"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_template_var_template_name", table_name="whatsapp_template_var"
    )
    op.drop_table("whatsapp_template_var")
