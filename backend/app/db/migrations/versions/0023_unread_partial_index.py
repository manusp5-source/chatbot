"""partial index for unread client messages

Revision ID: 0023_unread_partial_index
Revises: 0022_provider_message_id_index
Create Date: 2026-06-10

El inbox calcula "no leídos" constantemente: el contador agrupado por página de
GET /conversations, el filtro unread_only (EXISTS) y el UPDATE de mark-read
filtran todos por `rol = 'user' AND leido_at IS NULL`. Esas filas son una
fracción mínima de `messages` (lo no leído tiende a cero), así que un índice
PARCIAL sobre conversation_id con ese predicado las localiza en O(log n) y se
mantiene diminuto (solo indexa lo pendiente).

El enum `message_role` se creó en 20260515_0001 con valores en minúsculas
('user', 'assistant', 'operator', 'system') → el predicado usa 'user'.
"""
from alembic import op

revision = "0023_unread_partial_index"
down_revision = "0022_provider_message_id_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_messages_unread ON messages (conversation_id) "
        "WHERE rol = 'user' AND leido_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_messages_unread")
