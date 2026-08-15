"""Task que ejecuta el procesamiento del agente UNA vez por rafaga.

Sustituye al patron viejo `asyncio.sleep(buffer_seconds)` dentro de
`process_message`, que bloqueaba el thread del worker N segundos enteros.

Ahora `process_message` solo encola el mensaje en el buffer y agenda esta
task con `countdown=buffer_seconds`. Cuando se ejecuta:
  - Si han pasado N segundos desde el ultimo mensaje del phone -> drena y procesa.
  - Si no (entrego otro mensaje en medio) -> sale silenciosamente, otro drain
    posterior se encargara.

Resultado: los threads del worker quedan libres para procesar OTRAS
conversaciones mientras esperan al buffer de una concreta.
"""
import asyncio

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(name="app.tasks.drain_buffer.drain_buffer", bind=True, max_retries=3)
def drain_buffer(self, phone: str) -> None:
    from app.services.conversation import process_buffered_messages

    try:
        asyncio.run(process_buffered_messages(phone))
    except Exception as exc:
        logger.error("drain_buffer.error", phone="<PHONE>", error=str(exc))
        # El reintento solo sirve para lo que falla ANTES de drenar la cola
        # (config, BD…): si la cola ya se vació, la reejecución no encuentra
        # mensajes y sale en silencio. Los fallos posteriores (modelo, envío)
        # los resuelve `process_buffered_messages` derivando a humano, así que
        # aquí no deberían llegar.
        try:
            raise self.retry(exc=exc, countdown=2 ** self.request.retries * 5)
        except MaxRetriesExceededError:
            # Sin manejador global de fallos de Celery, esto era un log perdido
            # dentro del contenedor. Que quede en Monitorización.
            _log_visible_failure(phone, exc)
            raise


def _channel_label(buffer_key: str) -> str | None:
    """Canal deducible de la clave del buffer, o None si no se puede.

    Las claves nuevas son "conv:<conversation_id>" (por conversación) y no
    llevan el canal dentro. Las heredadas son el identificador del contacto y sí
    lo delatan por el prefijo.
    """
    if buffer_key.startswith("conv:"):
        return None
    if buffer_key.startswith(("ig:", "web:", "email:")):
        return buffer_key.split(":", 1)[0]
    return "whatsapp"


def _log_visible_failure(phone: str, exc: Exception) -> None:
    """Deja el fallo definitivo del drain en los logs en vivo del panel."""
    from app.services.runtime_logs import push_runtime_log

    try:
        asyncio.run(
            push_runtime_log(
                level="error",
                event="drain_buffer.failed",
                message=(
                    "No se pudo procesar la ráfaga de mensajes de un contacto "
                    "tras agotar los reintentos; revisa la conversación a mano."
                ),
                # `phone` es la clave del buffer. Desde el arreglo del buffer por
                # conversación llega como "conv:<id>", que no dice el canal: en
                # ese caso no ponemos ninguno en vez de inventar "conv".
                channel=_channel_label(phone),
                error=str(exc)[:200],
            )
        )
    except Exception as e:  # noqa: BLE001 — el log nunca puede romper la task
        logger.warning("drain_buffer.log_failed", error=str(e))
