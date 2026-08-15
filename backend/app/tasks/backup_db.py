"""Backup de Postgres (pg_dump) a disco persistente + bucket S3 cifrado.

Dos tareas:

- `backup_db`: hace el backup completo. Dump custom comprimido a
  settings.BACKUP_DIR (retención local BACKUP_RETENTION_DAYS) y después,
  según el DESTINO elegido en el panel (cloud/server/both, ver
  services/backups.resolve_destination): con destino bucket lo cifra
  (AES-256-GCM, services/backups.py) y lo sube a chatbot/<fecha>.dump.enc con
  retención remota (7 diarias + 4 semanales + recientes según frecuencia);
  con destino "cloud" el dump local se borra tras subida OK (si falla se
  conserva); con destino "server" no se sube nada. Cada intento queda
  registrado en `backup_runs` (visible en la UI).
- `backup_tick`: la dispara el beat cada 10 min; consulta la configuración de
  la BD (frecuencia cada hora/6h/12h/diaria/semanal/mensual + hora de las
  copias "de días", editable en el panel) y encola `backup_db` cuando toca.
  El sello last_scheduled_at se escribe ANTES de encolar → no-solape aunque
  el tick se repita. Además dispara una vez al día (candado en Redis) los
  chequeos: conexión al bucket + verificación real de la última copia.

Detalles del dump local:
  - Destino: settings.BACKUP_DIR (/data/audios/backups). Comparte el volumen
    de audios porque es el ÚNICO mount persistente de app/worker — fuera de
    /data/audios todo se pierde al recrear el contenedor.
  - Retención local: por días (BACKUP_RETENTION_DAYS) y por número de ficheros
    (BACKUP_RETENTION_MAX_FILES). Corre ANTES del guardia de disco: si lo que
    llenó el disco son dumps viejos, se limpian antes de decidir que no cabe.
  - Guardia de disco: si quedan menos de BACKUP_MIN_FREE_GB libres NO se
    escribe el dump a fichero (un disco lleno tumba la BD) y se alerta. Pero
    el guardia NO bloquea la subida: con destino bucket la copia se hace en
    streaming y sin fichero local (ver services/backups.stream_dump_to_bucket).
  - La password va SOLO en PGPASSWORD (env del subprocess): nunca en argv
    (visible en /proc) ni en logs/alertas.

Qué hace cada combinación de destino y espacio en disco:
  - server + disco OK      → dump a fichero, sin subir.
  - server + sin disco     → se avisa y se sale (sin destino remoto no hay
                             nada que salvar).
  - cloud  + disco OK      → copia DIRECTA al bucket, sin fichero local (es lo
                             que promete la propia pantalla del panel).
  - cloud  + sin disco     → igual, y además se alerta del disco.
  - both   + disco OK      → dump a fichero + subida desde el fichero.
  - both   + sin disco     → copia directa al bucket, marcada como degradada.

Requiere pg_dump >= la versión del server en la imagen (postgresql-client-17
vía repo PGDG, ver backend/Dockerfile): pg_dump ABORTA si su versión es menor
que la del server — la BD de producción es pgvector/pg17. Runbook de
restauración: docs/backups.md (o botón "Restaurar…" del panel).

Mismo molde Celery que purge_emails.py / purge_internal_logs.py, con la
diferencia de que el trabajo central (subprocess) es síncrono; asyncio.run se
usa solo para BD/S3/notificaciones best-effort.
"""
import asyncio
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from celery import shared_task

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# pg_dump no debería tardar ni minutos con esta BD; 15 min de techo evita que
# un cuelgue de red deje la task colgada para siempre.
PG_DUMP_TIMEOUT_SECS = 900
# Un dump custom real de esta BD nunca baja de 1KB: por debajo es un fichero
# parcial/corrupto (p.ej. pg_dump matado nada más arrancar).
MIN_DUMP_BYTES = 1024

# Patrón HISTÓRICO de nombres de dump. La retención lo sigue limpiando aunque
# el prefijo actual sea otro, para que los dumps antiguos de instalaciones
# existentes no queden huérfanos (y sin borrar) tras cambiar el prefijo.
LEGACY_DUMP_GLOB = "chatbot_*.dump"


