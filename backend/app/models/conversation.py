import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted_type import EncryptedText
from app.db.session import Base


class ConversationStatus(str, enum.Enum):
    bot = "bot"
    humano = "humano"
    cerrada = "cerrada"


class ConversationCanal(str, enum.Enum):
    whatsapp = "whatsapp"
    web = "web"
    instagram_dm = "instagram_dm"
    email = "email"
    retell_voice = "retell_voice"


conv_status_enum = ENUM(
    ConversationStatus, name="conversation_status", create_type=False,
    values_callable=lambda x: [e.value for e in x],
)
conv_canal_enum = ENUM(
    ConversationCanal, name="conversation_canal", create_type=False,
    values_callable=lambda x: [e.value for e in x],
)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canal: Mapped[ConversationCanal] = mapped_column(conv_canal_enum, nullable=False)
    session_id: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        conv_status_enum, nullable=False, default=ConversationStatus.bot, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Resumen humano de la conversación (puede contener PII): cifrado en reposo.
    resumen: Mapped[str | None] = mapped_column(EncryptedText)
    # Resumen RODANTE para conversaciones largas: condensa los mensajes que ya
    # cayeron fuera de la ventana de contexto, para que el agente no pierda el
    # hilo inicial (nombre, motivo). Cifrado. `rolling_summary_upto` = nº de
    # mensajes más antiguos ya cubiertos por el resumen (para actualizar en
    # incrementos y no re-resumir todo cada vez).
    rolling_summary: Mapped[str | None] = mapped_column(EncryptedText)
    rolling_summary_upto: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    derivada_a_humano_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    asignada_a: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Archivada manualmente por el operador para sacarla de la bandeja por
    # defecto. Se desarchiva automaticamente cuando el cliente vuelve a
    # escribir (logica en services/conversation.py:store_incoming).
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Cuarentena del clasificador anti-spam: el bot no responde y la conversación
    # sale de la bandeja por defecto hasta que un operador la libere.
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    quarantine_reason: Mapped[str | None] = mapped_column(String(255))
    # Marcada como revisada por un humano (liberada): el clasificador no la
    # vuelve a evaluar (evita re-cuarentena en bucle).
    spam_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Canal Email (F5a): hilo de Gmail al que pertenece esta conversación. Se
    # usa para agrupar todos los correos del mismo hilo en una sola
    # conversación (en vez de la "última conversación no cerrada" del contacto).
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # Asunto del correo (canal Email). Se muestra en la cabecera y como preview
    # del inbox. Otros canales lo dejan en NULL.
    subject: Mapped[str | None] = mapped_column(Text)
    # Canal Voz (Retell) — metadatos de la llamada. Los rellena el webhook de
    # ciclo de vida (call_ended / call_analyzed), no el WebSocket del LLM. NULL
    # en el resto de canales (y en llamadas antiguas previas al webhook).
    # El fin de la llamada reutiliza `ended_at` y el resumen reutiliza `resumen`.
    call_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    # URL de la grabación en Retell. NO se descarga al servidor; el panel la
    # reproduce/enlaza directamente. Puede caducar (es un enlace de Retell).
    #
    # CIFRADA en reposo: mientras `opt_in_signed_url` no esté activo en el
    # agente de Retell, esta URL es PÚBLICA y sin caducidad — quien la tenga se
    # escucha la llamada entera del cliente sin credenciales. Guardarla en claro
    # equivalía a guardar el audio en claro. Solo se usa en Python (el panel la
    # recibe descifrada) y el único predicado SQL que la toca es `IS NOT NULL`,
    # que sigue funcionando sobre BYTEA.
    call_recording_url: Mapped[str | None] = mapped_column(EncryptedText)
    # Motivo de fin de la llamada (disconnection_reason de Retell).
    call_ended_reason: Mapped[str | None] = mapped_column(String(80))
    # Coste de TELEFONÍA de la llamada en dólares (I10).
    #
    # Por qué una columna propia y no la tabla de precios de modelos: Retell
    # factura por MINUTO de conversación, no por tokens. `llm_model_price`
    # tiene tarifas por millón de tokens de entrada/salida y `llm_usage_log`
    # guarda contadores de tokens; meter aquí unos "tokens" inventados para que
    # cuadrara la aritmética habría falseado la pantalla de consumo por modelo.
    # El minuto es un coste PLANO por conversación, así que vive en la
    # conversación y `services/budget.py` lo suma como un sumando aparte.
    #
    # Se rellena en el webhook de fin de llamada, prefiriendo SIEMPRE el coste
    # real que manda Retell (`call_cost.combined_cost`, en centavos) sobre la
    # estimación duración × `RETELL_PRICE_PER_MINUTE_USD`.
    call_cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    # De dónde salió `call_cost_usd`: "retell" (dato real facturado) o
    # "estimado" (duración × tarifa configurada en Ajustes). Sin esto, un cero
    # por tarifa sin configurar y un cero real de Retell son indistinguibles.
    call_cost_source: Mapped[str | None] = mapped_column(String(16))
