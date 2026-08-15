import time

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import get_db

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


async def _worker_seconds_since_heartbeat() -> int | None:
    """Segundos desde el último latido del worker. None si no hay marca.

    El latido lo escribe la tarea `app.tasks.ping.ping`, que beat encola cada 5
    minutos (ver backend/app/tasks/ping.py). Si la marca falta o está caducada,
    el worker no está consumiendo la cola.
    """
    from app.tasks.ping import WORKER_HEARTBEAT_KEY

    try:
        raw = await get_redis().get(WORKER_HEARTBEAT_KEY)
    except Exception as e:  # noqa: BLE001
        logger.warning("health.worker.heartbeat_error", error=str(e))
        return None
    if not raw:
        return None
    try:
        return max(0, int(time.time()) - int(raw))
    except (TypeError, ValueError):
        return None


@router.get("/health")
async def health(response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    """Healthcheck que EasyPanel usa para mantener el container vivo.

    Devolvemos 503 si BD o Redis estan caidos para que EasyPanel reinicie el
    container. Solo 200 cuando esas dependencias del PROPIO backend estan OK.

    El worker se INFORMA pero NO cambia el código de estado, a propósito
    ---------------------------------------------------------------------
    Hasta ahora esta respuesta solo miraba Postgres y Redis, así que devolvía
    200 con el worker muerto: el contenedor figuraba "sano" y el sistema estaba
    inoperante (sin copias, sin transcripciones, sin respuestas del agente).
    Eso ha llegado a costar 20 horas sin ejecutar ni una tarea.

    Ahora el estado del worker sale en el cuerpo (`worker`,
    `worker_last_seen_seconds`), pero el código sigue siendo 200: este endpoint
    es el healthcheck del contenedor `app`, y devolver 503 porque murió OTRO
    contenedor haría que EasyPanel reiniciara la API en bucle. Reiniciar la API
    no resucita al worker; lo que sí hace es cortar el panel, los webhooks
    entrantes (mensajes de clientes perdidos) y la propia página desde la que se
    diagnostica la avería. Un worker caído es un problema del worker: su
    contenedor tiene su propio healthcheck (`celery inspect ping`) y su propio
    `restart: unless-stopped`, que es quien debe reiniciarlo.

    Quien quiera un semáforo global (el que se pone rojo si falta cualquier
    pieza) lo tiene en el endpoint de estado del Agent API, autenticado, que sí
    marca `degraded` cuando el worker no late.
    """
    db_ok = False
    redis_ok = False

    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.error("health.db.error", error=str(e))

    try:
        await get_redis().ping()
        redis_ok = True
    except Exception as e:
        logger.error("health.redis.error", error=str(e))

    # Estado del worker (informativo). Solo tiene sentido si Redis responde:
    # con Redis caído no hay marca que leer y "no sabemos" no es "está muerto".
    worker_last_seen_s: int | None = None
    worker_estado = "desconocido"
    if redis_ok:
        from app.tasks.ping import WORKER_HEARTBEAT_TTL_S

        worker_last_seen_s = await _worker_seconds_since_heartbeat()
        if worker_last_seen_s is None:
            # Sin marca: o nunca ha latido (recién desplegado, beat aún no ha
            # encolado el primer ping) o lleva más de 15 min sin latir y la
            # clave ha caducado sola.
            worker_estado = "sin_latido"
        elif worker_last_seen_s <= WORKER_HEARTBEAT_TTL_S:
            worker_estado = "ok"
        else:
            worker_estado = "caido"

        if worker_estado != "ok":
            # Que quede en el log del backend aunque el código sea 200: es la
            # pista para no perseguir al contenedor equivocado.
            logger.warning(
                "health.worker.no_late",
                estado=worker_estado,
                last_seen_seconds=worker_last_seen_s,
                msg=(
                    "El worker no está latiendo. La API sigue sana y responde 200 "
                    "a propósito (reiniciarla no arregla el worker). Mira los "
                    "contenedores worker y beat."
                ),
            )

    healthy = db_ok and redis_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if healthy else "degraded",
        "db": "ok" if db_ok else "fail",
        "redis": "ok" if redis_ok else "fail",
        # Informativo: NO influye en el código de estado (ver docstring).
        "worker": worker_estado,
        "worker_last_seen_seconds": worker_last_seen_s,
    }
