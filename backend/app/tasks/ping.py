"""Latido del worker.

Beat encola esta tarea cada 5 minutos. Además de dejar rastro en el log, deja
una marca en Redis con TTL: si la marca falta o está caducada, el worker no
está consumiendo la cola y el panel puede decirlo.

Por qué existe: el worker se ha quedado muerto tras un despliegue y
estuvo 20 horas sin ejecutar NADA (ni transcripciones de notas de voz, ni
respuestas del agente, ni tareas nocturnas) sin que ningún indicador se pusiera
en rojo — el panel solo miraba BD y Redis, que son del backend, no del worker.
"""
import asyncio
import time

from celery import shared_task

from app.core.logging import get_logger

logger = get_logger(__name__)

# Clave y ventana del latido. Beat lo refresca cada 5 min; damos margen de tres
# ciclos antes de dar el worker por caído (un reinicio normal no debe alertar).
WORKER_HEARTBEAT_KEY = "worker:heartbeat"
WORKER_HEARTBEAT_TTL_S = 15 * 60


async def _write_heartbeat() -> None:
    from app.core.redis import get_redis

    await get_redis().set(
        WORKER_HEARTBEAT_KEY, str(int(time.time())), ex=WORKER_HEARTBEAT_TTL_S
    )


@shared_task(name="app.tasks.ping.ping")
def ping() -> str:
    try:
        asyncio.run(_write_heartbeat())
    except Exception as e:  # noqa: BLE001 — el latido nunca debe romper la tarea
        logger.warning("celery.ping.heartbeat_failed", error=str(e))
    logger.info("celery.ping")
    return "pong"
