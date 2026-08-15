"""Copias de seguridad cifradas a un bucket S3-compatible.

Este módulo concentra TODO lo que no es el pg_dump en sí (que sigue
viviendo en tasks/backup_db.py, con sus tests):

  - Credenciales del proveedor: claves `backup_s3_*` de la tabla `credentials`
    (cifradas como el resto de integraciones del panel).
  - Cifrado del dump ANTES de subirlo: AES-256-GCM con clave de 32 bytes
    derivada (HKDF-SHA256) de settings.ENCRYPTION_KEY. Dos formatos:
      · EKB1 (histórico) MAGIC(4) + nonce(12) + ciphertext||tag, un solo bloque.
      · EKB2 (nuevo) por trozos, para poder cifrar y subir SIN fichero local.
    Se descifran los dos; las copias ya subidas siguen restaurándose igual.
  - Subida a `<bucket>/chatbot/<YYYY-MM-DD_HHmm>.dump.enc` (hora Europe/Madrid).
    SOLO se escribe/borra dentro de la carpeta `chatbot/`. Dos caminos:
      · desde un fichero de dump ya escrito (process_dump), o
      · directo desde la salida de pg_dump, sin disco (stream_dump_to_bucket).
  - Retención remota tras cada copia: diaria → 7 diarias + 4 semanales; con
    frecuencia mayor se conservan además las últimas 24-48 copias.
  - Verificación: tamaño del objeto tras subirlo + prueba periódica real
    (descargar, descifrar y comprobar la cabecera del dump) + prueba diaria de
    conexión al bucket.
  - Registro de cada intento en `backup_runs` (consultable en la UI).
  - Restore: descargar → descifrar → pg_restore --clean --single-transaction
    (todo-o-nada) con el cliente Postgres del contenedor.

AVISO de recuperación de desastres: la copia se cifra con ENCRYPTION_KEY, que
es una variable de entorno del MISMO servidor. Si se pierde el servidor se
pierde la clave y el fichero del bucket es ruido: guarda ENCRYPTION_KEY fuera
(gestor de contraseñas). Ver docs/backups.md.

boto3 es síncrono: SIEMPRE se llama vía asyncio.to_thread para no bloquear el
event loop (los endpoints admin y las tasks Celery comparten estos helpers).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import tempfile
import threading
import time
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import db_session
from app.models.backup import (
    BACKUP_DESTINATIONS,
    BACKUP_FREQUENCIES,
    BackupRun,
    BackupSettings,
)

logger = get_logger(__name__)

# Carpeta de ESTA aplicación dentro del bucket. El bucket puede estar
# compartido con otras aplicaciones, cada una en su propia carpeta: por eso
# aquí NUNCA se escribe ni se borra nada fuera de este prefijo.
APP_PREFIX = "chatbot/"
KEY_SUFFIX = ".dump.enc"
KEY_STAMP_FMT = "%Y-%m-%d_%H%M"
TZ_MADRID = ZoneInfo("Europe/Madrid")

# Cabecera del blob cifrado: magic + versión (por si algún día cambia el
# esquema de cifrado, poder distinguir formatos al descifrar).
#   EKB1 — formato histórico: UN solo AES-GCM sobre el dump entero. No admite
#          streaming (hay que tener el dump completo en RAM para cifrarlo).
#   EKB2 — formato por trozos: permite cifrar y subir SIN fichero local y con
#          un pico de RAM de un trozo, no del tamaño del dump.
# Los DOS se siguen descifrando: las copias ya subidas son EKB1 y tienen que
# poder restaurarse igual (por eso el magic va delante y decrypt_backup_bytes
# despacha por él).
MAGIC = b"EKB1"
MAGIC_STREAM = b"EKB2"
NONCE_LEN = 12

# EKB2: nonce por trozo = base_nonce aleatorio (8 B, uno por copia) +
# contador (4 B BE, uno por trozo) → 12 B. Nunca se repite un nonce con la
# misma clave, que es la única regla que AES-GCM no perdona.
STREAM_NONCE_PREFIX_LEN = 8
STREAM_COUNTER_LEN = 4
# Cada trozo viaja como: flag(1) + longitud del cifrado(4 BE) + cifrado.
# El flag (0 = hay más, 1 = último) entra en el AAD, así que no se puede
# voltear sin romper la etiqueta: un fichero cortado por la mitad NO descifra.
# Esa es la detección de truncamiento que hoy no existe.
STREAM_FLAG_MORE = 0
STREAM_FLAG_LAST = 1
STREAM_HEADER_LEN = 1 + 4
# Tope defensivo al leer: un trozo declarado de más de esto es basura o un
# intento de que reservemos memoria a lo tonto.
STREAM_MAX_CHUNK_BYTES = 512 * 1024 * 1024

# Cabecera de un dump de PostgreSQL en formato custom. La usa la verificación
# periódica para comprobar que lo que hay en el bucket es un dump de verdad.
PG_DUMP_MAGIC = b"PGDMP"

# Claves en la tabla `credentials` (se editan por PUT /admin/credentials/{key},
# con su auditoría y su cifrado estándar).
CRED_ENDPOINT = "backup_s3_endpoint"
CRED_ACCESS_KEY = "backup_s3_access_key_id"
CRED_SECRET_KEY = "backup_s3_secret_access_key"
CRED_BUCKET = "backup_s3_bucket"
S3_CREDENTIAL_KEYS = (CRED_ENDPOINT, CRED_ACCESS_KEY, CRED_SECRET_KEY, CRED_BUCKET)

FREQUENCY_SECONDS = {"hourly": 3600, "6h": 6 * 3600, "12h": 12 * 3600}
# Frecuencias "de días": corren a la hora
# configurada (pared Europe/Madrid) cuando han pasado >= N días desde la
# última programada. daily=1 conserva el comportamiento histórico.
DAY_INTERVALS = {"daily": 1, "weekly": 7, "monthly": 30}
# Copias "recientes" a conservar además de 7 diarias + 4 semanales (spec:
# mayor frecuencia → últimas 24-48). Las frecuencias de días van a 0 porque
# sus copias YA son diarias o menos frecuentes.
RECENT_KEEP = {"hourly": 48, "6h": 28, "12h": 24, "daily": 0, "weekly": 0, "monthly": 0}

PG_RESTORE_TIMEOUT_SECS = 1800


# ---------------------------------------------------------------------------
# Cifrado AES-256-GCM (clave derivada de ENCRYPTION_KEY)
# ---------------------------------------------------------------------------


def _derive_backup_key() -> bytes:
    """32 bytes derivados de ENCRYPTION_KEY vía HKDF-SHA256.

    No usamos ENCRYPTION_KEY directamente: es una clave Fernet (base64) y
    derivar con un `info` propio separa dominios — comprometer/rotar una cosa
    no arrastra la otra. Determinista: la MISMA ENCRYPTION_KEY descifra los
    backups en otro despliegue (requisito de recuperación de desastres).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if not settings.ENCRYPTION_KEY:
        raise RuntimeError("ENCRYPTION_KEY no configurada: no se puede cifrar el backup")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"chatbot-backup-aes-256-gcm",
    )
    return hkdf.derive(settings.ENCRYPTION_KEY.encode())


