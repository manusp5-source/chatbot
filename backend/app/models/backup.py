"""Copias de seguridad — configuración (singleton) y registro de intentos.

El ciclo de una copia es: dump de Postgres →
cifrado AES-256-GCM (clave derivada de ENCRYPTION_KEY) → subida S3-compatible
a <bucket>/chatbot/<YYYY-MM-DD_HHmm>.dump.enc → retención → registro.

- `backup_settings`: fila ÚNICA con frecuencia/hora/proveedor. Las credenciales
  del proveedor (endpoint, access key, secret, bucket) NO viven aquí: van en la
  tabla `credentials` cifradas, como el resto de integraciones del panel
  (claves backup_s3_*).
- `backup_runs`: un registro por intento (copia programada, manual o restore),
  consultable desde la UI (estado "última copia" + histórico).
"""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

# Frecuencias válidas (mismos NOMBRES en el resto de módulos).
BACKUP_FREQUENCIES = ("hourly", "6h", "12h", "daily", "weekly", "monthly")
# Proveedores del desplegable (mismo form para los tres).
BACKUP_PROVIDERS = ("r2", "s3", "s3_compatible")
# Destino ELEGIDO de las copias (selector del panel):
#   cloud  → solo bucket: el dump temporal se borra tras subir con éxito
#            (si la subida falla se CONSERVA — nunca perder la única copia).
#   server → solo disco del servidor: no se sube nada aunque haya bucket.
#   both   → disco del servidor + bucket (comportamiento histórico).
# NULL = instalación anterior al selector: sigue el comportamiento histórico
# (both si hay bucket configurado, server si no) hasta que el usuario elija.
BACKUP_DESTINATIONS = ("cloud", "server", "both")


class BackupSettings(Base):
    __tablename__ = "backup_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # hourly | 6h | 12h | daily | weekly | monthly (DEFECTO diaria).
    frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="daily")
    # Hora local (Europe/Madrid) de las copias "de días" (diaria, semanal y
    # mensual corren a esta hora de pared Madrid).
    daily_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    # r2 | s3 | s3_compatible — solo etiqueta de UI; el form y el cliente S3
    # son idénticos para los tres.
    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="r2")
    # cloud | server | both — dónde ACABA cada copia (ver BACKUP_DESTINATIONS).
    # NULL = aún sin elegir → comportamiento histórico según haya bucket o no.
    destination: Mapped[str | None] = mapped_column(String(16))
    # Sello del último disparo PROGRAMADO (lo escribe backup_tick ANTES de
    # encolar para que dos ticks seguidos no dupliquen la copia).
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BackupRun(Base):
    __tablename__ = "backup_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # scheduled | manual | restore | verify (chequeo diario: la última copia
    # del bucket descifra y es un dump) | check (prueba diaria de conexión al
    # bucket, solo se registra cuando FALLA).
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # ok | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Clave del objeto en el bucket (chatbot/<fecha>.dump.enc). NULL si la
    # subida no llegó a producirse (fallo del dump, S3 sin configurar…).
    s3_key: Mapped[str | None] = mapped_column(String(255))
    # Nombre del fichero de dump en el disco del servidor. NULL si el dump
    # falló (los parciales se borran), en restauraciones, o cuando la copia fue
    # DIRECTA al bucket (destino "cloud" o rescate sin espacio en disco).
    local_file: Mapped[str | None] = mapped_column(String(255))
    # Destino externo de ESTA copia: uploaded (subida al bucket) | failed
    # (bucket configurado pero la subida falló) | not_configured (sin bucket:
    # la copia queda SOLO en el servidor) | skipped (el destino elegido es
    # "server": no se intentó subir). NULL si el dump falló o en restores.
    upload_status: Mapped[str | None] = mapped_column(String(16))
    # Tamaño del dump SIN cifrar (lo que ocuparía restaurado).
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # Motivo del fallo (truncado), visible en la UI. En una fila con status
    # "ok" es una NOTA, no un fallo: p. ej. "copia degradada: sin fichero local
    # por falta de espacio" (el panel la pinta en ámbar, no en rojo).
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
