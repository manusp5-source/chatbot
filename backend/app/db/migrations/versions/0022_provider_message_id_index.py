"""index on messages.metadata->>'provider_message_id' for dedupe

Revision ID: 0022_provider_message_id_index
Revises: 0021_simplify_close
Create Date: 2026-06-10

El dedupe de mensajes entrantes (conversation.store_incoming y
store_outgoing_email) filtra por
`Message.extra["provider_message_id"].astext == <id>` en cada webhook/poll.
El atributo ORM `extra` mapea a la columna REAL `metadata` (JSONB) — ver
app/models/message.py: `mapped_column("metadata", JSONB, ...)`. Sin índice,
ese filtro hace seq-scan sobre toda la tabla messages en cada mensaje (I3).

Creamos un índice de expresión sobre `metadata->>'provider_message_id'` para
que el lookup de idempotencia sea O(log n). No es UNIQUE: hay muchas filas sin
ese campo (mensajes del operador, del bot, etc.) y NULL no debe colisionar.
"""
from alembic import op

revision = "0022_provider_message_id_index"
down_revision = "0021_simplify_close"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_messages_provider_message_id "
        "ON messages ((metadata->>'provider_message_id'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_messages_provider_message_id")
