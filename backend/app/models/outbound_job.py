import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

if TYPE_CHECKING:
    from app.models.user import User


# Estados de un envío masivo. Plano (String) en vez de ENUM de Postgres: son
# estados internos de orquestación que pueden evolucionar y no merece la pena
# el coste de gestionar un tipo ENUM en migraciones.
JOB_STATUS_QUEUED = "queued"        # creado, a la espera de que el worker empiece
JOB_STATUS_RUNNING = "running"      # enviando, re-encolándose entre destinatarios
JOB_STATUS_CANCELED = "canceled"    # cancelado por el operador (deja de enviar)
JOB_STATUS_DONE = "done"            # todos los destinatarios procesados
JOB_STATUS_FAILED = "failed"        # error irrecuperable a nivel de job

# Estados por destinatario.
RECIPIENT_STATUS_PENDING = "pending"
RECIPIENT_STATUS_OK = "ok"
RECIPIENT_STATUS_ERROR = "error"


class OutboundJob(Base):
    """Un envío masivo de una plantilla de WhatsApp, ejecutado en segundo plano.

    El envío NO ocurre en la petición HTTP: una tarea Celery
    (`app.tasks.outbound_send`) procesa los destinatarios UNO A UNO,
    re-encolándose con un `countdown` aleatorio entre envíos para ir "poco a
    poco" sin disparar baneos de WhatsApp y sin bloquear a los workers que
    también atienden el chatbot.

    El progreso (`sent_ok` / `sent_error`) se actualiza incrementalmente para que
    la UI pueda hacer polling de `GET /admin/outbound/jobs/{id}`. El operador
    puede cancelar: la tarea relee `status` en cada vuelta y deja de re-encolar.
    """

    __tablename__ = "outbound_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_name: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=JOB_STATUS_QUEUED, index=True
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_error: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Ventana de retardo (segundos) entre envíos: la tarea espera un valor
    # aleatorio en [min, max] para añadir jitter. "Lento y seguro" = 20-40.
    throttle_min_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    throttle_max_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    # Cabecera de la plantilla, si la tiene. Media ya subido al proveedor
    # (`{"format": "image", "media_id": "...", "filename": "..."}`) o cabecera
    # de texto con sus variables (`{"format": "text", "variables": [...]}`).
    # Sin esto, una plantilla con cabecera fallaba en TODOS los destinatarios.
    header: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # "positional" (`{{1}}`) o "named" (`{{nombre}}`, lo que ofrece hoy el
    # asistente de Meta). Con nombres, cada parámetro viaja con su
    # `parameter_name` o Meta rechaza el envío.
    param_format: Mapped[str] = mapped_column(
        String(16), nullable=False, default="positional", server_default="positional"
    )
    # Nombres de las variables del cuerpo, en orden, para `param_format=named`.
    variable_names: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipients: Mapped[list["OutboundJobRecipient"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OutboundJobRecipient.position",
    )
    creator: Mapped["User | None"] = relationship()


class OutboundJobRecipient(Base):
    """Un destinatario concreto dentro de un envío masivo.

    Filas por destinatario (no un blob JSON) para actualizar el progreso de
    forma incremental e inspeccionar los fallos. `variables` es el array
    ORDENADO de valores ya resueltos/completados en el frontend, con el mismo
    contrato de siempre: `variables[idx-1] -> {{idx}}` de la plantilla.
    """

    __tablename__ = "outbound_job_recipient"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("outbound_job.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Orden estable de procesamiento dentro del job.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    phone: Mapped[str] = mapped_column(String(320), nullable=False)
    variables: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=RECIPIENT_STATUS_PENDING, index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped["OutboundJob"] = relationship(back_populates="recipients")
