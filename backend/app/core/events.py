"""Bus interno publish/subscribe en proceso para conectar workers <-> WebSocket.

Implementacion minima basada en Redis pub/sub. Cada evento se publica a un canal
identificado por `user_id` para que el WebSocket del cliente solo reciba lo suyo.

Cliente publisher cacheado POR EVENT LOOP (mismo patron exacto que
app.core.redis, ver alli el razonamiento largo):

  - Celery con --pool=threads llama asyncio.run() por task, o sea un event loop
    NUEVO cada vez. Un singleton global quedaria atado al primero y desde otro
    loop daria 'Event loop is closed'. -> no puede ser un singleton global.
  - Starlette ejecuta cada request en un Context NUEVO. La version anterior
    cacheaba en un ContextVar: el set() de una peticion se descartaba al
    terminar y la siguiente veia el default (None), creando un POOL DE
    CONEXIONES NUEVO por peticion del panel (handshake TCP incluido) y sin
    cerrar el anterior. -> no puede ser un ContextVar.

Solucion: dict {(id(loop), url): (loop, cliente)} con poda de loops cerrados.
En la API el loop es unico y estable -> un solo pool reutilizado.

SUSCRIPCION: una sola conexion pub/sub por proceso, con reparto en memoria
------------------------------------------------------------------------
`subscribe()` abria ANTES un cliente Redis nuevo por suscriptor
(`redis_async.from_url(...)` dentro de la propia funcion). Con el widget web
embebido en la web de un cliente, cada visitante que abre la burbuja crea una
suscripcion; si la corrutina que la consume no termina (el caso normal: el
WebSocket no detectaba la marcha del visitante), la conexion no se cierra
NUNCA. 300 visitas al dia = 300 conexiones Redis colgadas al dia hasta agotar
Redis o los descriptores del proceso.

Ahora hay UN broker por (event loop, url): una unica conexion pub/sub, una
unica tarea lectora, y el reparto a los suscriptores se hace en memoria con
colas acotadas. Consecuencias:

  - N suscriptores = 1 conexion Redis, no N.
  - Si el enlace con Redis muere en silencio, el `ping` periodico lo detecta y
    se avisa a TODOS los suscriptores (sale su `async for`), en vez de dejar
    WebSockets abiertos y mudos.
  - La limpieza al soltar un suscriptor es SINCRONA (quitar la cola del
    registro). Asi se ejecuta entera aunque la tarea que consume el generador
    haya sido cancelada — un `await` en el `finally` de un generador cancelado
    aborta a la primera y dejaria la cola registrada para siempre. El
    `UNSUBSCRIBE` a Redis, que si necesita await, se lanza como tarea aparte y
    es idempotente.
  - Cola acotada por suscriptor: un consumidor lento (WebSocket atascado) tira
    sus eventos mas viejos en vez de bloquear al lector, que sirve a todos.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator

import redis.asyncio as redis_async

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_clients: dict[
    tuple[int, str], tuple[asyncio.AbstractEventLoop, redis_async.Redis]
] = {}

# Canal ficticio al que el broker se queda suscrito siempre. Sin ninguna
# suscripcion activa, `PubSub.get_message()` revienta ("pubsub connection not
# set") y `listen()` termina; con esto la conexion se mantiene viva aunque en
# ese instante no haya ningun visitante escuchando.
_KEEPALIVE_CHANNEL = "__eventbus_keepalive__"

# Cada cuanto se hace PING sobre la conexion pub/sub. Sin esto, un enlace que
# muere en silencio (proxy que corta, Redis reiniciado) deja el WebSocket del
# visitante abierto y mudo: pone "conectado" y no le llega nada nunca.
PUBSUB_PING_INTERVAL_SECONDS = 20.0

# Tope de eventos en vuelo por suscriptor antes de empezar a tirar los viejos.
SUBSCRIBER_QUEUE_MAX = 500

# Centinela: le dice al suscriptor "el bus se ha caido, sal del async for".
_BUS_DOWN = object()


def _prune_dead_loops() -> None:
    """Suelta los clientes de loops ya cerrados (Celery crea un loop por task).

    Tambien evita que un id() reciclado devuelva el cliente de un loop muerto.
    """
    for key, (loop, _client) in list(_clients.items()):
        if loop.is_closed():
            _clients.pop(key, None)


class _Broker:
    """Una sola conexion pub/sub + reparto en memoria a N suscriptores."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: redis_async.Redis | None = None
        self._pubsub = None
        self._reader: asyncio.Task | None = None
        self._subs: dict[str, set[asyncio.Queue]] = {}
        # Serializa TODAS las escrituras sobre la conexion pub/sub
        # (SUBSCRIBE / UNSUBSCRIBE / PING). La lectura la hace solo la tarea
        # lectora, asi que lectura y escritura no compiten entre si.
        self._lock = asyncio.Lock()

    # ---- ciclo de vida ----

    @property
    def _running(self) -> bool:
        return (
            self._pubsub is not None
            and self._reader is not None
            and not self._reader.done()
        )

    async def _start_locked(self) -> None:
        if self._running:
            return
        # Restos de un intento anterior (enlace caido): a la basura.
        await self._shutdown_locked()
        client = redis_async.from_url(self._url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(_KEEPALIVE_CHANNEL)
        for channel in list(self._subs):
            await pubsub.subscribe(channel)
        self._client = client
        self._pubsub = pubsub
        self._reader = asyncio.create_task(self._read_loop(pubsub))

    async def _shutdown_locked(self) -> None:
        reader, pubsub, client = self._reader, self._pubsub, self._client
        self._reader = self._pubsub = self._client = None
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.aclose()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            self._release_all()
            await self._shutdown_locked()

    # ---- lector unico ----

    async def _read_loop(self, pubsub) -> None:
        last_ping = time.monotonic()
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is not None:
                    self._dispatch(message)
                    continue
                now = time.monotonic()
                if now - last_ping >= PUBSUB_PING_INTERVAL_SECONDS:
                    # Comprobacion de salud: si el enlace esta muerto, esto
                    # levanta excepcion y salimos avisando a los suscriptores.
                    async with self._lock:
                        if self._pubsub is not pubsub:
                            return
                        await pubsub.ping()
                    last_ping = now
        except asyncio.CancelledError:
            raise
        except Exception as e:  # enlace roto / Redis caido
            logger.warning("event_bus.subscriber_link_down", error=str(e))
        # Salida no cancelada = el bus ya no entrega nada. Se avisa a los
        # suscriptores para que cierren su WebSocket y el cliente reconecte,
        # en vez de dejarlos "conectados" y mudos para siempre.
        if self._pubsub is pubsub:
            self._release_all()
            self._pubsub = None

    def _dispatch(self, message: dict) -> None:
        if message.get("type") not in ("message", "pmessage"):
            return
        channel = message.get("channel")
        data = message.get("data")
        if not channel or not data:
            return
        queues = self._subs.get(channel)
        if not queues:
            return
        try:
            event = json.loads(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        for queue in list(queues):
            _offer(queue, event)

    def _release_all(self) -> None:
        for queues in list(self._subs.values()):
            for queue in list(queues):
                _offer(queue, _BUS_DOWN)
        self._subs.clear()

    # ---- API de suscripcion ----

    async def subscribe(self, channel: str) -> AsyncIterator[dict]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        async with self._lock:
            await self._start_locked()
            first = not self._subs.get(channel)
            self._subs.setdefault(channel, set()).add(queue)
            if first:
                await self._pubsub.subscribe(channel)
        try:
            while True:
                item = await queue.get()
                if item is _BUS_DOWN:
                    return
                yield item
        finally:
            # SINCRONO a proposito: ver la nota del docstring del modulo.
            self._drop(channel, queue)

    def _drop(self, channel: str, queue: asyncio.Queue) -> None:
        queues = self._subs.get(channel)
        if queues is None:
            return
        queues.discard(queue)
        if queues:
            return
        self._subs.pop(channel, None)
        pubsub = self._pubsub
        if pubsub is None:
            return
        # sin loop corriendo (cierre del proceso) no hay nada que soltar
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(
                self._unsubscribe_channel(channel, pubsub)
            )

    async def _unsubscribe_channel(self, channel: str, pubsub) -> None:
        async with self._lock:
            if self._subs.get(channel):
                return  # alguien volvio a suscribirse mientras tanto
            if self._pubsub is not pubsub:
                return
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)


def _offer(queue: asyncio.Queue, item) -> None:
    """Encola sin bloquear. Si el suscriptor va lento, pierde lo mas viejo."""
    try:
        queue.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass
    with contextlib.suppress(asyncio.QueueEmpty):
        queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(item)


_brokers: dict[tuple[int, str], tuple[asyncio.AbstractEventLoop, _Broker]] = {}


def _prune_dead_broker_loops() -> None:
    for key, (loop, _broker) in list(_brokers.items()):
        if loop.is_closed():
            _brokers.pop(key, None)


def _get_broker(url: str) -> _Broker:
    loop = asyncio.get_running_loop()
    _prune_dead_broker_loops()
    key = (id(loop), url)
    entry = _brokers.get(key)
    if entry is not None:
        return entry[1]
    broker = _Broker(url)
    _brokers[key] = (loop, broker)
    return broker


async def close_event_bus() -> None:
    """Cierra el/los clientes del loop actual (best-effort). Lo llama el lifespan."""
    loop = asyncio.get_running_loop()
    _prune_dead_loops()
    for key in [k for k in _clients if k[0] == id(loop)]:
        _loop, client = _clients.pop(key)
        with contextlib.suppress(Exception):
            await client.aclose()
    _prune_dead_broker_loops()
    for key in [k for k in _brokers if k[0] == id(loop)]:
        _loop, broker = _brokers.pop(key)
        with contextlib.suppress(Exception):
            await broker.aclose()


class EventBus:
    def __init__(self, url: str | None = None) -> None:
        self._url = url or settings.REDIS_URL

    async def _get_publisher(self) -> redis_async.Redis:
        loop = asyncio.get_running_loop()
        _prune_dead_loops()
        key = (id(loop), self._url)
        entry = _clients.get(key)
        if entry is not None:
            return entry[1]
        client = redis_async.from_url(
            self._url, decode_responses=True, max_connections=20
        )
        _clients[key] = (loop, client)
        return client

    async def publish(self, channel: str, event_type: str, payload: dict) -> None:
        client = await self._get_publisher()
        message = json.dumps({"type": event_type, "payload": payload})
        await client.publish(channel, message)

    async def subscribe(self, channel: str) -> AsyncIterator[dict]:
        """Eventos del canal. El generador termina si el bus se cae.

        Una sola conexion pub/sub por proceso: el broker reparte en memoria.
        """
        broker = _get_broker(self._url)
        async for event in broker.subscribe(channel):
            yield event

    async def close(self) -> None:
        """Compatibilidad: cierra los clientes cacheados de este event loop."""
        await close_event_bus()


event_bus = EventBus()


def inbox_channel(user_id: str = "all") -> str:
    """Por ahora todos los usuarios cliente escuchan el mismo canal global.
    Cuando haya multiples clientes por instancia, esto puede particionar."""
    return f"inbox:{user_id}"
