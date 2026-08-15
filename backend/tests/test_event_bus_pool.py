"""El bus de eventos NO puede crear un pool de Redis por petición.

Fallo que cubre (auditoría #5): `EventBus._get_publisher` cacheaba el cliente en
un `ContextVar`. Starlette ejecuta cada request en un `Context` NUEVO, así que
el `set()` de una petición se descarta al acabar y la siguiente vuelve a ver el
default (`None`) → cliente y pool de conexiones NUEVOS en cada request del
panel, con su handshake TCP, y sin cerrar los anteriores. Es exactamente el
fallo que ya se corrigió en `app/core/redis.py` y que aquí quedó sin aplicar.

El patrón correcto es caché POR EVENT LOOP: en la API el loop es único y
estable (un solo pool reutilizado); en Celery cada task abre un loop nuevo con
`asyncio.run`, y un singleton global fallaría con "attached to a different
loop".

`redis.asyncio.from_url` no conecta hasta el primer comando, así que estos
tests no necesitan un Redis levantado.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_mismo_cliente_entre_contextos_distintos():
    """Dos "peticiones" (contexts distintos) comparten el mismo cliente/pool.

    `asyncio.create_task` copia el contexto: lo que se hace `set()` dentro NO
    vuelve al padre — la misma semántica que un request de Starlette. Con la
    implementación de ContextVar cada tarea creaba su propio cliente.
    """
    from app.core.events import EventBus, close_event_bus

    await close_event_bus()
    bus = EventBus()
    try:
        c1 = await asyncio.create_task(bus._get_publisher())
        c2 = await asyncio.create_task(bus._get_publisher())
        c3 = await bus._get_publisher()
        assert c1 is c2 is c3
    finally:
        await close_event_bus()


@pytest.mark.asyncio
async def test_dos_instancias_de_eventbus_comparten_pool_por_url():
    """La caché es por (loop, url), no por instancia: `event_bus` global y
    cualquier otra instancia con la misma URL reutilizan el mismo pool."""
    from app.core.events import EventBus, close_event_bus

    await close_event_bus()
    try:
        assert await EventBus()._get_publisher() is await EventBus()._get_publisher()
    finally:
        await close_event_bus()


def test_cada_event_loop_tiene_su_cliente():
    """Celery abre un loop por task (`asyncio.run`): un singleton global se
    quedaría atado al primer loop y desde el segundo daría 'Event loop is
    closed'. Cada loop debe tener el suyo."""
    from app.core.events import EventBus, close_event_bus

    async def _one():
        bus = EventBus()
        client = await bus._get_publisher()
        # Mismo loop, misma instancia devuelta.
        assert await bus._get_publisher() is client
        return id(client)

    a = asyncio.run(_one())
    b = asyncio.run(_one())
    assert a != b
    asyncio.run(close_event_bus())


def test_no_se_acumulan_clientes_de_loops_muertos():
    """La caché se poda: en Celery, un cliente por task acabaría en fuga."""
    from app.core import events as events_mod

    async def _one():
        await events_mod.EventBus()._get_publisher()

    for _ in range(5):
        asyncio.run(_one())
    # Como mucho queda la entrada del último loop (ya cerrado), que se poda en
    # la siguiente llamada. Nunca 5.
    assert len(events_mod._clients) <= 1
    asyncio.run(events_mod.close_event_bus())


@pytest.mark.asyncio
async def test_close_event_bus_libera_el_cliente_del_loop():
    from app.core import events as events_mod

    await events_mod.close_event_bus()
    client = await events_mod.event_bus._get_publisher()
    assert events_mod._clients
    await events_mod.close_event_bus()
    assert not events_mod._clients
    # Y la siguiente llamada crea uno nuevo (no devuelve el cerrado).
    nuevo = await events_mod.event_bus._get_publisher()
    assert nuevo is not client
    await events_mod.close_event_bus()