def encrypt_backup_bytes(data: bytes) -> bytes:
    """MAGIC + nonce(12) + AESGCM(ciphertext||tag)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(_derive_backup_key()).encrypt(nonce, data, MAGIC)
    return MAGIC + nonce + ct


def decrypt_backup_bytes(blob: bytes) -> bytes:
    """Descifra una copia, sea del formato que sea (EKB1 o EKB2).

    El magic manda: las copias antiguas (un solo bloque) se siguen
    restaurando exactamente igual que antes.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if blob.startswith(MAGIC_STREAM):
        return decrypt_stream_blob(blob)
    if len(blob) < len(MAGIC) + NONCE_LEN + 16 or not blob.startswith(MAGIC):
        raise ValueError("Formato de backup cifrado no reconocido")
    nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_LEN]
    ct = blob[len(MAGIC) + NONCE_LEN :]
    return AESGCM(_derive_backup_key()).decrypt(nonce, ct, MAGIC)


# --- EKB2: cifrado por trozos (el que permite subir sin fichero local) ------


def _stream_nonce(base: bytes, counter: int) -> bytes:
    return base + counter.to_bytes(STREAM_COUNTER_LEN, "big")


def _stream_aad(counter: int, flag: int) -> bytes:
    """AAD por trozo: magic + nº de trozo + flag de "último".

    Ata cada trozo a su POSICIÓN y a si es el final. Reordenar, quitar o
    repetir trozos rompe la etiqueta GCM, y un fichero cortado se queda sin
    su trozo final → error en vez de un dump a medias que parece bueno.
    """
    return MAGIC_STREAM + counter.to_bytes(STREAM_COUNTER_LEN, "big") + bytes([flag])


class ChunkedEncryptingReader:
    """Fichero de solo lectura que cifra al vuelo lo que lee de `source`.

    boto3 (`upload_fileobj`) solo pide `.read(n)` y va subiendo por partes,
    así que el dump nunca se materializa entero: ni en disco (no hace falta
    fichero local) ni en RAM (el pico es un trozo en claro + su cifrado, 8 MB
    por defecto). Es lo contrario del camino histórico —leer el .dump entero
    y cifrarlo a otro búfer— que necesitaba el DOBLE del tamaño del dump en
    memoria y tenía el techo de 5 GB de `put_object`.

    `bytes_read` (dump en claro) y `bytes_out` (cifrado subido) quedan
    expuestos para poder verificar el objeto después de subirlo.
    """

    def __init__(self, source, chunk_size: int) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._source = source
        self._chunk_size = max(1, chunk_size)
        self._aes = AESGCM(_derive_backup_key())
        self._base_nonce = os.urandom(STREAM_NONCE_PREFIX_LEN)
        self._counter = 0
        self._eof = False
        self._buf = bytearray(MAGIC_STREAM + self._base_nonce)
        self.bytes_read = 0
        self.bytes_out = 0

    def _read_exactly(self, n: int) -> bytes:
        """Lee n bytes o hasta EOF. Un pipe devuelve lecturas cortas: hay que
        insistir o partiríamos el dump en trozos minúsculos."""
        parts: list[bytes] = []
        left = n
        while left > 0:
            piece = self._source.read(left)
            if not piece:
                break
            parts.append(piece)
            left -= len(piece)
        return b"".join(parts)

    def _fill_one_chunk(self) -> None:
        plain = self._read_exactly(self._chunk_size)
        self.bytes_read += len(plain)
        # Un trozo corto (o vacío) es el final. Si el dump mide justo un
        # múltiplo del bloque, el último trozo va vacío: existe SOLO para
        # marcar el final, y sin él no habría forma de detectar un corte.
        last = len(plain) < self._chunk_size
        flag = STREAM_FLAG_LAST if last else STREAM_FLAG_MORE
        ct = self._aes.encrypt(
            _stream_nonce(self._base_nonce, self._counter),
            plain,
            _stream_aad(self._counter, flag),
        )
        self._buf += bytes([flag]) + len(ct).to_bytes(4, "big") + ct
        self._counter += 1
        if last:
            self._eof = True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self._eof:
                self._fill_one_chunk()
            out = bytes(self._buf)
            self._buf.clear()
        else:
            while len(self._buf) < size and not self._eof:
                self._fill_one_chunk()
            out = bytes(self._buf[:size])
            del self._buf[:size]
        self.bytes_out += len(out)
        return out


def encrypt_backup_stream(source, chunk_size: int) -> ChunkedEncryptingReader:
    return ChunkedEncryptingReader(source, chunk_size)


