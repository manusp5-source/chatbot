import enum
import uuid
from datetime import datetime

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encrypted_type import EncryptedText
from app.db.session import Base

if TYPE_CHECKING:
    from app.models.contact_note import ContactNote


class ContactEstado(str, enum.Enum):
    contacto = "contacto"
    solicitud_presupuesto = "solicitud_presupuesto"
    seguimiento = "seguimiento"
    cliente = "cliente"
    perdido = "perdido"
    no_cualifica = "no_cualifica"


class ContactOrigen(str, enum.Enum):
    whatsapp = "whatsapp"
    web = "web"
    manual = "manual"
    instagram = "instagram"
    email = "email"


contact_estado_enum = ENUM(
    ContactEstado, name="contact_estado", create_type=False,
    values_callable=lambda x: [e.value for e in x],
)
contact_origen_enum = ENUM(
    ContactOrigen, name="contact_origen", create_type=False,
    values_callable=lambda x: [e.value for e in x],
)


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telefono: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    nombre: Mapped[str | None] = mapped_column(String(120))
    # Instagram: el @usuario (handle), separado del nombre real para mostrar
    # "Nombre" + "@usuario" debajo en la bandeja. NULL en otros canales.
    social_handle: Mapped[str | None] = mapped_column(String(80))
    # WhatsApp — BSUID (Business-Scoped User ID). Desde abril de 2026, cuando el
    # cliente tiene nombre de usuario de WhatsApp, Meta puede NO mandar su
    # teléfono. Lo que manda siempre es este identificador, que además es
    # ESTABLE aunque el cliente se cambie de número.
    #
    # O sea que la llave de identidad de verdad es esta, y `telefono` pasa a ser
    # un dato más: puede llegar hoy y faltar mañana. Es único POR NEGOCIO —el
    # mismo cliente tiene un BSUID distinto en cada empresa—, así que no vale
    # para cruzar datos entre instalaciones.
    #
    # `wa_parent_user_id` es el identificador de la cuenta padre cuando el
    # negocio cuelga de una cartera de Meta. Se guarda solo como traza.
    #
    # Sin `unique=True` ni `index=True` a propósito: el único vive en un índice
    # PARCIAL (`ux_contacts_wa_user_id ... WHERE wa_user_id IS NOT NULL`, ver
    # migración 0053) para no indexar las miles de fichas de correo, web e
    # Instagram que no tienen BSUID. Declararlo aquí además haría que un
    # autogenerate de alembic propusiera un segundo índice encima del bueno.
    wa_user_id: Mapped[str | None] = mapped_column(String(128))
    wa_parent_user_id: Mapped[str | None] = mapped_column(String(128))
    estado: Mapped[ContactEstado] = mapped_column(
        contact_estado_enum, nullable=False, default=ContactEstado.contacto, index=True
    )
    origen: Mapped[ContactOrigen] = mapped_column(contact_origen_enum, nullable=False)
    servicio_interes: Mapped[str | None] = mapped_column(String(200))
    # --- Ficha CRM (todos opcionales) ---
    empresa: Mapped[str | None] = mapped_column(String(200))
    cargo: Mapped[str | None] = mapped_column(String(120))
    web: Mapped[str | None] = mapped_column(String(255))
    # NIF/CIF y dirección postal: PII fiscal → cifrados en reposo.
    nif: Mapped[str | None] = mapped_column(EncryptedText)
    direccion: Mapped[str | None] = mapped_column(EncryptedText)
    ultimo_mensaje_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Notas que el equipo escribe sobre el cliente: cifrado en reposo.
    notas_internas: Mapped[str | None] = mapped_column(EncryptedText)
    # Si True, aparece en la pestaña Contactos del CRM. WhatsApp/web se
    # crean con True (son leads válidos por defecto). Instagram se crea con
    # False — El administrador decide manualmente si lo añade al CRM con el botón
    # "Añadir al CRM" desde el inbox.
    in_crm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    notes: Mapped[list["ContactNote"]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ContactTag(Base):
    __tablename__ = "contact_tags"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