def dump_prefix() -> str:
    """Prefijo de los ficheros de dump (<prefijo>_<fecha>.dump).

    BACKUP_FILE_PREFIX si está definido; si no, el nombre de la BD de
    DATABASE_URL (cada instalación hereda su propio nombre sin configurar
    nada). "backup" como último recurso si la URL no trae nombre de BD.
    """
    if settings.BACKUP_FILE_PREFIX:
        return settings.BACKUP_FILE_PREFIX
    return parse_database_url(settings.DATABASE_URL)["dbname"] or "backup"


def parse_database_url(url: str) -> dict:
    """Extrae host/port/user/password/dbname de una URL SQLAlchemy.

    Acepta el sufijo de driver (postgresql+asyncpg://, +psycopg2) y
    percent-encoding en user/password (SQLAlchemy codifica así los caracteres
    especiales; urlparse NO los decodifica solo → unquote explícito).
    """
    clean = url.replace("+asyncpg", "").replace("+psycopg2", "")
    parsed = urlparse(clean)
    return {
        "host": parsed.hostname or "db",
        "port": parsed.port or 5432,
        "user": unquote(parsed.username) if parsed.username else "",
        "password": unquote(parsed.password) if parsed.password else "",
        "dbname": (parsed.path or "").lstrip("/"),
    }


def has_enough_disk(backup_dir: Path, min_free_gb: float) -> tuple[bool, float]:
    """(¿hay al menos min_free_gb libres en el disco de backup_dir?, GB libres)."""
    free_gb = shutil.disk_usage(backup_dir).free / (1024**3)
    return free_gb >= min_free_gb, free_gb


def _pg_dump_common_args(db: dict) -> list[str]:
    """Argv común de pg_dump (sin destino). La password NUNCA va aquí: viaja
    en PGPASSWORD (env).

    --no-password: jamás pedir password interactiva (worker sin TTY); si
    PGPASSWORD falta o es errónea, falla rápido y limpio.
    """
    return [
        "pg_dump",
        "--format=custom",
        "--compress=6",
        "--no-password",
        "-h", db["host"],
        "-p", str(db["port"]),
        "-U", db["user"],
        "-d", db["dbname"],
    ]


def build_pg_dump_command(db: dict, out_path: Path) -> list[str]:
    """pg_dump a FICHERO (camino clásico: hace falta espacio en disco)."""
    return [*_pg_dump_common_args(db), "-f", str(out_path)]


def build_pg_dump_stdout_command(db: dict) -> list[str]:
    """pg_dump a la SALIDA ESTÁNDAR (copia directa al bucket, sin disco).

    Sin `-f`, pg_dump escribe el dump por stdout: es lo que permite cifrarlo
    por trozos y subirlo mientras se genera, sin fichero intermedio.
    """
    return _pg_dump_common_args(db)


def apply_retention(
    backup_dir: Path,
    retention_days: int,
    *,
    max_files: int | None = None,
    now: float | None = None,
) -> tuple[int, int]:
    """Limpia los dumps locales por ANTIGÜEDAD y por NÚMERO.

    - Por antigüedad: mtime más viejo que retention_days.
    - Por número (max_files): conserva solo los N más recientes. Sin este tope,
      una frecuencia horaria acumula 168 dumps antes de que el primero cumpla
      7 días — el disco se llena y la retención por edad no libera ni un byte
      justo el día que hace falta. 0/None = sin tope.

    El dump MÁS RECIENTE nunca se borra, ni por edad: esta limpieza corre
    ANTES del volcado del día, y borrarlo dejaría el servidor sin ninguna copia
    si el volcado de hoy falla.

    Aplica sobre <prefijo>_*.dump Y sobre el patrón histórico chatbot_*.dump
    (compatibilidad: los dumps hechos antes del prefijo configurable se siguen
    purgando). Otros ficheros del directorio no se tocan.

    Devuelve (kept, deleted). Best-effort: un fallo al borrar un fichero no
    rompe el backup (se loggea y se cuenta como conservado).
    """
    now_ts = time.time() if now is None else now
    cutoff = now_ts - retention_days * 86400
    kept = 0
    deleted = 0
    candidates = set(backup_dir.glob(f"{dump_prefix()}_*.dump")) | set(
        backup_dir.glob(LEGACY_DUMP_GLOB)
    )
    # Orden por fecha de modificación, del más nuevo al más viejo: así se sabe
    # cuál es el intocable y cuáles sobran por número.
    with_mtime: list[tuple[float, Path]] = []
    for f in sorted(candidates):
        try:
            with_mtime.append((f.stat().st_mtime, f))
        except OSError as exc:
            logger.warning("backup.retention_error", file=f.name, error=str(exc))
            kept += 1
    with_mtime.sort(key=lambda t: t[0], reverse=True)

    for idx, (mtime, f) in enumerate(with_mtime):
        too_old = mtime < cutoff
        too_many = bool(max_files) and idx >= max_files  # type: ignore[operator]
        # idx == 0 → el más reciente, nunca se toca.
        if idx > 0 and (too_old or too_many):
            try:
                f.unlink()
                deleted += 1
                continue
            except OSError as exc:
                logger.warning("backup.retention_error", file=f.name, error=str(exc))
        kept += 1
    return kept, deleted


