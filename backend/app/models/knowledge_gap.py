import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted_type import EncryptedText
from app.db.session import Base


class KnowledgeGap(Base):
    """Autoaprendizaje · Fase 2 — huecos de conocimiento.

    Cuando el agente NO supo resolver una consulta (o la operadora lo corrige una
    y otra vez) dejamos aquí una traza para que la operadora la revise y, si
    procede, la convierta en conocimiento permanente. Disparadores:

      - "handoff": el agente derivó la conversación a un humano (`derivar_humano`).
      - "kb_miss": la búsqueda en la base de conocimiento (`consultar_kb`) no
        devolvió ningún resultado.
      - "correction": la operadora corrigió lo mismo varias veces (Fase 1,
        `agent_corrections` agrupadas por similitud) → se propone una regla de
        estilo o una Q&A. Ver `proposal_kind`.

    El flujo es siempre con aprobación humana (nunca se aprende solo): la
    operadora revisa la propuesta en la pantalla "Aprendizajes" y al aprobar, o
    bien se crea un `Document` de Q&A que se indexa en la KB (huecos de contenido)
    o bien se persiste una regla de estilo (`LearnedRule`) que se inyecta en el
    prompt del agente → el agente deja de repetir el fallo.

    Los textos (pregunta del cliente / respuesta) van cifrados en reposo
    (EncryptedText → BYTEA), igual que el resto de PII del sistema.
    """

    __tablename__ = "knowledge_gaps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # No es FK dura con CASCADE: si se borra la conversación queremos conservar
    # la traza del aprendizaje (SET NULL).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # "handoff" | "kb_miss" | "correction" | "faq"
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    # Qué propone este hueco y, por tanto, qué hace "Aprobar":
    #   - "content": crear una Q&A (Document) e indexarla en la KB. Es el valor
    #     por defecto e implícito de los huecos handoff/kb_miss.
    #   - "style": persistir una regla de estilo (LearnedRule) que se inyecta en
    #     el prompt del agente. Solo lo usan algunos huecos "correction".
    # Nullable por compatibilidad con los huecos ya existentes (se tratan como
    # "content").
    proposal_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # La pregunta del cliente (handoff/kb_miss) o un resumen de lo que se corrige
    # (correction). Texto que se muestra arriba en la tarjeta de "Aprendizajes".
    question: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    # La propuesta editable: respuesta de Q&A o regla de estilo. En huecos
    # "correction" el detector la rellena con un borrador del LLM; en
    # handoff/kb_miss la rellena la operadora al aprobar (null al crearse).
    suggested_answer: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    # "pendiente" | "aprobado" | "descartado"
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pendiente", index=True
    )
    # El documento de KB creado al aprobar el hueco.
    created_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