def decrypt_stream_blob(blob: bytes) -> bytes:
    """EKB2 → dump en claro. Lanza si falta el trozo final (truncamiento)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    head = len(MAGIC_STREAM) + STREAM_NONCE_PREFIX_LEN
    if len(blob) < head or not blob.startswith(MAGIC_STREAM):
        raise ValueError("Formato de backup cifrado no reconocido")
    base_nonce = blob[len(MAGIC_STREAM) : head]
    aes = AESGCM(_derive_backup_key())
    out = bytearray()
    pos = head
    counter = 0
    while True:
        if pos + STREAM_HEADER_LEN > len(blob):
            raise ValueError("Copia cifrada truncada: falta el bloque final")
        flag = blob[pos]
        length = int.from_bytes(blob[pos + 1 : pos + STREAM_HEADER_LEN], "big")
        pos += STREAM_HEADER_LEN
        if flag not in (STREAM_FLAG_MORE, STREAM_FLAG_LAST) or length > STREAM_MAX_CHUNK_BYTES:
            raise ValueError("Copia cifrada corrupta: cabecera de bloque no válida")
        ct = blob[pos : pos + length]
        if len(ct) != length:
            raise ValueError("Copia cifrada truncada: bloque incompleto")
        pos += length
        out += aes.decrypt(_stream_nonce(base_nonce, counter), ct, _stream_aad(counter, flag))
        counter += 1
        if flag == STREAM_FLAG_LAST:
            break
    if pos != len(blob):
        raise ValueError("Copia cifrada corrupta: sobran bytes tras el bloque final")
    return bytes(out)


# ---------------------------------------------------------------------------
# Cliente S3 (credenciales del panel)
# ---------------------------------------------------------------------------


@dataclass
class S3Config:
    endpoint: str
    access_key_id: str
    secret_access_key: str
    bucket: str


async def get_s3_config() -> S3Config | None:
    """Lee las 4 credenciales del panel. None si falta alguna (no configurado).

    LANZA `CredentialUnavailableError` si alguna credencial está guardada pero
    no se puede descifrar: eso NO es "no configurado" — es "hay bucket puesto y
    no lo podemos usar". Devolver None ahí hacía que las copias se registraran
    en verde sin subir nada (ver process_dump).
    """
    from app.services.credentials import get_credential

    values: dict[str, str | None] = {}
    for key in S3_CREDENTIAL_KEYS:
        values[key] = await get_credential(key, strict=True)
    if not all((values[k] or "").strip() for k in S3_CREDENTIAL_KEYS):
        return None
    return S3Config(
        endpoint=values[CRED_ENDPOINT].strip(),  # type: ignore[union-attr]
        access_key_id=values[CRED_ACCESS_KEY].strip(),  # type: ignore[union-attr]
        secret_access_key=values[CRED_SECRET_KEY].strip(),  # type: ignore[union-attr]
        bucket=values[CRED_BUCKET].strip(),  # type: ignore[union-attr]
    )


def _s3_client(cfg: S3Config):
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        # R2 ignora la región; "auto" es lo que documenta Cloudflare. Path-style
        # para máxima compatibilidad (MinIO y otros S3-compatible lo exigen).
        region_name="auto",
        config=BotoConfig(
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 3},
            s3={"addressing_style": "path"},
        ),
    )


def _sync_upload(cfg: S3Config, key: str, body: bytes) -> None:
    _s3_client(cfg).put_object(Bucket=cfg.bucket, Key=key, Body=body)


def _sync_upload_fileobj(cfg: S3Config, key: str, fileobj, part_bytes: int) -> None:
    """Sube leyendo de un fichero/pipe, en varias partes si hace falta.

    `put_object` exige el objeto entero en memoria y tiene techo de 5 GB;
    `upload_fileobj` trocea solo. use_threads=False a propósito: la fuente es
    un pipe (la salida de pg_dump) que solo se puede leer en orden y una vez.
    """
    from boto3.s3.transfer import TransferConfig

    _s3_client(cfg).upload_fileobj(
        fileobj,
        cfg.bucket,
        key,
        Config=TransferConfig(
            multipart_threshold=part_bytes,
            multipart_chunksize=part_bytes,
            max_concurrency=1,
            use_threads=False,
        ),
    )


def _sync_head_size(cfg: S3Config, key: str) -> int:
    """Tamaño REAL del objeto en el bucket (verificación post-subida)."""
    return int(_s3_client(cfg).head_object(Bucket=cfg.bucket, Key=key)["ContentLength"])


async def verify_uploaded_size(cfg: S3Config, key: str, expected: int) -> None:
    """Comprueba que lo que hay en el bucket pesa lo que subimos.

    Sin esto, una subida cortada (red, token caducado a mitad, multiparte
    incompleta) quedaba registrada en verde: el único filtro era que el dump
    LOCAL pesara más de 1 KB, y del objeto remoto no se comprobaba nada.
    """
    real = await asyncio.to_thread(_sync_head_size, cfg, key)
    if real != expected:
        raise RuntimeError(
            f"El objeto subido no cuadra: {real} bytes en el bucket frente a "
            f"{expected} subidos (copia incompleta)"
        )


def _sync_download(cfg: S3Config, key: str) -> bytes:
    obj = _s3_client(cfg).get_object(Bucket=cfg.bucket, Key=key)
    return obj["Body"].read()


def _sync_delete(cfg: S3Config, keys: list[str]) -> None:
    if not keys:
        return
    client = _s3_client(cfg)
    for i in range(0, len(keys), 1000):
        chunk = keys[i : i + 1000]
        client.delete_objects(
            Bucket=cfg.bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
        )


def _sync_list(cfg: S3Config) -> list[dict]:
    """Objetos bajo chatbot/ → [{key, size, last_modified}] (orden: recientes 1º)."""
    client = _s3_client(cfg)
    out: list[dict] = []
    token: str | None = None
    while True:
        kwargs = {"Bucket": cfg.bucket, "Prefix": APP_PREFIX, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            out.append(
                {
                    "key": obj["Key"],
                    "size": int(obj.get("Size") or 0),
                    "last_modified": obj.get("LastModified"),
                }
            )
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    out.sort(key=lambda o: o["key"], reverse=True)
    return out


def _sync_test_connection(cfg: S3Config) -> None:
    """Escribe y borra un objeto de prueba DENTRO de chatbot/ (spec)."""
    key = f"{APP_PREFIX}.connection-test-{uuid_mod.uuid4().hex[:8]}"
    client = _s3_client(cfg)
    client.put_object(Bucket=cfg.bucket, Key=key, Body=b"backup-connection-test")
    client.delete_object(Bucket=cfg.bucket, Key=key)


async def test_connection() -> tuple[bool, str]:
    from app.services.credentials import CredentialUnavailableError

    try:
        cfg = await get_s3_config()
    except CredentialUnavailableError as exc:
        # Guardadas pero ilegibles: decirlo tal cual, no "faltan credenciales".
        logger.error("backups.credentials_unreadable", error=str(exc)[:300])
        return False, (
            "Las credenciales están guardadas pero no se pueden descifrar "
            "(¿ENCRYPTION_KEY cambiada?). Vuelve a guardarlas."
        )
    if cfg is None:
        return False, "Faltan credenciales: endpoint, Access Key ID, Secret y bucket"
    try:
        await asyncio.to_thread(_sync_test_connection, cfg)
        return True, "Conexión OK: objeto de prueba escrito y borrado"
    except Exception as exc:  # boto lanza ClientError/EndpointConnectionError…
        logger.warning("backups.test_connection.failed", error=str(exc)[:300])
        return False, f"Error: {str(exc)[:200]}"


# ---------------------------------------------------------------------------
# Retención remota
# ---------------------------------------------------------------------------


def parse_key_datetime(key: str) -> datetime | None:
    """chatbot/2026-07-14_0400.dump.enc → datetime aware (Europe/Madrid)."""
    name = key[len(APP_PREFIX) :] if key.startswith(APP_PREFIX) else key
    if not name.endswith(KEY_SUFFIX):
        return None
    try:
        return datetime.strptime(name[: -len(KEY_SUFFIX)], KEY_STAMP_FMT).replace(
            tzinfo=TZ_MADRID
        )
    except ValueError:
        return None


def compute_retention_deletions(keys: list[str], frequency: str) -> list[str]:
    """Qué claves BORRAR según la política de la spec. Función pura (testeable).

    Se conserva la unión de:
      - las últimas RECENT_KEEP[frequency] copias (24-48 si la frecuencia es
        mayor que diaria; 0 si diaria),
      - la última copia de cada uno de los últimos 7 días con copia,
      - la última copia de cada una de las últimas 4 semanas ISO con copia.
    Todo lo demás se borra. Claves con formato desconocido NI SE TOCAN.
    """
    entries: list[tuple[datetime, str]] = []
    for k in keys:
        dt = parse_key_datetime(k)
        if dt is not None:
            entries.append((dt, k))
    entries.sort(reverse=True)

    keep: set[str] = set()
    recent_n = RECENT_KEEP.get(frequency, 0)
    # Siempre se conserva al menos la más reciente, pase lo que pase.
    for _, k in entries[: max(recent_n, 1)]:
        keep.add(k)

    latest_per_day: dict = {}
    latest_per_week: dict = {}
    for dt, k in entries:  # orden descendente → el primero de cada grupo es el último del grupo
        day = dt.date()
        if day not in latest_per_day:
            latest_per_day[day] = k
        week = dt.isocalendar()[:2]
        if week not in latest_per_week:
            latest_per_week[week] = k
    for day in sorted(latest_per_day, reverse=True)[:7]:
        keep.add(latest_per_day[day])
    for week in sorted(latest_per_week, reverse=True)[:4]:
        keep.add(latest_per_week[week])

    return [k for _, k in entries if k not in keep]


async def apply_remote_retention(cfg: S3Config) -> None:
    """Aplica la retención del bucket. Best-effort: un fallo del listado o del
    borrado NO invalida la copia recién subida (que es lo que importa)."""
    try:
        row = await get_settings_row()
        frequency = row.frequency if row else "daily"
        objects = await asyncio.to_thread(_sync_list, cfg)
        to_delete = compute_retention_deletions([o["key"] for o in objects], frequency)
        if to_delete:
            await asyncio.to_thread(_sync_delete, cfg, to_delete)
            logger.info("backups.retention_applied", deleted=len(to_delete))
    except Exception as exc:
        logger.warning("backups.retention_failed", error=str(exc)[:300])


# ---------------------------------------------------------------------------
# Settings + registro de intentos
# ---------------------------------------------------------------------------


async def get_settings_row() -> BackupSettings | None:
    async with db_session() as db:
        return (await db.execute(select(BackupSettings).limit(1))).scalar_one_or_none()


async def record_run(
    *,
    kind: str,
    status: str,
    s3_key: str | None = None,
    size_bytes: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    local_file: str | None = None,
    upload_status: str | None = None,
) -> None:
    async with db_session() as db:
        db.add(
            BackupRun(
                kind=kind,
                status=status,
                s3_key=s3_key,
                size_bytes=size_bytes,
                duration_ms=duration_ms,
                error=error[:1000] if error else None,
                local_file=local_file,
                upload_status=upload_status,
            )
        )
        await db.commit()


async def _record_run_safe(**kwargs) -> None:
    """record_run que nunca rompe el flujo de backup (BD caída, tests eager)."""
    try:
        await record_run(**kwargs)
    except Exception as exc:
        logger.warning("backups.record_run_failed", error=str(exc)[:200])


async def last_successful_backup_at() -> datetime | None:
    """Fecha de la última copia CORRECTA (programada o manual).

    "Última copia" a secas no vale para saber si estás protegido: si la de hoy
    falló, lo que importa es cuánto hace de la última buena.
    """
    async with db_session() as db:
        return (
            await db.execute(
                select(BackupRun.created_at)
                .where(
                    BackupRun.status == "ok",
                    BackupRun.kind.in_(("scheduled", "manual")),
                )
                .order_by(desc(BackupRun.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()


async def list_runs(limit: int = 30) -> list[BackupRun]:
    async with db_session() as db:
        rows = (
            await db.execute(select(BackupRun).order_by(desc(BackupRun.created_at)).limit(limit))
        ).scalars().all()
        return list(rows)


async def last_copy_run() -> BackupRun | None:
    """Última fila que es una COPIA (programada o manual).

    La tarjeta "Última copia" del panel no puede coger la última fila a secas:
    en el histórico también hay restauraciones y los chequeos diarios
    (verify/check), y taparían el estado real de las copias.
    """
    async with db_session() as db:
        return (
            await db.execute(
                select(BackupRun)
                .where(BackupRun.kind.in_(("scheduled", "manual")))
                .order_by(desc(BackupRun.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()


def compute_next_run(
    frequency: str,
    daily_hour: int,
    last_scheduled_at: datetime | None,
    now: datetime | None = None,
) -> datetime:
    """Próxima copia programada (para el estado de la UI)."""
    now = now or datetime.now(timezone.utc)
    if frequency in FREQUENCY_SECONDS:
        if last_scheduled_at is None:
            return now
        return last_scheduled_at + timedelta(seconds=FREQUENCY_SECONDS[frequency])
    # Frecuencias "de días" (diaria/semanal/mensual): corren a daily_hour en
    # hora de pared Europe/Madrid, cada DAY_INTERVALS[frequency] días.
    interval_days = DAY_INTERVALS.get(frequency, 1)
    local_now = now.astimezone(TZ_MADRID)
    target = local_now.replace(hour=daily_hour, minute=0, second=0, microsecond=0)
    if last_scheduled_at is None:
        # Nunca corrió: hoy a la hora configurada (o ya mismo si la hora pasó).
        return now if local_now >= target else target.astimezone(timezone.utc)
    next_local = datetime.combine(
        last_scheduled_at.astimezone(TZ_MADRID).date() + timedelta(days=interval_days),
        dt_time(hour=daily_hour),
        tzinfo=TZ_MADRID,
    )
    if next_local <= local_now:
        # El slot ya pasó sin ejecutarse → el tick lo disparará ya.
        return now
    return next_local.astimezone(timezone.utc)


def day_slot_due(
    frequency: str, daily_hour: int, last: datetime | None, now: datetime
) -> bool:
    """¿Toca copia de una frecuencia de días (diaria/semanal/mensual)?

    Función pura (testeable), pared Madrid: toca cuando la hora local ya
    alcanzó la configurada y han pasado >= N días (en fecha local) desde la
    última programada. Frecuencias desconocidas caen a diaria (fail-safe).
    """
    interval_days = DAY_INTERVALS.get(frequency, 1)
    local_now = now.astimezone(TZ_MADRID)
    target = local_now.replace(
        hour=max(0, min(23, daily_hour)), minute=0, second=0, microsecond=0
    )
    if local_now < target:
        return False
    if last is None:
        return True
    days = (local_now.date() - last.astimezone(TZ_MADRID).date()).days
    return days >= interval_days


async def claim_scheduled_slot(now: datetime | None = None) -> bool:
    """¿Toca copia programada? Si sí, sella last_scheduled_at y devuelve True.

    La escritura del sello ANTES de encolar hace de candado: el beat dispara
    el tick cada pocos minutos y sin esto dos ticks seguidos duplicarían la
    copia (no-solape de la spec).
    """
    now = now or datetime.now(timezone.utc)
    async with db_session() as db:
        row = (
            await db.execute(select(BackupSettings).limit(1).with_for_update())
        ).scalar_one_or_none()
        if row is None or not row.enabled:
            return False
        last = row.last_scheduled_at
        if row.frequency in FREQUENCY_SECONDS:
            interval = FREQUENCY_SECONDS[row.frequency]
            due = last is None or (now - last).total_seconds() >= interval
        else:
            # daily/weekly/monthly (y cualquier valor desconocido cae a
            # diaria: fail-safe): hora de pared Madrid cada N días.
            due = day_slot_due(row.frequency, row.daily_hour, last, now)
        if due:
            row.last_scheduled_at = now
            await db.commit()
        return due


# ---------------------------------------------------------------------------
# Destino de las copias (selector del panel)
# ---------------------------------------------------------------------------


def resolve_destination(destination: str | None, s3_configured: bool) -> str:
    """Destino EFECTIVO de las copias. Función pura (testeable).

    Si el usuario eligió uno válido, ese. Si aún no eligió (NULL, instalación
    anterior al selector), comportamiento histórico: con bucket configurado la
    copia iba al servidor Y al bucket (both); sin bucket, solo al servidor.
    """
    if destination in BACKUP_DESTINATIONS:
        return destination  # type: ignore[return-value]
    return "both" if s3_configured else "server"


async def get_effective_destination() -> str:
    """Destino efectivo de la próxima copia. RELANZA si no se puede saber.

    Antes, un fallo leyendo las credenciales se tragaba y se resolvía como "no
    hay bucket" → destino "server" → la copia se registraba ok/skipped sin que
    nadie hubiera decidido eso. Ahora el error sube: el llamante
    (tasks/backup_db._effective_destination) tiene su propio fail-safe, "both",
    que conserva el dump local Y fuerza el intento de subida.
    """
    row = await get_settings_row()
    cfg = await get_s3_config()
    return resolve_destination(row.destination if row else None, cfg is not None)


# ---------------------------------------------------------------------------
# Post-dump: cifrar + subir + retención + registro
# ---------------------------------------------------------------------------


def build_s3_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{APP_PREFIX}{now.astimezone(TZ_MADRID).strftime(KEY_STAMP_FMT)}{KEY_SUFFIX}"


async def process_dump(
    dump_path: Path, *, kind: str, dump_duration_ms: int, destination: str = "both"
) -> None:
    """Tras un pg_dump correcto: cifra, sube, aplica retención y registra.

    `destination` (selector del panel) manda:
      - server → NO se sube nada (upload_status="skipped"); la copia queda en
        el disco del servidor.
      - cloud  → se sube y, SOLO si la subida fue bien, se borra el dump local
        (no acumular ficheros en el servidor). Si la subida falla, el dump se
        CONSERVA — nunca perder la única copia — y se relanza para que la task
        alerte. Si el bucket no está configurado, también se conserva.
      - both   → comportamiento histórico: dump en disco + subida al bucket.

    Si S3 no está configurado, registra el intento como ok SIN s3_key (la
    copia local existe; la UI avisa de que falta conectar el bucket). Un fallo
    de cifrado/subida registra el intento como failed y RELANZA para que la
    task alerte — el dump local se conserva igualmente.

    "No configurado" es SOLO cuando de verdad no hay credenciales guardadas. Si
    no se pueden LEER (credencial que no descifra, Redis/BD caídos) la copia se
    registra como FALLIDA y se relanza: no sabemos si había destino remoto y,
    desde luego, no se ha subido nada. Antes esto salía en verde.
    """
    t0 = time.monotonic()
    size_bytes = dump_path.stat().st_size
    if destination == "server":
        # Destino elegido: solo servidor. Ni siquiera se mira el bucket.
        await _record_run_safe(
            kind=kind,
            status="ok",
            size_bytes=size_bytes,
            duration_ms=dump_duration_ms,
            local_file=dump_path.name,
            upload_status="skipped",
        )
        logger.info("backups.server_only", file=dump_path.name)
        return
    try:
        cfg = await get_s3_config()
    except Exception as exc:
        # NO se ha podido LEER la configuración del bucket (credencial que no
        # descifra, Redis/BD caídos). Jamás dar la copia por buena: no se ha
        # subido nada y ni siquiera sabemos si había destino remoto. Se registra
        # como fallida y se relanza para que la task alerte (el dump local, que
        # ya está hecho, se conserva).
        total_ms = dump_duration_ms + int((time.monotonic() - t0) * 1000)
        logger.error("backups.s3_config_unreadable", error=str(exc)[:300])
        await _record_run_safe(
            kind=kind,
            status="failed",
            size_bytes=size_bytes,
            duration_ms=total_ms,
            error=(
                "No se pudo leer la configuración del bucket: la copia NO se ha "
                f"subido (dump local conservado): {str(exc)[:250]}"
            ),
            local_file=dump_path.name,
            upload_status="failed",
        )
        raise
    if cfg is None:
        await _record_run_safe(
            kind=kind,
            status="ok",
            size_bytes=size_bytes,
            duration_ms=dump_duration_ms,
            local_file=dump_path.name,
            upload_status="not_configured",
        )
        logger.info("backups.s3_not_configured", file=dump_path.name)
        return

    key = build_s3_key()
    try:
        raw = await asyncio.to_thread(dump_path.read_bytes)
        blob = await asyncio.to_thread(encrypt_backup_bytes, raw)
        await asyncio.to_thread(_sync_upload, cfg, key, blob)
        # Verificar el objeto ANTES de darlo por bueno (una subida a medias
        # se registraba en verde).
        await verify_uploaded_size(cfg, key, len(blob))
        await apply_remote_retention(cfg)
        # Destino "cloud": la copia ya está a salvo en el bucket → el dump
        # temporal se borra para no acumular ficheros en el servidor. SOLO
        # tras subida OK; si el borrado falla, se conserva (mal menor).
        local_file: str | None = dump_path.name
        if destination == "cloud":
            try:
                await asyncio.to_thread(dump_path.unlink, missing_ok=True)
                local_file = None
                logger.info("backups.local_dump_removed", file=dump_path.name)
            except OSError as exc:
                logger.warning(
                    "backups.local_dump_remove_failed",
                    file=dump_path.name,
                    error=str(exc)[:200],
                )
        total_ms = dump_duration_ms + int((time.monotonic() - t0) * 1000)
        await _record_run_safe(
            kind=kind,
            status="ok",
            s3_key=key,
            size_bytes=size_bytes,
            duration_ms=total_ms,
            local_file=local_file,
            upload_status="uploaded",
        )
        logger.info(
            "backups.uploaded",
            key=key,
            size_mb=round(size_bytes / 1024 / 1024, 2),
            encrypted_mb=round(len(blob) / 1024 / 1024, 2),
        )
    except Exception as exc:
        total_ms = dump_duration_ms + int((time.monotonic() - t0) * 1000)
        await _record_run_safe(
            kind=kind,
            status="failed",
            s3_key=key,
            size_bytes=size_bytes,
            duration_ms=total_ms,
            error=f"Subida a S3 fallida (dump local conservado): {str(exc)[:300]}",
            local_file=dump_path.name,
            upload_status="failed",
        )
        raise


# ---------------------------------------------------------------------------
# Copia DIRECTA al bucket (sin fichero local): pg_dump → cifrado por trozos →
# subida multiparte. Es el camino normal con destino "cloud" y el de rescate
# cuando no queda disco pero SÍ hay destino remoto.
# ---------------------------------------------------------------------------

# Motivo legible que se guarda en la copia hecha sin fichero local por falta
# de espacio (el panel lo enseña como nota, no como fallo).
DEGRADED_NO_DISK_REASON = (
    "Copia degradada: sin fichero local por falta de espacio en el servidor. "
    "La copia se ha subido cifrada al bucket directamente."
)
# Rescate cuando el volcado a fichero falló (p. ej. el disco se llenó a mitad,
# que el guardia no ve porque mira antes de empezar).
DEGRADED_DUMP_FAILED_REASON = (
    "Copia degradada: sin fichero local porque el volcado a disco falló. "
    "La copia se ha subido cifrada al bucket directamente."
)


@dataclass
class StreamResult:
    """Resultado de la copia en streaming.

    status: "done" (subida y registrada) · "no_bucket" (no hay destino remoto
    usable → que decida el llamante) · "failed" (se intentó y falló; ya está
    registrado en backup_runs, falta alertar).
    """

    status: str
    key: str | None = None
    size_bytes: int = 0
    error: str | None = None


def _discard_remote_object(cfg: S3Config, key: str) -> None:
    """Borra el objeto a medio subir. Un fichero cortado en el bucket es peor
    que ninguno: parece una copia buena hasta el día que la necesitas."""
    try:
        _sync_delete(cfg, [key])
    except Exception as exc:
        logger.warning("backups.discard_failed", key=key, error=str(exc)[:200])


async def stream_dump_to_bucket(
    *, kind: str, degraded_reason: str | None = None
) -> StreamResult:
    """Copia completa SIN tocar el disco del servidor.

    pg_dump escribe a su salida estándar, se cifra por trozos (EKB2) según se
    lee y se sube en varias partes. Ni el dump ni su cifrado se materializan
    enteros: el pico de memoria es un bloque (BACKUP_STREAM_CHUNK_MB), no dos
    veces el tamaño de la base de datos.

    Se comprueba DESPUÉS de subir: código de salida de pg_dump, tamaño mínimo
    del dump y tamaño real del objeto en el bucket. Si algo no cuadra se borra
    el objeto — media copia en el bucket engaña más que no tener ninguna.
    """
    from app.tasks.backup_db import (
        MIN_DUMP_BYTES,
        PG_DUMP_TIMEOUT_SECS,
        build_pg_dump_stdout_command,
        parse_database_url,
    )

    t0 = time.monotonic()
    try:
        cfg = await get_s3_config()
    except Exception as exc:
        # Credenciales guardadas pero ilegibles: no es "no configurado", pero
        # tampoco podemos subir. Lo decide el llamante (con disco, camino
        # clásico; sin disco, fallo con motivo).
        logger.error("backups.s3_config_unreadable", error=str(exc)[:300])
        return StreamResult(
            status="no_bucket",
            error=f"No se pudo leer la configuración del bucket: {str(exc)[:200]}",
        )
    if cfg is None:
        return StreamResult(status="no_bucket", error="Bucket no configurado")

    db = parse_database_url(settings.DATABASE_URL)
    cmd = build_pg_dump_stdout_command(db)
    # La password va SOLO en el env del subprocess, como en el camino clásico.
    env = {**os.environ, "PGPASSWORD": db["password"]}
    key = build_s3_key()
    chunk_bytes = max(1, settings.BACKUP_STREAM_CHUNK_MB) * 1024 * 1024

    proc = subprocess.Popen(  # noqa: S603 — argv fijo, sin shell
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    # stderr se vacía en un hilo: si pg_dump escribe mucho ahí y nadie lo lee,
    # llena el pipe, se bloquea y deja de producir stdout → interbloqueo.
    stderr_out: list[bytes] = []
    drain = threading.Thread(
        target=lambda: stderr_out.append(proc.stderr.read() or b""), daemon=True
    )
    drain.start()

    reader = ChunkedEncryptingReader(proc.stdout, chunk_bytes)
    try:
        await asyncio.to_thread(_sync_upload_fileobj, cfg, key, reader, chunk_bytes)
        rc = await asyncio.to_thread(proc.wait, PG_DUMP_TIMEOUT_SECS)
        drain.join(timeout=5)
        stderr_txt = (stderr_out[0] if stderr_out else b"").decode(errors="replace").strip()
        if rc != 0:
            # pg_dump murió a mitad: lo que se ha subido es un dump cortado.
            raise RuntimeError(f"pg_dump rc={rc}: {stderr_txt[-300:]}")
        if reader.bytes_read <= MIN_DUMP_BYTES:
            raise RuntimeError("dump sospechosamente pequeño (<= 1KB)")
        await verify_uploaded_size(cfg, key, reader.bytes_out)
    except Exception as exc:
        with contextlib.suppress(Exception):
            proc.kill()  # si ya había terminado, da igual
        await asyncio.to_thread(_discard_remote_object, cfg, key)
        duration_ms = int((time.monotonic() - t0) * 1000)
        error = f"Copia directa al bucket fallida (sin fichero local): {str(exc)[:300]}"
        logger.error("backups.stream_failed", key=key, error=str(exc)[:300])
        await _record_run_safe(
            kind=kind,
            status="failed",
            duration_ms=duration_ms,
            error=error,
            upload_status="failed",
        )
        return StreamResult(status="failed", key=key, error=error)

    await apply_remote_retention(cfg)
    duration_ms = int((time.monotonic() - t0) * 1000)
    await _record_run_safe(
        kind=kind,
        status="ok",
        s3_key=key,
        size_bytes=reader.bytes_read,
        duration_ms=duration_ms,
        # local_file=None a propósito: el panel lo pinta como "solo bucket".
        local_file=None,
        upload_status="uploaded",
        error=degraded_reason,
    )
    logger.info(
        "backups.streamed",
        key=key,
        size_mb=round(reader.bytes_read / 1024 / 1024, 2),
        encrypted_mb=round(reader.bytes_out / 1024 / 1024, 2),
        degraded=bool(degraded_reason),
    )
    return StreamResult(status="done", key=key, size_bytes=reader.bytes_read)


# ---------------------------------------------------------------------------
# Verificación: que la copia del bucket VALGA algo
# ---------------------------------------------------------------------------

# Por encima de esto no se descarga entera una copia del formato antiguo
# (EKB1): en un servidor de 3,8 GB, meterse 2 GB en RAM para comprobarla
# tumbaría el worker. Las copias nuevas (EKB2) se verifican por rango, sin
# descargar el resto, así que este tope no las afecta.
VERIFY_FULL_DOWNLOAD_MAX_BYTES = 1024 * 1024 * 1024


def _sync_download_range(cfg: S3Config, key: str, last_byte: int) -> bytes:
    obj = _s3_client(cfg).get_object(Bucket=cfg.bucket, Key=key, Range=f"bytes=0-{last_byte}")
    return obj["Body"].read()


async def verify_last_backup() -> tuple[bool, str]:
    """Descarga la última copia, la DESCIFRA y comprueba que es un dump real.

    Es la única prueba de que la copia sirve: hasta ahora nada garantizaba que
    lo del bucket se pudiera descifrar ni que fuera un dump de PostgreSQL. Un
    fichero cortado o cifrado con otra clave pasaba desapercibido hasta el día
    de la restauración.

    Queda registrado en el histórico como kind="verify".
    """
    t0 = time.monotonic()
    try:
        cfg = await get_s3_config()
    except Exception as exc:
        return False, f"No se pudo leer la configuración del bucket: {str(exc)[:200]}"
    if cfg is None:
        return True, "Sin bucket configurado: nada que verificar"
    objects = await asyncio.to_thread(_sync_list, cfg)
    copies = [o for o in objects if parse_key_datetime(o["key"]) is not None]
    if not copies:
        return True, "Todavía no hay copias en el bucket"
    latest = copies[0]
    key = latest["key"]
    chunk_bytes = max(1, settings.BACKUP_STREAM_CHUNK_MB) * 1024 * 1024
    try:
        head = await asyncio.to_thread(
            _sync_download_range, cfg, key, len(MAGIC_STREAM) + STREAM_NONCE_PREFIX_LEN
            + STREAM_HEADER_LEN + chunk_bytes + 64
        )
        if head.startswith(MAGIC_STREAM):
            # Formato por trozos: basta el PRIMER bloque para descifrar y ver
            # la cabecera del dump. Verificación barata sin bajarlo entero.
            dump_head = await asyncio.to_thread(_decrypt_first_stream_chunk, head)
        elif int(latest["size"] or 0) <= VERIFY_FULL_DOWNLOAD_MAX_BYTES:
            blob = await asyncio.to_thread(_sync_download, cfg, key)
            dump_head = await asyncio.to_thread(decrypt_backup_bytes, blob)
        else:
            raise RuntimeError(
                "La copia usa el formato antiguo (bloque único) y pesa "
                f"{round(int(latest['size']) / 1024 / 1024)} MB: no se puede "
                "verificar sin cargarla entera en memoria. La próxima copia "
                "ya usará el formato por trozos y sí se verificará."
            )
        if not dump_head.startswith(PG_DUMP_MAGIC):
            raise RuntimeError("descifra, pero NO es un dump de PostgreSQL (falta la cabecera)")
    except Exception as exc:
        message = f"Verificación FALLIDA de {key}: {str(exc)[:250]}"
        logger.error("backups.verify_failed", key=key, error=str(exc)[:300])
        await _record_run_safe(
            kind="verify",
            status="failed",
            s3_key=key,
            duration_ms=int((time.monotonic() - t0) * 1000),
            error=message,
        )
        return False, message
    message = f"Copia verificada: {key} descifra y es un dump de PostgreSQL"
    await _record_run_safe(
        kind="verify",
        status="ok",
        s3_key=key,
        size_bytes=int(latest["size"] or 0),
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    logger.info("backups.verified", key=key)
    return True, message


def _decrypt_first_stream_chunk(head: bytes) -> bytes:
    """Descifra SOLO el primer bloque de una copia EKB2 (descarga por rango)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    start = len(MAGIC_STREAM) + STREAM_NONCE_PREFIX_LEN
    base_nonce = head[len(MAGIC_STREAM) : start]
    flag = head[start]
    length = int.from_bytes(head[start + 1 : start + STREAM_HEADER_LEN], "big")
    ct = head[start + STREAM_HEADER_LEN : start + STREAM_HEADER_LEN + length]
    if len(ct) != length:
        raise ValueError("No se pudo leer el primer bloque completo")
    return AESGCM(_derive_backup_key()).decrypt(
        _stream_nonce(base_nonce, 0), ct, _stream_aad(0, flag)
    )


