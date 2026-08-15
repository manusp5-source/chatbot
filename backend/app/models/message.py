import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted_type import EncryptedText
from app.db.session import Base


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    operator = "operator"
    system = "system"


message_role_enum = ENUM(
    MessageRole, name="message_role", create_type=False,
    values_callable=lambda x: [e.value for e in x],
)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rol: Mapped[MessageRole] = mapped_column(message_role_enum, nullable=False)
    # Texto del mensaje EN CLARO. No se cifra a propósito: hay SQL crudo que lo
    # lee (`api/admin.py`, panel de Atención), predicados SQL sobre su valor
    # (`services/gmail_retention.py` compara contra '') y tareas que filtran por
    # él, y ninguno de esos sobrevive a una columna BYTEA. Lo que SÍ es PII de
    # máxima sensibilidad —la voz del cliente— no se guarda aquí: ver
    # `audio_transcript`.
    contenido: Mapped[str | None] = mapped_column(Text)
    audio_url: Mapped[str | None] = mapped_column(String(500))
    # PII alta sensibilidad (voz del cliente): cifrado en reposo.
    #
    # Aquí va TODO lo que el cliente dijo con su voz, venga de donde venga:
    #   - nota de voz de WhatsApp/Instagram → lo escribe `audio_processor`,
    #   - turno de una LLAMADA de teléfono (canal retell_voice) → lo escribe
    #     `api/voice.py`.
    # En ambos casos `contenido` queda a NULL: una transcripción de voz no se
    # guarda nunca en claro. Antes la llamada entera iba a `contenido` mientras
    # una nota de voz de cinco segundos iba cifrada, que es justo al revés de lo
    # que dice esta línea.
    #
    # Todos los sitios que muestran "el texto del mensaje" ya leen
    # `contenido or audio_transcript` (bandeja, resumen rodante, ficha de
    # contacto, avisos), así que el panel enseña la llamada igual que antes.
    audio_transcript: Mapped[str | None] = mapped_column(EncryptedText)
    extra: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    leido_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Multimedia (Fase 0.5 — outgoing del operador y entrantes del cliente).
    # media_type es uno de: image | audio | video | document | sticker.
    # media_url es la ruta relativa al volumen local servida por el endpoint
    # protegido `/uploads/{filename}`. Se conserva aunque el media externo de
    # WhatsApp expire, para que el histórico siga mostrando lo que se mandó.
    # media_purged_at se setea cuando la política de retención borra el
    # archivo físico — la fila se queda con el resto de metadata para que en
    # el histórico aparezca "se envió un audio el día X".
    media_type: Mapped[str | None] = mapped_column(String(20))
    media_url: Mapped[str | None] = mapped_column(String(500))
    media_mime: Mapped[str | None] = mapped_column(String(120))
    media_size: Mapped[int | None] = mapped_column(Integer)
    media_filename: Mapped[str | None] = mapped_column(String(255))
    media_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    media_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
