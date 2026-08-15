import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class DocumentFormato(str, enum.Enum):
    pdf = "pdf"
    docx = "docx"
    txt = "txt"
    md = "md"
    csv = "csv"
    xlsx = "xlsx"


class DocumentStatus(str, enum.Enum):
    procesando = "procesando"
    indexado = "indexado"
    # Troceado y guardado, pero con vectores de CEROS porque no había clave de
    # embeddings. El agente lo encuentra por texto completo, no por significado.
    # Existe para que ese caso no se pinte en verde como si todo fuera bien: sin
    # este estado, quien añadía la clave más tarde se quedaba con sus PDF y Word
    # muertos sin que nada lo dijera.
    indexado_sin_semantica = "indexado_sin_semantica"
    error = "error"


doc_formato_enum = ENUM(
    DocumentFormato, name="document_formato", create_type=False,
    values_callable=lambda x: [e.value for e in x],
)
doc_status_enum = ENUM(
    DocumentStatus, name="document_status", create_type=False,
    values_callable=lambda x: [e.value for e in x],
)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    formato: Mapped[DocumentFormato] = mapped_column(doc_formato_enum, nullable=False)
    tamano_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        doc_status_enum, nullable=False, default=DocumentStatus.procesando
    )
    error_msg: Mapped[str | None] = mapped_column(Text)
    num_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_path: Mapped[str | None] = mapped_column(String(500))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class DocumentVersion(Base):
    """Versión anterior de un documento de texto editable (txt/md).

    Cada edición guarda el contenido PREVIO aquí antes de sobrescribir el
    archivo, para poder restaurar. Como los chunks (el mismo contenido troceado)
    se guardan en claro para la búsqueda híbrida, las versiones tampoco se
    cifran. Se conservan las últimas N por documento (el endpoint recorta).
    """

    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contenido: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