async def check_bucket_connection() -> tuple[bool, str]:
    """Prueba diaria de conexión al bucket. Solo registra y avisa si FALLA.

    Sin esto, un token de Cloudflare caducado no se descubría hasta que la
    copia de esa noche fallaba (o peor: hasta el día de la restauración).
    """
    try:
        if await get_s3_config() is None:
            # Sin bucket no hay nada que probar (y no es un fallo).
            return True, "Sin bucket configurado"
    except Exception:  # noqa: BLE001 — credenciales ilegibles: eso SÍ es fallo
        pass
    ok, message = await test_connection()
    if not ok:
        await _record_run_safe(
            kind="check", status="failed", error=f"Conexión con el bucket FALLIDA: {message[:250]}"
        )
    return ok, message


async def list_remote_backups() -> list[dict]:
    cfg = await get_s3_config()
    if cfg is None:
        return []
    objects = await asyncio.to_thread(_sync_list, cfg)
    return [o for o in objects if parse_key_datetime(o["key"]) is not None]


async def download_and_decrypt(key: str) -> bytes:
    """Descarga un objeto chatbot/*.dump.enc y devuelve el .dump en claro."""
    if not key.startswith(APP_PREFIX) or parse_key_datetime(key) is None:
        raise ValueError("Clave de backup no válida")
    cfg = await get_s3_config()
    if cfg is None:
        raise RuntimeError("Proveedor S3 no configurado")
    blob = await asyncio.to_thread(_sync_download, cfg, key)
    return await asyncio.to_thread(decrypt_backup_bytes, blob)


