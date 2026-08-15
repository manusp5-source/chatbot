"""Tarea Celery que envía un Web Push a todas las suscripciones.

pywebpush es bloqueante (HTTP sync), así que el envío se hace fuera de la
petición / del hilo del agente. `send_to_all` ya paraleliza por suscripción con
`asyncio.to_thread` y limpia las suscripciones expiradas.
"""
import asyncio

from celery import shared_task

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(name="app.tasks.web_push_send.send_web_push", bind=True, max_retries=2)
def send_web_push(self, title: str, body: str, url: str | None = None) -> None:
    from app.services.web_push import send_to_all

    try:
        asyncio.run(send_to_all(title, body, url))
    except Exception as exc:
        logger.warning("web_push_send.error", error=str(exc))
        # Reintento suave; si sigue fallando, lo dejamos (best-effort).
        raise self.retry(exc=exc, countdown=10)
