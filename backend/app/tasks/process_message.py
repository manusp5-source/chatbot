"""Tarea Celery que procesa un mensaje entrante.

El detalle de orquestación está en services/conversation.py para mantener este
fichero corto y testeable. La tarea se limita a delegar y manejar reintentos.
"""
import asyncio
import uuid

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(name="app.tasks.process_message.process_message", bind=True, max_retries=3)
def process_message(self, message_id: str) -> None:
    from app.services.conversation import handle_incoming_message

    try:
        asyncio.run(handle_incoming_message(uuid.UUID(message_id)))
    except Exception as exc:
        logger.error("process_message.error", message_id=message_id, error=str(exc))
        try:
            raise self.retry(exc=exc, countdown=2 ** self.request.retries * 5)
        except MaxRetriesExceededError:
            # Se acabaron los intentos. Antes la excepción subía y moría en el
            # log del contenedor: el mensaje del cliente se quedaba sin procesar
            # y en el panel no había ni una línea que lo dijera. `drain_buffer`
            # ya hacía esto bien; esta tarea no.
            from app.services.runtime_logs import push_runtime_log

            asyncio.run(
                push_runtime_log(
                    level="error",
                    event="process_message.failed",
                    message=(
                        "Un mensaje entrante no se ha podido procesar después de "
                        "varios intentos: el cliente se queda sin respuesta "
                        f"automática. Motivo: {exc}"
                    ),
                    message_id=message_id,
                )
            )
            raise
