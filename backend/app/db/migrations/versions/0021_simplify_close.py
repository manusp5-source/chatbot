"""simplify inbox: retire 'cerrada' state -> bot + archived

Revision ID: 0021_simplify_close
Revises: 0020_voice_call_meta
Create Date: 2026-06-10

Simplificación de la bandeja (gestión diaria): se elimina el estado "Cerrar" de
la interfaz; la única acción de "hecho" es Archivar. Para que las conversaciones
ya cerradas no queden invisibles (Activas oculta las cerradas y ya no hay pestaña
"Cerradas"), las migramos a **bot + archivadas**:

  - status 'cerrada' -> 'bot': así los lookups existentes ("última no cerrada")
    las REABREN solas si el contacto vuelve a escribir, conservando el historial
    (memoria del agente intacta). Ya nada crea estado 'cerrada'.
  - archived_at = now() (si no lo tenían): siguen "fuera de la bandeja" pero
    recuperables desde la pestaña "Archivadas".

Es una migración de datos irreversible (no podemos saber cuáles estaban cerradas
originalmente), por eso downgrade es no-op.
"""
from alembic import op

revision = "0021_simplify_close"
down_revision = "0020_voice_call_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE conversations "
        "SET status = 'bot', "
        "    archived_at = COALESCE(archived_at, now()) "
        "WHERE status = 'cerrada'"
    )


def downgrade() -> None:
    # Irreversible: no se puede reconstruir qué conversaciones estaban cerradas.
    pass
