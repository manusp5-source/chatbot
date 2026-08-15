"""El WebSocket de la bandeja tiene que enterarse de que la operadora se ha ido.

Mismo fallo que tenía el del widget (ver test_webchat_ws.py): el handler
aceptaba la conexión y se ponía a escuchar el bus con un `async for` SIN llamar
nunca a `websocket.receive()`. En Starlette la desconexión solo se detecta por
`receive()` o cuando falla un `send`, así que si en el canal de la bandeja no se
publicaba nada, la corrutina se quedaba colgada indefinidamente.

El bus ya comparte una sola conexión a Redis por proceso, así que no fugaba una
conexión por pestaña, pero sí una tarea y una cola por cada pestaña del panel
abierta. El impacto es menor que en el widget (son operadoras identificadas, no
tráfico anónimo), pero es el mismo fallo.

Se prueba el handler REAL contra Postgres y Redis, con un doble del WebSocket
que se comporta como Starlette.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.config import settings


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("select 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False


def _redis_available() -> bool:
    try:
        import redis as redis_sync

        redis_sync.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (_db_available() and _redis_available()),
    reason="Necesita Postgres y Redis (se ejecutan en CI)",
)


class FakeWebSocket:
    """Doble del WebSocket de Starlette, con su semántica de desconexión."""

    def __init__(self) -> None:
        self.accepted = False
        self.closed_with: int | None = None
        self.sent: list[str] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._gone = False

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        if self.closed_with is None:
            self.closed_with = code

    async def send_text(self, data: str) -> None:
        if self._gone:
            raise RuntimeError("socket cerrado")
        self.sent.append(data)

    async def receive(self) -> dict:
        return await self._incoming.get()

    # --- lo que hace la operadora ---
    def cierra_la_pestana(self) -> None:
        self._gone = True
        self._incoming.put_nowait({"type": "websocket.disconnect", "code": 1001})


async def _operadora_con_ticket() -> tuple[uuid.UUID, str]:
    """Usuaria del panel + ticket efímero válido (lo que emite /auth/ws-ticket)."""
    import secrets

    from app.core.redis import get_redis
    from app.core.security import hash_password
    from app.db.session import db_session
    from app.models.user import User

    suf = uuid.uuid4().hex[:8]
    async with db_session() as db:
        user = User(
            email=f"ws-{suf}@test.local",
            password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
            nombre="Operadora WS",
            role="cliente",
        )
        db.add(user)
        await db.commit()
        user_id = user.id
    ticket = secrets.token_urlsafe(32)
    await get_redis().set(f"wsticket:{ticket}", str(user_id), ex=60)
    return user_id, ticket


async def _borrar_usuario(user_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app.db.session import db_session

    async with db_session() as db:
        await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(user_id)})
        await db.commit()


# ---------------------------------------------------------------------------


def test_cuando_la_operadora_cierra_la_pestana_se_suelta_la_suscripcion():
    from app.api import conversations as conv_api
    from app.core import events as bus_mod
    from app.core.events import inbox_channel

    async def _run():
        await bus_mod.close_event_bus()
        user_id, ticket = await _operadora_con_ticket()
        ws = FakeWebSocket()
        try:
            tarea = asyncio.create_task(conv_api.ws_inbox(ws, ticket=ticket))
            canal = inbox_channel()
            for _ in range(60):
                await asyncio.sleep(0.05)
                brokers = [b for (_l, b) in bus_mod._brokers.values()]
                if brokers and canal in brokers[0]._subs:
                    break
            assert ws.accepted
            broker = [b for (_l, b) in bus_mod._brokers.values()][0]
            assert canal in broker._subs

            ws.cierra_la_pestana()

            # ANTES esto se quedaba colgado para siempre: sin nada publicado en
            # el canal, la corrutina no volvía nunca.
            await asyncio.wait_for(tarea, timeout=5)

            await asyncio.sleep(0.1)
            assert canal not in broker._subs, "la suscripción quedó colgando"
        finally:
            await _borrar_usuario(user_id)
            await bus_mod.close_event_bus()

    asyncio.run(_run())


def test_los_eventos_de_la_bandeja_siguen_llegando_al_panel():
    """El arreglo no vale si deja al panel sin eventos en tiempo real."""
    import json

    from app.api import conversations as conv_api
    from app.core import events as bus_mod
    from app.core.events import inbox_channel

    async def _run():
        await bus_mod.close_event_bus()
        user_id, ticket = await _operadora_con_ticket()
        ws = FakeWebSocket()
        try:
            tarea = asyncio.create_task(conv_api.ws_inbox(ws, ticket=ticket))
            canal = inbox_channel()
            for _ in range(60):
                await asyncio.sleep(0.05)
                brokers = [b for (_l, b) in bus_mod._brokers.values()]
                if brokers and canal in brokers[0]._subs:
                    break
            await asyncio.sleep(0.2)

            await bus_mod.event_bus.publish(
                canal, "message.new", {"conversation_id": "abc"}
            )
            for _ in range(60):
                if ws.sent:
                    break
                await asyncio.sleep(0.05)

            assert ws.sent, "no llegó nada al panel"
            payload = json.loads(ws.sent[0])
            assert payload["type"] == "message.new"
            assert payload["payload"]["conversation_id"] == "abc"

            ws.cierra_la_pestana()
            await asyncio.wait_for(tarea, timeout=5)
        finally:
            await _borrar_usuario(user_id)
            await bus_mod.close_event_bus()

    asyncio.run(_run())


def test_si_el_bus_se_cae_el_websocket_no_se_queda_abierto_y_mudo():
    from app.api import conversations as conv_api
    from app.core import events as bus_mod
    from app.core.events import inbox_channel

    async def _run():
        await bus_mod.close_event_bus()
        user_id, ticket = await _operadora_con_ticket()
        ws = FakeWebSocket()
        try:
            tarea = asyncio.create_task(conv_api.ws_inbox(ws, ticket=ticket))
            canal = inbox_channel()
            for _ in range(60):
                await asyncio.sleep(0.05)
                brokers = [b for (_l, b) in bus_mod._brokers.values()]
                if brokers and canal in brokers[0]._subs:
                    break
            broker = [b for (_l, b) in bus_mod._brokers.values()][0]

            # El enlace con Redis muere en silencio (lo detecta el ping).
            broker._release_all()

            # El WebSocket se cierra: el panel reconectará con su backoff.
            await asyncio.wait_for(tarea, timeout=5)
            assert ws.closed_with is not None
        finally:
            await _borrar_usuario(user_id)
            await bus_mod.close_event_bus()

    asyncio.run(_run())


def test_sin_ticket_valido_ni_se_acepta():
    from app.api import conversations as conv_api

    async def _run():
        for ticket in (None, "", "basura"):
            ws = FakeWebSocket()
            await conv_api.ws_inbox(ws, ticket=ticket)
            assert ws.accepted is False
            assert ws.closed_with == 4401

    asyncio.run(_run())


def test_el_ticket_es_de_un_solo_uso():
    from app.api import conversations as conv_api

    async def _run():
        user_id, ticket = await _operadora_con_ticket()
        ws = FakeWebSocket()
        try:
            tarea = asyncio.create_task(conv_api.ws_inbox(ws, ticket=ticket))
            for _ in range(60):
                await asyncio.sleep(0.05)
                if ws.accepted:
                    break
            assert ws.accepted

            # El mismo ticket una segunda vez ya no vale.
            otro = FakeWebSocket()
            await conv_api.ws_inbox(otro, ticket=ticket)
            assert otro.accepted is False
            assert otro.closed_with == 4401

            ws.cierra_la_pestana()
            await asyncio.wait_for(tarea, timeout=5)
        finally:
            await _borrar_usuario(user_id)

    asyncio.run(_run())


def test_hay_latido_y_vida_maxima():
    """Sin latido, un enlace muerto deja el panel en 'conectado' sin recibir
    nada. Sin vida máxima, una conexión se queda colgada para siempre."""
    from app.api import conversations as conv_api

    assert 0 < conv_api.WS_HEARTBEAT_SECONDS <= 60
    assert 0 < conv_api.WS_MAX_LIFETIME_SECONDS <= 24 * 3600


def test_el_handler_lee_del_socket():
    """Guarda: que nadie vuelva a un `async for` sobre el bus sin leer del
    socket. Es la única forma que da Starlette de detectar la marcha."""
    import inspect

    from app.api import conversations as conv_api

    src = inspect.getsource(conv_api.ws_inbox)
    assert "websocket.receive()" in src