async def _alert_async(
    *, level: str, event: str, message: str, kind: str, details: dict, push: bool
) -> None:
    from app.services.runtime_logs import push_runtime_log
    from app.services.security_alerts import notify_security

    await push_runtime_log(level=level, event=event, message=message, **details)
    await notify_security(kind=kind, title=message, details=details)
    if push:
        # Web Push a la PWA del operador. El buffer de Monitorización son 1000
        # líneas en Redis que se pierden al reiniciar: un backup fallido no
        # puede depender de que alguien mire el panel ese día.
        from app.services.web_push import notify

        await notify("Copias de seguridad", message[:180], "/admin/backups")


def _alert(
    *, level: str, event: str, message: str, kind: str, details: dict, push: bool = False
) -> None:
    """Runtime log + alerta de seguridad (+ Web Push si `push`), best-effort:
    si Redis/el push caen, el backup sigue su curso (el structlog local ya
    quedó escrito)."""
    try:
        asyncio.run(
            _alert_async(
                level=level,
                event=event,
                message=message,
                kind=kind,
                details=details,
                push=push,
            )
        )
    except Exception as exc:
        logger.warning("backup.alert_failed", error=str(exc))


def _record_run_safe(**kwargs) -> None:
    """Registra el intento en backup_runs, best-effort: si la BD no está
    disponible (tests eager, BD caída a mitad de incidente) el backup no se
    rompe por el registro — el structlog local ya cuenta la historia."""
    try:
        from app.services.backups import record_run

        asyncio.run(record_run(**kwargs))
    except Exception as exc:
        logger.warning("backup.record_run_failed", error=str(exc)[:200])


def _effective_destination() -> str:
    """Destino elegido en el panel (cloud/server/both), resuelto contra la BD.

    Fail-safe si la BD/credenciales no responden: "both" — conserva el dump
    local Y intenta subir; nunca borra ni deja de subir por un fallo de
    lectura de configuración."""
    try:
        from app.services.backups import get_effective_destination

        return asyncio.run(get_effective_destination())
    except Exception as exc:
        logger.warning("backup.destination_lookup_failed", error=str(exc)[:200])
        return "both"


def _post_dump_s3(
    out_path: Path, *, kind: str, dump_duration_ms: int, destination: str
) -> None:
    """Cifra y sube el dump al bucket (si está configurado y el destino lo
    pide) + registra el intento. Un fallo aquí NO invalida el dump local
    recién hecho: alerta (backup_upload_failed) pero la task termina bien."""
    try:
        from app.services.backups import process_dump

        asyncio.run(
            process_dump(
                out_path,
                kind=kind,
                dump_duration_ms=dump_duration_ms,
                destination=destination,
            )
        )
    except Exception as exc:
        details = {"file": out_path.name, "error": str(exc)[:300]}
        logger.error("backup.upload_failed", **details)
        _alert(
            level="error",
            event="backup.upload_failed",
            message="Backup de BD: subida al bucket FALLIDA (dump local OK)",
            kind="backup_upload_failed",
            details=details,
            push=True,
        )


DAILY_CHECKS_CLAIM_KEY = "backup:daily_checks"


