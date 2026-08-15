"""Propuestas de cambio en la KB generadas por el agente interno.

El agente interno NUNCA escribe en la base de conocimiento: deja aquí una
propuesta (editar un documento de texto o crear uno nuevo) y el admin la
aplica o descarta con un clic desde el chat. Es la barrera contra la
inyección indirecta: aunque un mensaje de cliente manipulara al agente,
el cambio muere en una tarjeta que el admin ve y descarta.

El contenido va en claro, como los chunks/versiones: es material de la KB.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class KBProposalKind(str, enum.Enum):
    edit = "edit"      # reemplaza el contenido de un documento txt/md existente
    create = "create"  # crea un documento .txt nuevo (título + contenido)


class KBProposalStatus(str, enum.Enum):
    pendiente = "pendiente"
    aplicada = "aplicada"
    descartada = "descartada"


class KBEditProposal(Base):
    __tablename__ = "kb_edit_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # edit: documento objetivo. SET NULL si se borra (la propuesta queda inaplicable).
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    titulo: Mapped[str | None] = mapped_column(String(255))
    contenido: Mapped[str] = mapped_column(Text, nullable=False)
    motivo: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=KBProposalStatus.pendiente.value)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # create aplicado: el documento resultante (para enlazarlo desde la UI).
    applied_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
