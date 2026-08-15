import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AgentConfig(Base):
    __tablename__ = "agent_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_system: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(80), nullable=False, default="gpt-5.4-mini")
    temperature: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=0.3)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    buffer_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    response_split_max_parts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    context_window: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    # Mensaje que el bot manda al cliente justo antes de pasar la conversacion
    # al equipo humano. Editable desde /admin/agent/config. Si esta vacio se
    # usa el default hardcoded en la tool `derivar_humano`.
    handoff_bridge_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Tope de coste mensual en USD. Si los costes acumulados del mes superan
    # este valor, el agente se pausa automaticamente y se notifica al admin.
    # NULL = sin tope.
    monthly_budget_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