async def _claim_daily_checks() -> bool:
    """Candado de 24 h en Redis: los chequeos diarios corren UNA vez al día.

    Va aquí y no en el beat_schedule a propósito: el tick ya pasa cada 10 min,
    así que no hace falta tocar la configuración del beat (ni coordinar un
    redespliegue) para tener chequeos diarios. Si Redis se vacía, como mucho se
    repiten: verificar una copia dos veces no rompe nada.
    """
    from app.core.redis import get_redis

    return bool(await get_redis().set(DAILY_CHECKS_CLAIM_KEY, "1", ex=86400, nx=True))


async def _run_daily_checks() -> None:
    """Prueba de conexión al bucket + verificación real de la última copia."""
    from app.services.backups import check_bucket_connection, verify_last_backup

    ok, message = await check_bucket_connection()
    if not ok:
        details = {"error": message[:300]}
        logger.error("backup.bucket_check_failed", **details)
        await _alert_async(
            level="error",
            event="backup.bucket_check_failed",
            message=f"Copias: no se puede conectar con el bucket ({message[:150]})",
            kind="backup_bucket_check_failed",
            details=details,
            push=True,
        )
        return  # sin conexión, verificar la copia solo daría un segundo error
    ok, message = await verify_last_backup()
    if not ok:
        details = {"error": message[:300]}
        logger.error("backup.verify_failed", **details)
        await _alert_async(
            level="error",
            event="backup.verify_failed",
            message=f"Copias: la última copia del bucket NO es restaurable ({message[:150]})",
            kind="backup_verify_failed",
            details=details,
            push=True,
        )


@shared_task(name="app.tasks.backup_db.backup_tick", bind=True, max_retries=0)
def backup_tick(self) -> None:
    """Scheduler configurable: el beat lo dispara cada 10 min y aquí se decide
    (contra backup_settings en BD) si toca copia. Así la frecuencia/hora se
    cambian desde el panel sin tocar el beat_schedule ni redesplegar.

    Aprovecha el mismo tick para los chequeos diarios (conexión al bucket +
    verificación de la última copia), con candado en Redis."""
    if settings.BACKUP_DAILY_CHECKS:
        try:
            if asyncio.run(_claim_daily_checks()):
                asyncio.run(_run_daily_checks())
        except Exception as exc:
            # Best-effort: los chequeos jamás impiden la copia.
            logger.warning("backup.daily_checks_failed", error=str(exc)[:200])
    try:
        from app.services.backups import claim_scheduled_slot

        due = asyncio.run(claim_scheduled_slot())
    except Exception as exc:
        # Sin BD no hay decisión; el siguiente tick lo reintenta.
        logger.warning("backup.tick_failed", error=str(exc)[:200])
        return
    if due:
        backup_db.delay(kind="scheduled")


@shared_task(name="app.tasks.backup_db.restore_db", bind=True, max_retries=0)
def restore_db(self, key: str) -> None:
    """Restaura una copia del bucket (la pide el endpoint admin con doble
    confirmación). pg_restore --clean --single-transaction: PISA los datos
    actuales; si falla a mitad hay rollback y la BD queda como estaba."""
    from app.services.backups import restore_backup

    try:
        asyncio.run(restore_backup(key))
    except Exception as exc:
        details = {"key": key, "error": str(exc)[:300]}
        logger.error("backup.restore_failed", **details)
        _record_run_safe(kind="restore", status="failed", s3_key=key, error=str(exc)[:300])
        _alert(
            level="error",
            event="backup.restore_failed",
            message="Restauración de BD FALLIDA (rollback aplicado)",
            kind="backup_restore_failed",
            details=details,
        )
        raise


def _stream_to_bucket(*, kind: str, degraded_reason: str | None):
    """Copia directa al bucket (sin fichero local). Devuelve el StreamResult.

    Fail-safe: si ni siquiera se puede llamar al servicio (BD/Redis caídos),
    se responde "no_bucket" para que el llamante siga por el camino clásico.
    """
    from app.services.backups import StreamResult, stream_dump_to_bucket

    try:
        return asyncio.run(
            stream_dump_to_bucket(kind=kind, degraded_reason=degraded_reason)
        )
    except Exception as exc:
        logger.error("backup.stream_call_failed", error=str(exc)[:300])
        return StreamResult(status="no_bucket", error=str(exc)[:300])


