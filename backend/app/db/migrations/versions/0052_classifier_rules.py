"""Reglas duras del clasificador (sin LLM) + acción en Gmail.

Tabla `classifier_rules`: lista de "esto no lo quiero ver" por remitente,
dominio o texto del asunto. Se evalúa antes del modelo, así que lo que caza no
gasta tokens ni genera borrador.

No toca datos existentes: la tabla nace vacía y, sin reglas, el comportamiento
es exactamente el de antes. La acción sobre Gmail (archivar + etiquetar) vive
en la tabla KV `app_settings` (clave `classifier.gmail_action`), que no
necesita migración; su valor por defecto — solo las reglas duras tocan el
buzón — está en el código.

Revision ID: 0052_classifier_rules
Revises: 0051_voice_cost_and_privacy
"""
import sqlalchemy as sa
from alembic import op

revision = "0052_classifier_rules"
down_revision = "0051_voice_cost_and_privacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "classifier_rules",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("canal", sa.String(30), nullable=True),
        sa.Column("campo", sa.String(20), nullable=False),
        sa.Column("valor", sa.String(255), nullable=False),
        sa.Column("nota", sa.String(255), nullable=True),
        sa.Column("hits", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "campo IN ('remitente', 'dominio', 'asunto')",
            name="ck_classifier_rules_campo",
        ),
    )
    # La misma regla dos veces no filtra más y descuadra los contadores. Va
    # sobre `coalesce(canal, '')` a propósito: en Postgres dos NULL no chocan,
    # así que un índice normal dejaría meter la misma regla "para todos los
    # canales" tantas veces como se le diera al botón.
    op.execute(
        "CREATE UNIQUE INDEX ux_classifier_rules_unica ON classifier_rules "
        "(campo, valor, coalesce(canal, ''))"
    )
    # El runtime lee solo las activas en cada mensaje entrante.
    op.create_index("ix_classifier_rules_enabled", "classifier_rules", ["enabled"])


def downgrade() -> None:
    op.drop_index("ix_classifier_rules_enabled", table_name="classifier_rules")
    op.drop_index("ux_classifier_rules_unica", table_name="classifier_rules")
    op.drop_table("classifier_rules")
