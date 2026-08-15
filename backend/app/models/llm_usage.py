import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class LLMUsage(Base):
    __tablename__ = "llm_usage_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 'agent' = chat completion del orchestrator, 'rag' = embeddings,
    # 'moderation' = la API gratuita de OpenAI Moderation (no cuenta tokens
    # realmente, lo registramos a 0 para contar llamadas).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Agente (tabla `agents`) que generó la llamada, para el desglose de consumo
    # "por agente". Solo se rellena en source='agent' (el agente conversacional
    # resuelto para el canal). null = transversal (clasificador, agente interno,
    # embeddings, moderación) o histórico previo a esta columna. Sin FK a propósito:
    # si un Agent se borra, conservamos el id para no perder el histórico de gasto.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
