import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encrypted_type import EncryptedText
from app.db.session import Base


class ContactNote(Base):
    """Nota interna que un miembro del equipo escribe sobre un contacto.

    Sustituye al campo único `Contact.notas_internas` por una lista de notas
    con autoría. El texto va cifrado en reposo (mismo tipo que notas_internas).

    - `contact_id` con ON DELETE CASCADE: al borrar el contacto se borran sus
      notas (no tiene sentido conservarlas huérfanas).
    - `author_user_id` nullable + ON DELETE SET NULL: si el usuario que escribió
      la nota se borra, la nota se conserva (queda sin autor resoluble).
    """

    __tablename__ = "contact_note"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Texto de la nota: cifrado en reposo con Fernet (igual que notas_internas).
    texto: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contact: Mapped["Contact"] = relationship(back_populates="notes")  # noqa: F821
    author: Mapped["User | None"] = relationship()  # noqa: F821
