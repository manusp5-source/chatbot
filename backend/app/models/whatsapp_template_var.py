import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class WhatsappTemplateVar(Base):
    """Metadatos AMIGABLES de una variable de plantilla de WhatsApp.

    Capa puramente aditiva ENCIMA del envío. Las plantillas viven en YCloud/Meta
    y su cuerpo usa placeholders numéricos `{{1}}`, `{{2}}`… El envío real mapea
    un array ORDENADO de valores a esos placeholders (contrato sagrado en
    `providers/whatsapp/ycloud.py::send_template`) y NO se toca.

    Aquí guardamos, por cada (template_name, language, idx):
      - una etiqueta amigable (`nombre`, p.ej. "Nombre del contacto") para que el
        operador no vea `{{1}}` sino algo legible.
      - un enlace opcional a un campo del contacto (`contact_field`) para
        autorrellenar el valor desde la ficha del contacto. `null` = relleno
        manual.

    `idx` es 1-based y mapea directamente a `{{idx}}` del cuerpo de la plantilla.
    El UNIQUE (template_name, language, idx) garantiza una sola fila por
    posición de variable en una plantilla concreta y su idioma.
    """

    __tablename__ = "whatsapp_template_var"
    __table_args__ = (
        UniqueConstraint(
            "template_name", "language", "idx", name="uq_whatsapp_template_var_name_lang_idx"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    # 1-based: mapea a `{{idx}}` del cuerpo de la plantilla.
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    # Etiqueta amigable que ve el operador (p.ej. "Nombre del contacto").
    nombre: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    # Enlace opcional a un campo del contacto para autorrelleno. null = manual.
    contact_field: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
