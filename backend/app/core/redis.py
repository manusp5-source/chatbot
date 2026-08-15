"""Cliente Redis async, cacheado POR EVENT LOOP.

Dos restricciones a la vez:

  - Celery con --pool=threads crea un event loop NUEVO en cada task via
    asyncio.run(). Un singleton global quedaria atado al primer loop y desde
    otro loop falla con 'bound to a different event loop' / 'Event loop is
    closed'. → No puede ser un singleton global.
  - En el proceso API (uvicorn) hay UN solo loop pero un Context NUEVO por
    request: la version anterior con ContextVar veia el default (None) en cada
    request y creaba un POOL DE CONEXIONES NUEVO por peticion (handshake TCP
    incluido). → No puede ser un ContextVar.

Solucion: cache por loop, clave id(asyncio.get_running_loop()). Guardamos
tambien la referencia al loop para podar entradas de loops ya cerrados (en
Celery se acumularia un cliente por task) y para que un id() reciclado nunca
devuelva el cliente de un loop muerto. En la API el loop es unico y estable →
un solo pool reutilizado entre requests.
"""
from __future__ import annotations

import asyncio

import redis.asyncio as redis_async

from app.core.config import settings

_clients: dict[int, tuple[asyncio.AbstractEventLoop, redis_async.Redis]] = {}


def get_redis() -> redis_async.Redis:
    """Devuelve el cliente Redis del event loop actual (lo crea si no existe)."""
    loop = asyncio.get_running_loop()
    # Poda best-effort: suelta los clientes de loops ya cerrados (Celery crea
    # un loop por task; al cerrarse el loop, redis-async libera los sockets).
    for key, (cached_loop, _client) in list(_clients.items()):
        if cached_loop.is_closed():
            _clients.pop(key, None)
    entry = _clients.get(id(loop))
    if entry is not None:
        return entry[1]
    client = redis_async.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        max_connections=50,
        # Sin timeouts, un Redis que acepta la conexión y luego no contesta deja
        # el await colgado PARA SIEMPRE. En el worker (--pool=threads) eso se
        # come un hilo sin dejar ni un log; con los 16 hilos así, el bot se
        # queda mudo y desde fuera parece que todo va bien.
        socket_connect_timeout=5,
        socket_timeout=10,
        retry_on_timeout=True,
    )
    _clients[id(loop)] = (loop, client)
    return client


async def close_redis() -> None:
    """Cierra el cliente del loop actual (best-effort)."""
    entry = _clients.pop(id(asyncio.get_running_loop()), None)
    if entry is not None:
        try:
            await entry[1].close()
        except Exception:
            pass
