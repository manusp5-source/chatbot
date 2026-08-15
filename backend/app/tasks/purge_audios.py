"""Tarea Celery de retención de ficheros en disco (RGPD 5.1.e).

Corre una vez al día (ver beat_schedule en app/tasks/__init__.py). Dos pasadas:

  - Notas de voz: borra el FICHERO más antiguo que AUDIO_RETENTION_DAYS y
    conserva la transcripción (texto).
  - Adjuntos (imágenes, vídeos, documentos): borra el FICHERO más antiguo que
    MEDIA_RETENTION_DAYS y conserva la fila del mensaje, para que el histórico
    siga diciendo qué llegó y cuándo.

Molde Celery; la lógica vive en `services/data_erasure`.
"""
import asyncio

from celery import shared_task

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(name="app.tasks.purge_audios.purge_audios", bind=True, max_retries=2)
def purge_audios(self) -> None:
    from app.services.data_erasure import purge_old_audio_files, purge_old_media_files

    async def _run() -> dict:
        audio = await purge_old_audio_files()
        media = await purge_old_media_files()
        return {
            "audios": (audio or {}).get("purged", 0),
            "adjuntos": (media or {}).get("purged", 0),
            "bytes_liberados": (media or {}).get("freed_bytes", 0),
        }

    try:
        result = asyncio.run(_run())
        logger.info("purge_audios.done", **(result or {}))
    except Exception as exc:
        logger.error("purge_audios.error", error=str(exc))
        raise self.retry(exc=exc, countdown=300)
