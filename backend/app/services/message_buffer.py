"""Buffer Redis para agrupar mensajes en ráfaga de la misma CONVERSACIÓN.

Cuando llega un mensaje:
- Acumula el id del mensaje en una lista por conversación
- Programa una tarea "procesar" en N segundos
- Si llega otro mensaje antes, extiende el deadline

La forma simple: usar un sorted set con timestamp del último mensaje, y un
worker periódico que recoge las claves con deadline pasada. Aquí
implementamos algo más directo: cada llamada a `should_process` indica si han
pasado N segundos desde el último mensaje. Si NO, el worker reagenda la tarea.

La clave del buffer (`key`) es hoy `conv:<conversation_id>`. ANTES era el
identificador del contacto, y eso rompía por dos sitios:

  - Email: dos hilos distintos del mismo remitente compartían buffer, así que
    la pregunta por una factura y un pedido nuevo se fundían en un solo
    borrador que iba a UNO de los dos hilos; el otro se quedaba sin respuesta
    para siempre.
  - WhatsApp/Instagram: el identificador del contacto se puede REESCRIBIR
    (Meta revela el teléfono de quien entró solo con nombre de usuario). El
    drenado buscaba entonces un contacto que ya no existía con ese nombre y
    tiraba mensajes ya sacados de la cola.

El identificador de conversación no cambia nunca. Las funciones siguen
aceptando cualquier cadena como clave para no romper lo que ya esté encolado en
Redis en el momento del despliegue (ver `process_buffered_messages`).
"""
import time
import uuid

from app.core.config import settings
from app.core.redis import get_redis

KEY_LAST = "msg_buffer:last:{phone}"  # timestamp del último mensaje
KEY_QUEUE = "msg_buffer:queue:{phone}"  # lista de message_ids pendientes

# Prefijo de las claves nuevas (por conversación). Lo que NO lo lleve es una
# clave heredada (identificador de contacto) que quedó encolada antes del
# despliegue: se sigue drenando igual, solo que sin cerrojo por conversación.
CONV_KEY_PREFIX = "conv:"


def conversation_key(conversation_id: "uuid.UUID | str") -> str:
    """Clave de buffer ESTABLE de una conversación."""
    return f"{CONV_KEY_PREFIX}{conversation_id}"


def conversation_id_from_key(key: str) -> "uuid.UUID | None":
    """Extrae el id de conversación de una clave nueva. None si es heredada."""
    if not key.startswith(CONV_KEY_PREFIX):
        return None
    try:
        return uuid.UUID(key[len(CONV_KEY_PREFIX):])
    except (ValueError, AttributeError):
        return None


async def push_message(phone: str, message_id: str) -> None:
    r = get_redis()
    await r.rpush(KEY_QUEUE.format(phone=phone), message_id)
    await r.set(KEY_LAST.format(phone=phone), str(time.time()), ex=300)


async def time_since_last(phone: str) -> float:
    r = get_redis()
    v = await r.get(KEY_LAST.format(phone=phone))
    if not v:
        return float("inf")
    return time.time() - float(v)


async def drain_queue(phone: str) -> list[str]:
    """Atómico: vacía la cola y devuelve los message_ids acumulados."""
    r = get_redis()
    async with r.pipeline(transaction=True) as p:
        p.lrange(KEY_QUEUE.format(phone=phone), 0, -1)
        p.delete(KEY_QUEUE.format(phone=phone))
        p.delete(KEY_LAST.format(phone=phone))
        results = await p.execute()
    return results[0] or []


async def drain_peek(phone: str) -> list[str]:
    """Mira la cola SIN vaciarla: message_ids que esperan un drain futuro.

    Lo usa el runtime justo antes de ENVIAR la respuesta generada: si llegó
    otro mensaje del cliente durante la generación (cola no vacía), la
    respuesta se descarta y el drain pendiente contesta a todo junto.
    """
    r = get_redis()
    return await r.lrange(KEY_QUEUE.format(phone=phone), 0, -1) or []


async def should_process(phone: str, wait_seconds: int | None = None) -> bool:
    """Decide si han pasado N segundos desde el último mensaje."""
    wait = wait_seconds if wait_seconds is not None else settings.MESSAGE_BUFFER_SECONDS
    elapsed = await time_since_last(phone)
    return elapsed >= wait
