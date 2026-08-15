"""Fábrica CACHEADA de clientes AsyncOpenAI, siempre con timeout.

Dos problemas que resuelve, los dos con la misma raíz (`AsyncOpenAI(api_key=…)`
creado a pelo en cada llamada):

  - SIN TIMEOUT. El default del SDK es 600 s de lectura y 2 reintentos: una
    llamada colgada bloquea hasta 30 minutos. Los workers atienden el chatbot
    con concurrencia 4 → cuatro llamadas atascadas y el bot se queda mudo, sin
    error, sin log y sin que nadie se entere hasta que llega la queja.
  - UN CLIENTE POR LLAMADA. Cada `AsyncOpenAI(...)` levanta su propio
    `httpx.AsyncClient` con su pool de conexiones, y nadie lo cierra: sockets
    que se acumulan y un handshake TCP+TLS nuevo en cada llamada.

Cache por (event loop, clave, base_url, timeout, reintentos), igual que
`app.core.redis`: el pool de httpx queda atado al loop donde se creó y Celery
abre un loop nuevo por task, así que un singleton global fallaría con
"attached to a different loop". Que la clave forme parte del identificador
mantiene el comportamiento de siempre: cambiarla en el panel aplica sin
reiniciar (se crea un cliente nuevo).
"""
from __future__ import annotations

import asyncio

import httpx
from openai import AsyncOpenAI

# Timeouts por tipo de uso. Criterio: cuánto puede tardar como mucho una
# llamada sana antes de que valga más la pena rendirse y derivar/reintentar.
CHAT_TIMEOUT_SECONDS = 60.0          # respuesta del agente; el usuario espera
EMBEDDING_TIMEOUT_SECONDS = 30.0     # lote de embeddings, siempre rápido
TRANSCRIPTION_TIMEOUT_SECONDS = 120.0  # una nota de voz larga sube y se procesa
MODERATION_TIMEOUT_SECONDS = 15.0    # va delante de cada mensaje: no puede frenar

# Conectar es rápido o no es: 10 s de sobra incluso con TLS y DNS lento.
CONNECT_TIMEOUT_SECONDS = 10.0
# 1 = un reintento. El SDK trae 2 por defecto, que triplica el tiempo colgado.
DEFAULT_MAX_RETRIES = 1

_clients: dict[tuple, tuple[asyncio.AbstractEventLoop, AsyncOpenAI]] = {}


def get_async_openai(
    *,
    api_key: str,
    base_url: str | None = None,
    timeout: float = CHAT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> AsyncOpenAI:
    """Cliente AsyncOpenAI del event loop actual (lo crea si no existe)."""
    loop = asyncio.get_running_loop()
    # Poda best-effort: suelta los clientes de loops ya cerrados (Celery crea un
    # loop por task) y evita que un id() reciclado devuelva un cliente muerto.
    for key, (cached_loop, _client) in list(_clients.items()):
        if cached_loop.is_closed():
            _clients.pop(key, None)

    key = (id(loop), api_key, base_url, timeout, max_retries)
    entry = _clients.get(key)
    if entry is not None:
        return entry[1]

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_SECONDS),
        max_retries=max_retries,
    )
    _clients[key] = (loop, client)
    return client


async def close_openai_clients() -> None:
    """Cierra los clientes del loop actual (se llama en el apagado de la API).

    Cierra también los de Anthropic, que tienen su propia caché en
    `providers/llm/anthropic_client.py` (otro SDK, otro constructor). Se
    encadena aquí en vez de añadir una segunda llamada en `main.py` para que no
    haya forma de añadir un proveedor y dejarse conexiones abiertas al apagar.
    """
    loop_id = id(asyncio.get_running_loop())
    for key in [k for k in _clients if k[0] == loop_id]:
        _loop, client = _clients.pop(key)
        try:
            await client.close()
        except Exception:
            pass
    try:
        from app.providers.llm.anthropic_client import close_anthropic_clients

        await close_anthropic_clients()
    except Exception:
        pass


def reset_openai_clients() -> None:
    """Vacía la caché sin cerrar nada. Solo para los tests."""
    _clients.clear()