@shared_task(name="app.tasks.backup_db.backup_db", bind=True, max_retries=1)
def backup_db(self, kind: str = "scheduled") -> None:
    started = time.monotonic()
    backup_dir = Path(settings.BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Destino elegido en el panel (cloud/server/both). Se lee ANTES del dump:
    # decide si se sube y si el fichero local se borra tras subir.
    destination = _effective_destination()

    # Limpieza de dumps viejos ANTES del guardia de disco. Antes corría solo
    # DESPUÉS de un volcado correcto: con el disco lleno de dumps viejos, el
    # guardia abortaba, la limpieza no llegaba a correr y el disco seguía lleno
    # — el mismo círculo, todos los días, para siempre.
    kept, deleted = apply_retention(
        backup_dir,
        settings.BACKUP_RETENTION_DAYS,
        max_files=settings.BACKUP_RETENTION_MAX_FILES,
    )
    if deleted:
        logger.info("backup.retention_prepass", kept=kept, deleted=deleted)

    # Guardia de disco. SOLO gobierna la escritura local: sin espacio no se
    # puede volcar a fichero, pero eso no es motivo para no subir la copia al
    # bucket (que es justo lo que fallaba: destino "cloud" con el disco lleno
    # no hacía NADA, teniendo el bucket disponible).
    ok, free_gb = has_enough_disk(backup_dir, settings.BACKUP_MIN_FREE_GB)

    # Camino sin fichero local: siempre con destino "cloud" (la propia UI
    # promete que no deja fichero en el servidor) y como rescate cuando no hay
    # disco pero sí destino remoto.
    if destination in ("cloud", "both") and (destination == "cloud" or not ok):
        from app.services.backups import DEGRADED_NO_DISK_REASON

        result = _stream_to_bucket(
            kind=kind, degraded_reason=None if ok else DEGRADED_NO_DISK_REASON
        )
        if result.status == "done":
            if not ok:
                # Se hizo, pero el disco sigue lleno: hay que arreglarlo igual.
                logger.warning("backup.degraded_no_disk", free_gb=round(free_gb, 2))
                _alert(
                    level="warn",
                    event="backup.degraded_no_disk",
                    message=(
                        "Copia de BD hecha SOLO en el bucket: no hay espacio en el "
                        "disco del servidor (libera espacio, ver runbook)"
                    ),
                    kind="backup_degraded_no_disk",
                    details={
                        "free_gb": round(free_gb, 2),
                        "min_free_gb": settings.BACKUP_MIN_FREE_GB,
                        "destination": destination,
                    },
                    push=True,
                )
            return
        if result.status == "failed":
            _alert(
                level="error",
                event="backup.upload_failed",
                message="Backup de BD: la copia directa al bucket FALLÓ",
                kind="backup_upload_failed",
                details={"error": (result.error or "")[:300], "destination": destination},
                push=True,
            )
            if not ok:
                # Sin disco no queda plan B: ya está registrado y alertado.
                return
            # Con disco sí lo hay: seguimos por el camino clásico para que al
            # menos quede el dump en el servidor (y se reintente la subida).
        elif not ok:
            # No hay bucket usable Y no hay disco: no hay nada que hacer.
            details = {
                "free_gb": round(free_gb, 2),
                "min_free_gb": settings.BACKUP_MIN_FREE_GB,
                "backup_dir": str(backup_dir),
                "destination": destination,
                "motivo_bucket": (result.error or "")[:200],
            }
            logger.error("backup.skipped_low_disk", **details)
            _alert(
                level="error",
                event="backup.skipped_low_disk",
                message=(
                    "Backup de BD omitido: sin espacio en disco y sin bucket donde "
                    "subir la copia"
                ),
                kind="backup_skipped_low_disk",
                details=details,
                push=True,
            )
            _record_run_safe(
                kind=kind,
                status="failed",
                error=(
                    f"Omitido: poco espacio en disco ({round(free_gb, 2)} GB libres) "
                    f"y sin destino remoto usable ({(result.error or 'bucket no configurado')[:150]})"
                ),
            )
            return

    if not ok:
        # Destino "server": sin espacio no se puede escribir el dump y no hay
        # destino remoto donde salvarlo. Se avisa y se termina sin excepción
        # (reintentar no aportaría nada; el beat lo vuelve a intentar).
        details = {
            "free_gb": round(free_gb, 2),
            "min_free_gb": settings.BACKUP_MIN_FREE_GB,
            "backup_dir": str(backup_dir),
            "destination": destination,
        }
        logger.error("backup.skipped_low_disk", **details)
        _alert(
            level="error",
            event="backup.skipped_low_disk",
            message=(
                "Backup de BD omitido: poco espacio en disco (destino «solo servidor»: "
                "sin bucket no hay dónde salvar la copia)"
            ),
            kind="backup_skipped_low_disk",
            details=details,
            push=True,
        )
        _record_run_safe(
            kind=kind,
            status="failed",
            error=(
                f"Omitido: poco espacio en disco ({round(free_gb, 2)} GB libres). "
                "Con destino «solo servidor» no hay bucket donde subir la copia: "
                "libera espacio y lanza una copia a mano."
            ),
        )
        return

    db = parse_database_url(settings.DATABASE_URL)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = backup_dir / f"{dump_prefix()}_{stamp}.dump"
    cmd = build_pg_dump_command(db, out_path)
    # Password SOLO por env del subprocess: ni en argv ni en ningún log.
    env = {**os.environ, "PGPASSWORD": db["password"]}

    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=PG_DUMP_TIMEOUT_SECS
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pg_dump rc={proc.returncode}: {(proc.stderr or '').strip()[-300:]}"
            )
        if not out_path.exists() or out_path.stat().st_size <= MIN_DUMP_BYTES:
            raise RuntimeError("dump inexistente o sospechosamente pequeño (<= 1KB)")
    except Exception as exc:
        # No dejar dumps parciales/corruptos que la retención conservaría días.
        out_path.unlink(missing_ok=True)
        details = {"file": out_path.name, "error": str(exc)[:300]}
        logger.error("backup.failed", **details)
        _alert(
            level="error",
            event="backup.failed",
            message="Backup de BD FALLIDO",
            kind="backup_failed",
            details=details,
            push=True,
        )
        _record_run_safe(kind=kind, status="failed", error=str(exc)[:300])
        if destination == "both":
            # El guardia de disco mira ANTES del volcado, no durante: el disco
            # se puede llenar a mitad y tumbar pg_dump. Antes de reintentar
            # contra el mismo disco lleno, se intenta la copia directa al
            # bucket, que no necesita fichero local. (Con destino "cloud" ya se
            # intentó arriba: no se repite.)
            from app.services.backups import DEGRADED_DUMP_FAILED_REASON

            rescue = _stream_to_bucket(
                kind=kind, degraded_reason=DEGRADED_DUMP_FAILED_REASON
            )
            if rescue.status == "done":
                logger.warning("backup.rescued_to_bucket", key=rescue.key)
                return
        # 1 reintento a los 5 min por si fue transitorio (BD reiniciando, etc).
        raise self.retry(exc=exc, countdown=300)

    dump_duration_ms = int((time.monotonic() - started) * 1000)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    # Segunda pasada de retención: el dump recién hecho puede haber pasado del
    # tope de ficheros (el más reciente nunca se borra).
    kept, deleted = apply_retention(
        backup_dir,
        settings.BACKUP_RETENTION_DAYS,
        max_files=settings.BACKUP_RETENTION_MAX_FILES,
    )
    logger.info(
        "backup.done",
        file=out_path.name,
        size_mb=round(size_mb, 2),
        kept=kept,
        deleted=deleted,
    )
    # Fase S3 (cifrar + subir + retención remota + registro del intento),
    # gobernada por el destino elegido (server = no subir; cloud = subir y
    # borrar el dump local tras subida OK). Va DESPUÉS de dar por bueno el
    # dump local: un fallo de red/bucket alerta pero no tira el backup local
    # ni provoca reintento del dump.
    _post_dump_s3(
        out_path, kind=kind, dump_duration_ms=dump_duration_ms, destination=destination
    )
