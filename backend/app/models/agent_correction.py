import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted_type import EncryptedText
from app.db.session import Base

# Marcador de `instruction` cuando la corrección viene de EDITAR el borrador a
# mano (no de una instrucción en lenguaje natural). Se registra igual (para que
# la operadora vea su feedback), pero el detector de reglas la EXCLUYE del
# clustering: de un texto editado a mano no se puede destilar una pauta de estilo.
MANUAL_EDIT_INSTRUCTION = "[edición manual]"


class AgentCorrection(Base):
    """Autoaprendizaje · Fase 1.

    Cada vez que la operadora corrige al agente con una instrucción en lenguaje
    natural ("sé más cálida, no ofrezcas descuentos") y el agente RE-REDACTA la
    respuesta, guardamos aquí: qué había propuesto, qué se le indicó y cómo
    quedó. De momento no cambia nada solo — es la materia prima de la Fase 2
    (destilar reglas de estilo / entradas de la base de conocimiento que la
    operadora aprobará).

    Los textos van cifrados en reposo (pueden contener datos del cliente), igual
    que el resto de PII del sistema (EncryptedText → BYTEA).
    """

    __tablename__ = "agent_corrections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # El borrador/sugerencia corregido. No es FK dura: el Message puede borrarse
    # al descartar la conversación; conservamos la traza del aprendizaje.
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    canal: Mapped[str | None] = mapped_column(String(40))
    original_text: Mapped[str | None] = mapped_column(EncryptedText)
    instruction: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    resulting_text: Mapped[str | None] = mapped_column(EncryptedText)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Autoaprendizaje · Fase 2 — marca de "ya analizada" por el detector de
    # correcciones repetidas (`detect_correction_gaps`). El detector solo calcula
    # embeddings y agrupa las correcciones SIN procesar, de modo que en cada
    # corrida periódica el coste es proporcional a lo NUEVO, no al histórico.
    # NULL = pendiente de analizar.
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Veces que el detector INTENTÓ analizarla y no pudo (el LLM falló). Una
    # corrección sin analizar se queda pendiente para la corrida siguiente en
    # vez de darse por procesada — pero no eternamente: al llegar a
    # MAX_ANALYSIS_ATTEMPTS se cierra con un log de error.
    analysis_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Si la operadora convirtió esta corrección en aprendizaje a mano ("Convertir
    # en aprendizaje ahora"): "style" (→ regla en el prompt) o "content" (→ base
    # de conocimiento). NULL = no promocionada. Sirve para el badge de "a dónde
    # fue" y para no volver a sugerirla.
    promoted_to: Mapped[str | None] = mapped_column(String(20), nullable=True)