# ---------------------------------------------------------------------------
# Restore (pg_restore del contenedor, --single-transaction = todo-o-nada)
# ---------------------------------------------------------------------------


async def restore_backup(key: str) -> None:
    """Descarga → descifra → pg_restore --clean --single-transaction.

    PISA los datos actuales (el endpoint exige doble confirmación explícita).
    --single-transaction: si algo falla a mitad, rollback y la BD queda como
    estaba. La password va SOLO en PGPASSWORD (env), nunca en argv.
    """
    from app.tasks.backup_db import parse_database_url

    started = time.monotonic()
    dump = await download_and_decrypt(key)
    db = parse_database_url(settings.DATABASE_URL)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="restore_", suffix=".dump", delete=False
        ) as tmp:
            tmp.write(dump)
            tmp_path = Path(tmp.name)
        cmd = [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "--no-password",
            "-h", db["host"],
            "-p", str(db["port"]),
            "-U", db["user"],
            "-d", db["dbname"],
            str(tmp_path),
        ]
        env = {**os.environ, "PGPASSWORD": db["password"]}
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=PG_RESTORE_TIMEOUT_SECS,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pg_restore rc={proc.returncode}: {(proc.stderr or '').strip()[-400:]}"
            )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    duration_ms = int((time.monotonic() - started) * 1000)
    await record_run(
        kind="restore", status="ok", s3_key=key, size_bytes=len(dump), duration_ms=duration_ms
    )
    # La BD restaurada puede traer credenciales distintas a las cacheadas.
    try:
        from app.services.credentials import invalidate_credential_cache

        await invalidate_credential_cache(None)
    except Exception:
        logger.warning("backups.restore.cache_invalidation_failed")
    logger.info("backups.restored", key=key, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Estado para la UI
# ---------------------------------------------------------------------------


async def get_overview() -> dict:
    """Todo lo que la tarjeta de estado necesita en una llamada."""
    row = await get_settings_row()
    cfg = await get_s3_config()
    last = await last_copy_run()
    last_ok_at = await last_successful_backup_at()
    frequency = row.frequency if row else "daily"
    daily_hour = row.daily_hour if row else 4
    next_run = compute_next_run(
        frequency, daily_hour, row.last_scheduled_at if row else None
    )
    destination = row.destination if row else None
    return {
        "enabled": row.enabled if row else True,
        "frequency": frequency,
        "daily_hour": daily_hour,
        "provider": row.provider if row else "r2",
        "s3_configured": cfg is not None,
        # Elegido (NULL = aún sin elegir) + efectivo (lo que se hará de verdad).
        "destination": destination,
        "effective_destination": resolve_destination(destination, cfg is not None),
        "next_run_at": next_run.isoformat() if (row and row.enabled) else None,
        # Última copia CORRECTA: lo que de verdad dice si estás protegido.
        "last_successful_at": last_ok_at.isoformat() if last_ok_at else None,
        "last_run": {
            "status": last.status,
            "kind": last.kind,
            "s3_key": last.s3_key,
            "size_bytes": last.size_bytes,
            "duration_ms": last.duration_ms,
            "error": last.error,
            "created_at": last.created_at.isoformat(),
            "local_file": last.local_file,
            "upload_status": last.upload_status,
        }
        if last
        else None,
    }


async def update_settings(
    *,
    enabled: bool | None = None,
    frequency: str | None = None,
    daily_hour: int | None = None,
    provider: str | None = None,
    destination: str | None = None,
) -> BackupSettings:
    async with db_session() as db:
        row = (await db.execute(select(BackupSettings).limit(1))).scalar_one_or_none()
        if row is None:
            row = BackupSettings()
            db.add(row)
        if enabled is not None:
            row.enabled = enabled
        if frequency is not None:
            if frequency not in BACKUP_FREQUENCIES:
                raise ValueError(f"Frecuencia no válida: {frequency}")
            row.frequency = frequency
        if daily_hour is not None:
            if not 0 <= daily_hour <= 23:
                raise ValueError("La hora debe estar entre 0 y 23")
            row.daily_hour = daily_hour
        if provider is not None:
            row.provider = provider
        if destination is not None:
            if destination not in BACKUP_DESTINATIONS:
                raise ValueError(f"Destino no válido: {destination}")
            row.destination = destination
        await db.commit()
        await db.refresh(row)
        return row
