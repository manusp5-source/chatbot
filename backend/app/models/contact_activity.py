import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ContactActivity(Base):
    """Evento de auditoría de la ficha de contacto (timeline de actividad).

    Registra acciones del equipo/sistema sobre un contacto DE AQUÍ EN ADELANTE
    (decisión del dueño: el histórico previo no se reconstruye, la tabla arranca
    vacía). Cada fila es un hecho inmutable: quién (actor), qué (tipo), cuándo
    (created_at) y un pequeño payload de contexto (meta).

    - `contact_id` con ON DELETE CASCADE: al borrar el contacto se borran sus
      eventos (no tiene sentido conservar la auditoría de algo inexistente).
    - `actor_user_id` nullable + ON DELETE SET NULL: si el usuario que ejecutó
      la acción se borra, el evento se conserva (queda sin actor resoluble).
      NULL también significa "sistema/agente" (p. ej. conversación iniciada por
      el cliente al escribir, sin operador humano detrás).
    - `meta` (JSONB) SIN PII: solo estados, nombre de etiqueta, canal e ids.
      NUNCA teléfono, email, nombre del contacto ni texto de nota.
    """

    __tablename__ = "contact_activity"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Tipo de evento: contact_created / status_changed / tag_added / tag_removed
    # / note_added / conversation_started.
    tipo: Mapped[str] = mapped_column(String(40), nullable=False)
    # Payload pequeño de contexto, SIN PII. Nullable: algunos eventos no llevan.
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Indexado para ordenar la línea de tiempo (más recientes primero).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    contact: Mapped["Contact"] = relationship()  # noqa: F821
    author: Mapped["User | None"] = relationship()  # noqa: F821
