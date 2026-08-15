"""Tarea Celery periódica de retención del canal Email (minimización).

Corre una vez al día (ver beat_schedule en app/tasks/__init__.py). Delega toda
la lógica en `services/gmail_retention.purge_old_emails`; este fichero solo hace
de molde Celery (asyncio.run + manejo de errores), igual que `poll_gmail.py`.

Purga el CONTENIDO de los correos (canal email) más viejos que
EMAIL_RETENTION_MONTHS, conservando una entrada ligera para el histórico y la
recuperación bajo demanda desde Gmail. Es idempotente.
"""
import asyncio

from celery import shared_task

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(name="app.tasks.purge_emails.purge_emails", bind=True, max_retries=2)
def purge_emails(self) -> None:
    from app.services.gmail_retention import purge_old_emails

    try:
        result = asyncio.run(purge_old_emails())
        logger.info("purge_emails.done", **(result or {}))
    except Exception as exc:
        logger.error("purge_emails.error", error=str(exc))
        # Reintento suave: la retención no es urgente; el siguiente beat (mañana)
        # reintenta igualmente. Un par de retries cortos por si fue un fallo
        # transitorio de BD.
        raise self.retry(exc=exc, countdown=60)
