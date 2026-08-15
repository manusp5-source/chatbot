import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

if TYPE_CHECKING:
    from app.models.user import User


class PushSubscription(Base):
    """Suscripción Web Push de un dispositivo del operador (PWA en el móvil).

    Cada navegador/PWA que activa las notificaciones registra aquí su endpoint
    del servicio de push (Apple/FCM/Mozilla) + las claves de cifrado del cliente
    (`p256dh`, `auth`). El backend firma con VAPID y envía a ese endpoint.

    `endpoint` es único: si el mismo dispositivo se re-suscribe (o cambia de
    operador), se reutiliza la fila (upsert por endpoint). Al borrar el usuario,
    sus suscripciones se borran (CASCADE).
    """

    __tablename__ = "push_subscription"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_subscription_endpoint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Los endpoints de push pueden ser largos (FCM ~200-400 chars).
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship()
