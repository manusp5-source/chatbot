"""Endpoints admin de Copias de seguridad (sección propia del panel).

Nombres de UI estables:
  - Estado siempre visible (última copia + próxima programada) → GET /.
  - Configuración (frecuencia, hora de la diaria, proveedor) → PUT /settings.
    Se guarda en BD (backup_settings) como el resto de settings del panel.
  - Credenciales del proveedor: claves backup_s3_* de /admin/credentials
    (aquí solo se EXPONE su estado; el guardado va por el endpoint estándar
    de credenciales, con su cifrado y su auditoría).
  - "Probar conexión" (escribe+borra un objeto de prueba en chatbot/).
  - "Hacer copia ahora" (encola la task Celery backup_db kind=manual).
  - "Descargar" (descarga del bucket + descifrado en el server → .dump).
  - "Restaurar…" con confirmación explícita: PISA los datos actuales, exige
    confirm="RESTAURAR" además de la doble confirmación de la UI.

Todo admin-only (require_admin) y con audit log en las acciones que mutan.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.backup import BACKUP_DESTINATIONS, BACKUP_FREQUENCIES, BACKUP_PROVIDERS
from app.models.user import User
from app.services.audit import record_audit
from app.services.backups import (
    APP_PREFIX,
    CRED_ACCESS_KEY,
    CRED_BUCKET,
    CRED_ENDPOINT,
    CRED_SECRET_KEY,
    download_and_decrypt,
    get_overview,
    list_remote_backups,
    list_runs,
    parse_key_datetime,
    test_connection,
    update_settings,
    verify_last_backup,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/backups", tags=["admin-backups"])

# Frase de confirmación del restore (la UI la pide tecleada, doble candado).
RESTORE_CONFIRM_PHRASE = "RESTAURAR"


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


class BackupRunOut(BaseModel):
    id: str
    kind: str
    status: str
    s3_key: str | None
    size_bytes: int | None
    duration_ms: int | None
    error: str | None
    created_at: str
    # Destino de la copia (claridad de "dónde quedó"): fichero en el disco del
    # servidor + estado de la subida al bucket (uploaded/failed/not_configured).
    local_file: str | None = None
    upload_status: str | None = None


class BackupCopyOut(BaseModel):
    key: str
    size: int
    last_modified: str | None


class BackupOverviewOut(BaseModel):
    enabled: bool
    frequency: str
    daily_hour: int
    provider: str
    s3_configured: bool
    # Destino elegido en el selector (cloud/server/both; None = aún sin
    # elegir) y destino EFECTIVO (lo que hará de verdad la próxima copia).
    destination: str | None
    effective_destination: str
    next_run_at: str | None
    # Última copia CORRECTA (la de "hace X días" del panel): si la de hoy
    # falló, esto es lo que dice de verdad cuánto estás expuesto.
    last_successful_at: str | None = None
    last_run: BackupRunOut | None
    # Estado de las credenciales (el guardado va por /admin/credentials/{key}).
    endpoint: str
    bucket: str
    access_key_masked: str
    secret_set: bool


class BackupSettingsUpdate(BaseModel):
    enabled: bool | None = None
    frequency: str | None = None
    daily_hour: int | None = None
    provider: str | None = None
    destination: str | None = None


class RestoreRequest(BaseModel):
    key: str
    confirm: str


@router.get("", response_model=BackupOverviewOut)
async def backups_overview(_: User = Depends(require_admin)) -> BackupOverviewOut:
    from app.services.credentials import CredentialUnavailableError, get_credential

    try:
        data = await get_overview()
    except CredentialUnavailableError as e:
        # Credenciales guardadas que no descifran: mejor un error explícito en
        # el panel que una tarjeta diciendo "sin bucket configurado" mientras
        # las copias salen en verde sin subir nada.
        logger.error("backups.overview_credentials_unreadable", error=str(e)[:300])
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Las credenciales del bucket están guardadas pero no se pueden "
            "descifrar (¿ENCRYPTION_KEY cambiada?). Vuelve a guardarlas: hasta "
            "entonces las copias NO se están subiendo al bucket.",
        ) from e
    endpoint = await get_credential(CRED_ENDPOINT) or ""
    bucket = await get_credential(CRED_BUCKET) or ""
    access_key = await get_credential(CRED_ACCESS_KEY) or ""
    secret = await get_credential(CRED_SECRET_KEY) or ""
    last = data.pop("last_run")
    return BackupOverviewOut(
        **data,
        last_run=BackupRunOut(id="", **last) if last else None,
        endpoint=endpoint,
        bucket=bucket,
        access_key_masked=_mask(access_key),
        secret_set=bool(secret),
    )


@router.put("/settings", response_model=BackupOverviewOut)
async def backups_update_settings(
    payload: BackupSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> BackupOverviewOut:
    if payload.frequency is not None and payload.frequency not in BACKUP_FREQUENCIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Frecuencia no válida (usa {', '.join(BACKUP_FREQUENCIES)})",
        )
    if payload.provider is not None and payload.provider not in BACKUP_PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Proveedor no válido")
    if payload.destination is not None and payload.destination not in BACKUP_DESTINATIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Destino no válido (usa {', '.join(BACKUP_DESTINATIONS)})",
        )
    if payload.daily_hour is not None and not 0 <= payload.daily_hour <= 23:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "La hora debe estar entre 0 y 23")
    try:
        row = await update_settings(
            enabled=payload.enabled,
            frequency=payload.frequency,
            daily_hour=payload.daily_hour,
            provider=payload.provider,
            destination=payload.destination,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await record_audit(
        db,
        user_id=user.id,
        action="backup_settings.updated",
        entity="backup_settings",
        entity_id=row.id,
        after=payload.model_dump(exclude_none=True),
    )
    await db.commit()
    return await backups_overview(user)


@router.post("/test-connection")
async def backups_test_connection(_: User = Depends(require_admin)) -> dict:
    ok, message = await test_connection()
    return {"ok": ok, "message": message}


@router.post("/verify")
async def backups_verify(_: User = Depends(require_admin)) -> dict:
    """Comprueba que la última copia del bucket SIRVE: la descarga, la descifra
    y mira que sea un dump de PostgreSQL. Queda en el histórico (kind=verify).

    Es la única prueba real de que la copia vale: que el objeto exista y pese
    algo no garantiza que se pueda restaurar.
    """
    ok, message = await verify_last_backup()
    return {"ok": ok, "message": message}


@router.get("/copies", response_model=list[BackupCopyOut])
async def backups_list_copies(_: User = Depends(require_admin)) -> list[BackupCopyOut]:
    try:
        objects = await list_remote_backups()
    except Exception as e:
        logger.warning("backups.list_copies_failed", error=str(e)[:200])
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"No se pudo listar el bucket: {str(e)[:150]}"
        ) from e
    return [
        BackupCopyOut(
            key=o["key"],
            size=o["size"],
            last_modified=o["last_modified"].isoformat() if o["last_modified"] else None,
        )
        for o in objects
    ]


@router.get("/runs", response_model=list[BackupRunOut])
async def backups_list_runs(_: User = Depends(require_admin)) -> list[BackupRunOut]:
    runs = await list_runs(limit=30)
    return [
        BackupRunOut(
            id=str(r.id),
            kind=r.kind,
            status=r.status,
            s3_key=r.s3_key,
            size_bytes=r.size_bytes,
            duration_ms=r.duration_ms,
            error=r.error,
            created_at=r.created_at.isoformat(),
            local_file=r.local_file,
            upload_status=r.upload_status,
        )
        for r in runs
    ]


@router.post("/run-now")
async def backups_run_now(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    """Encola una copia manual en el worker (misma task que las programadas)."""
    from app.tasks.backup_db import backup_db as backup_db_task

    backup_db_task.delay(kind="manual")
    await record_audit(
        db, user_id=user.id, action="backup.run_now", entity="backup_run"
    )
    await db.commit()
    return {"queued": True}


@router.get("/download")
async def backups_download(
    key: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> Response:
    """Descarga una copia del bucket, la DESCIFRA en el server y devuelve el
    .dump en claro (formato custom de pg_dump, restaurable con pg_restore)."""
    if not key.startswith(APP_PREFIX) or parse_key_datetime(key) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Clave de copia no válida")
    try:
        dump = await download_and_decrypt(key)
    except Exception as e:
        logger.warning("backups.download_failed", key=key, error=str(e)[:200])
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"No se pudo descargar/descifrar: {str(e)[:150]}"
        ) from e
    await record_audit(
        db, user_id=user.id, action="backup.downloaded", entity="backup_run", after={"key": key}
    )
    await db.commit()
    # chatbot/2026-07-14_0400.dump.enc → chatbot_2026-07-14_0400.dump
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", key.removesuffix(".enc"))
    return Response(
        content=dump,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/restore")
async def backups_restore(
    payload: RestoreRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    """Restaura una copia. PISA los datos actuales de la BD.

    Doble candado: la UI pide doble confirmación explícita Y aquí se exige la
    frase exacta. El trabajo corre en el worker (pg_restore --clean
    --single-transaction: si falla a mitad, rollback). El resultado queda en
    el registro de intentos (kind=restore).
    """
    if payload.confirm != RESTORE_CONFIRM_PHRASE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f'Confirmación incorrecta: escribe "{RESTORE_CONFIRM_PHRASE}"',
        )
    if not payload.key.startswith(APP_PREFIX) or parse_key_datetime(payload.key) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Clave de copia no válida")
    from app.tasks.backup_db import restore_db as restore_db_task

    restore_db_task.delay(key=payload.key)
    await record_audit(
        db,
        user_id=user.id,
        action="backup.restore_requested",
        entity="backup_run",
        after={"key": payload.key},
    )
    await db.commit()
    return {"queued": True}
