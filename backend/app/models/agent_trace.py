"""Evento de traza del agente para debugging por conversación.

Cada evento captura algo que pasó dentro del agente: una llamada al LLM,
una invocación a una tool, un lookup a la KB, una decisión del router,
o un error. La idea es que cuando algo sale raro el administrador pueda abrir el
panel de trace y ver paso a paso qué hizo el agente.

Es complementario a `llm_usage_log` (que agrega tokens/coste para el
budget tracker): aquí guardamos el detalle inspeccionable. Es
complementario también a `runtime_logs` en Redis (que muestra eventos
cross-conversation en /admin/system/logs): aquí persisten por conv.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TraceEventType(str, enum.Enum):
    llm_call = "llm_call"
    tool_invocation = "tool_invocation"
    kb_lookup = "kb_lookup"
    router_decision = "router_decision"
    error = "error"


class TraceLevel(str, enum.Enum):
    info = "info"
    warn = "warn"
    error = "error"


trace_event_type_enum = ENUM(
    TraceEventType,
    name="trace_event_type",
    create_type=False,
    values_callable=lambda x: [e.value for e in x],
)

trace_level_enum = ENUM(
    TraceLevel,
    name="trace_level",
    create_type=False,
    values_callable=lambda x: [e.value for e in x],
)


class AgentTraceEvent(Base):
    __tablename__ = "agent_trace_event"
    __table_args__ = (
        Index("ix_agent_trace_conv_created", "conversation_id", "created_at"),
        Index("ix_agent_trace_type", "event_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # message_id es opcional: muchos eventos (tool, kb_lookup) no se atan a un
    # mensaje concreto sino al ciclo "responder al último mensaje del cliente".
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[TraceEventType] = mapped_column(trace_event_type_enum, nullable=False)
    level: Mapped[TraceLevel] = mapped_column(
        trace_level_enum, nullable=False, default=TraceLevel.info
    )
    # payload con datos específicos por tipo de evento — ver `services/trace_logger.py`.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # mensaje legible corto (para el listado sin tener que expandir payload).
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
