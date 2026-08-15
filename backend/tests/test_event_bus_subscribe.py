"""El bus NO puede abrir una conexión Redis por suscriptor.

Fallo que cubre (auditoría del canal web, B2): `EventBus.subscribe()` hacía
`redis_async.from_url(...)` DENTRO de la propia función — un cliente Redis
entero por suscriptor, fuera del pool. Y el `finally` que lo cerraba no se
alcanzaba nunca, porque el WebSocket del visitante se quedaba bloqueado para
siempre en el `async for` (no leía del socket, así que no se enteraba de que el
visitante se había ido).

Cuentas: un WordPress con 300 visitas al día que abran la burbuja acumulaba 300
conexiones Redis y 300 tareas colgadas al día, hasta agotar Redis o los
descriptores del proceso. Por el canal más expuesto del producto.

Fix: un broker por proceso (una conexión pub/sub, una tarea lectora) y reparto
en memoria. Además, `pubsub.listen()` se usaba sin ninguna comprobación de
salud: si el enlace moría en silencio, el WebSocket quedaba abierto y mudo
—"conectado" y sin recibir nada— para siempre.

Necesita Redis de verdad: pub/sub no se puede fingir sin fingir el fallo.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings


def _redis_available() -> bool:
    try:
        import redis as redis_sync

        redis_sync.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(), reason="Redis no disponible en este entorno"
)


async def _drain(events, out, ready: asyncio.Event) -> None:
    ready.set()
    async for ev in events:
        out.append(ev)


@pytest.mark.asyncio
async def test_cien_suscriptores_una_sola_conexion():
    """Lo que antes eran N conexiones Redis ahora es UNA."""
    from app.core import events as mod

    await mod.close_event_bus()
    bus = mod.EventBus()
    generadores = []
    tareas = []
    try:
        for i in range(25):
            ready = asyncio.Event()
            gen = bus.subscribe(f"canal-test-{i}")
            generadores.append(gen)
            tareas.append(asyncio.create_task(_drain(gen, [], ready)))
            await ready.wait()
        await asyncio.sleep(0.2)

        brokers = [b for (_loop, b) in mod._brokers.values()]
        assert len(brokers) == 1
        # Un cliente y un pubsub para los 25, no 25.
        assert brokers[0]._client is not None
        assert brokers[0]._reader is not None and not brokers[0]._reader.done()
        assert len(brokers[0]._subs) == 25
    finally:
        for t in tareas:
            t.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
        await mod.close_event_bus()


@pytest.mark.asyncio
async def test_cancelar_al_suscriptor_libera_su_hueco():
    """La marcha de un visitante tiene que soltar SIEMPRE su suscripción.

    Es sincrona a propósito: un `await` en el `finally` de un generador
    cancelado aborta a la primera y dejaría la cola registrada para siempre —
    exactamente la fuga que se está arreglando.
    """
    from app.core import events as mod

    await mod.close_event_bus()
    bus = mod.EventBus()
    try:
        ready = asyncio.Event()
        gen = bus.subscribe("canal-que-se-va")
        tarea = asyncio.create_task(_drain(gen, [], ready))
        await ready.wait()
        await asyncio.sleep(0.1)
        broker = next(b for (_l, b) in mod._brokers.values())
        assert "canal-que-se-va" in broker._subs

        tarea.cancel()
        await asyncio.gather(tarea, return_exceptions=True)
        await asyncio.sleep(0.1)

        assert "canal-que-se-va" not in broker._subs
        # Y el broker sigue vivo para el resto del proceso.
        assert not broker._reader.done()
    finally:
        await mod.close_event_bus()


@pytest.mark.asyncio
async def test_lo_publicado_llega_a_todos_los_suscriptores_del_canal():
    """Dos pestañas del mismo visitante reciben lo mismo (reparto en memoria)."""
    from app.core import events as mod

    await mod.close_event_bus()
    bus = mod.EventBus()
    canal = "canal-reparto"
    a: list = []
    b: list = []
    tareas = []
    try:
        for destino in (a, b):
            ready = asyncio.Event()
            tareas.append(asyncio.create_task(_drain(bus.subscribe(canal), destino, ready)))
            await ready.wait()
        await asyncio.sleep(0.2)  # que el SUBSCRIBE llegue a Redis

        await bus.publish(canal, "message.out", {"text": "hola"})
        for _ in range(50):
            if a and b:
                break
            await asyncio.sleep(0.05)

        assert a == [{"type": "message.out", "payload": {"text": "hola"}}]
        assert b == a
        # Y no se cuela lo de otros canales.
        await bus.publish("otro-canal", "message.out", {"text": "no"})
        await asyncio.sleep(0.2)
        assert len(a) == 1
    finally:
        for t in tareas:
            t.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
        await mod.close_event_bus()


@pytest.mark.asyncio
async def test_si_el_bus_se_cae_el_suscriptor_sale_en_vez_de_quedarse_mudo():
    """Sin esto, el visitante ve "conectado" y no recibe nada nunca."""
    from app.core import events as mod

    await mod.close_event_bus()
    bus = mod.EventBus()
    salio = asyncio.Event()

    async def _consumir():
        async for _ev in bus.subscribe("canal-que-muere"):
            pass
        salio.set()

    try:
        tarea = asyncio.create_task(_consumir())
        await asyncio.sleep(0.2)
        broker = next(b for (_l, b) in mod._brokers.values())
        assert "canal-que-muere" in broker._subs

        # Simula el enlace roto: es lo que detecta el ping periódico.
        broker._release_all()

        await asyncio.wait_for(salio.wait(), timeout=2)
        await asyncio.gather(tarea, return_exceptions=True)
    finally:
        await mod.close_event_bus()


@pytest.mark.asyncio
async def test_un_consumidor_lento_no_bloquea_al_lector():
    """La cola por suscriptor está acotada: el que no lee pierde lo viejo, pero
    no atasca al lector, que sirve a TODOS los canales del proceso."""
    from app.core import events as mod

    cola: asyncio.Queue = asyncio.Queue(maxsize=3)
    for i in range(10):
        mod._offer(cola, i)
    assert cola.qsize() == 3
    assert [cola.get_nowait() for _ in range(3)] == [7, 8, 9]


@pytest.mark.asyncio
async def test_hay_comprobacion_de_salud_del_enlace():
    """`listen()` a secas no detecta un enlace muerto en silencio."""
    from app.core import events as mod

    assert 0 < mod.PUBSUB_PING_INTERVAL_SECONDS <= 60
    assert mod.SUBSCRIBER_QUEUE_MAX > 0


@pytest.mark.asyncio
async def test_close_event_bus_cierra_tambien_el_broker():
    from app.core import events as mod

    await mod.close_event_bus()
    bus = mod.EventBus()
    ready = asyncio.Event()
    tarea = asyncio.create_task(_drain(bus.subscribe("canal-cierre"), [], ready))
    await ready.wait()
    await asyncio.sleep(0.1)
    assert mod._brokers

    await mod.close_event_bus()
    assert not mod._brokers
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)
