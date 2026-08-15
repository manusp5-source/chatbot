"""Tarea Celery periódica que sincroniza el correo entrante de Gmail (F5a).

Corre cada 2 minutos (ver beat_schedule en app/tasks/__init__.py). Delega toda
la lógica en `services/gmail_ingest.sync_incoming_emails`; este fichero solo
hace de molde Celery (asyncio.run + manejo de errores), igual que
`process_message.py`.

GUARD 5a: `sync_incoming_emails` SOLO almacena los correos (no encola el agente).
El agente no responde correos en 5a. 5b/5c activarán el runtime del agente.
"""
import asyncio

from celery import shared_task

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(name="app.tasks.poll_gmail.poll_gmail", bind=True, max_retries=2)
def poll_gmail(self) -> None:
    from app.services.gmail_ingest import sync_incoming_emails

    try:
        result = asyncio.run(sync_incoming_emails())
        logger.info("poll_gmail.done", **(result or {}))
    except Exception as exc:
        logger.error("poll_gmail.error", error=str(exc))
        # Reintento suave: el siguiente beat (2 min) reintenta igualmente, así
        # que no hace falta una cascada larga de retries.
        raise self.retry(exc=exc, countdown=30)
