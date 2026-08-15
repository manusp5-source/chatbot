"""El WebSocket del visitante tiene que enterarse de que el visitante se ha ido.

Fallo que cubre (auditoría del canal web, B2): el handler aceptaba la conexión y
se ponía a escuchar el bus con un `async for` SIN llamar nunca a
`websocket.receive()`. En Starlette la desconexión solo se detecta por
`receive()` o cuando falla un `send`, así que si en ese canal no se publicaba
nada —el caso normal en cuanto el visitante cierra la burbuja o cambia de
página— la corrutina se quedaba bloqueada para siempre, con su suscripción
colgando. Tráfico anónimo desde la web de un cliente: cada visitante que abría
la burbuja dejaba una tarea y una conexión Redis muertas hasta el siguiente
reinicio.

Se prueba el handler REAL contra Postgres y Redis, con un doble del WebSocket
que se comporta como Starlette: `receive()` devuelve `websocket.disconnect`
cuando el visitante se va, y `send_text()` revienta si ya se ha ido.
"""
from __future__ import annotations

import asyncio
import time
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

    # --- lo que hace el visitante ---
    def visitante_se_va(self) -> None:
        self._gone = True
        self._incoming.put_nowait({"type": "websocket.disconnect", "code": 1001})


async def _montar_conversacion():
    """Canal + contacto + conversación web, con su token de sesión válido."""
    from app.api import webchat as wc
    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal

    api_key = f"k-{uuid.uuid4().hex}"
    visitor_id = str(uuid.uuid4())
    async with db_session() as db:
        ch = Channel(
            type=ChannelType.webchat,
            name="Widget ws test",
            enabled=True,
            config={"api_key": api_key},
        )
        db.add(ch)
        contact = Contact(
            telefono=f"web:{visitor_id}", origen=ContactOrigen.web, in_crm=False
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.web,
            session_id=visitor_id,
        )
        db.add(conv)
        await db.commit()
        ids = (ch.id, contact.id, conv.id)
    token, _exp = wc._make_session_token(visitor_id, api_key)
    return {
        "channel_id": ids[0],
        "contact_id": ids[1],
        "conv_id": ids[2],
        "visitor_id": visitor_id,
        "api_key": api_key,
        "token": token,
    }


async def _limpiar(ctx) -> None:
    from app.db.session import db_session
    from app.models.channel import Channel
    from app.models.contact import Contact

    async with db_session() as db:
        for modelo, pk in ((Contact, ctx["contact_id"]), (Channel, ctx["channel_id"])):
            row = await db.get(modelo, pk)
            if row:
                await db.delete(row)
        await db.commit()


# ---------------------------------------------------------------------------


def test_cuando_el_visitante_se_va_se_suelta_la_suscripcion():
    from app.api import webchat as wc
    from app.core import events as bus_mod
    from app.services.channel_sender import webchat_channel

    async def _run():
        await bus_mod.close_event_bus()
        ctx = await _montar_conversacion()
        ws = FakeWebSocket()
        try:
            tarea = asyncio.create_task(
                wc.webchat_ws(
                    ws, str(ctx["conv_id"]), token=ctx["token"], v=ctx["visitor_id"]
                )
            )
            # Espera a que esté suscrito.
            canal = webchat_channel(ctx["conv_id"])
            for _ in range(60):
                await asyncio.sleep(0.05)
                brokers = [b for (_l, b) in bus_mod._brokers.values()]
                if brokers and canal in brokers[0]._subs:
                    break
            assert ws.accepted
            broker = [b for (_l, b) in bus_mod._brokers.values()][0]
            assert canal in broker._subs

            # El visitante cierra la burbuja / cambia de página.
            ws.visitante_se_va()

            # ANTES esto se quedaba colgado para siempre.
            await asyncio.wait_for(tarea, timeout=5)

            await asyncio.sleep(0.1)
            assert canal not in broker._subs, "la suscripción quedó colgando"
        finally:
            await _limpiar(ctx)
            await bus_mod.close_event_bus()

    asyncio.run(_run())


def test_lo_que_publica_el_agente_llega_al_visitante():
    import json

    from app.api import webchat as wc
    from app.core import events as bus_mod
    from app.services.channel_sender import webchat_channel

    async def _run():
        await bus_mod.close_event_bus()
        ctx = await _montar_conversacion()
        ws = FakeWebSocket()
        try:
            tarea = asyncio.create_task(
                wc.webchat_ws(
                    ws, str(ctx["conv_id"]), token=ctx["token"], v=ctx["visitor_id"]
                )
            )
            canal = webchat_channel(ctx["conv_id"])
            for _ in range(60):
                await asyncio.sleep(0.05)
                brokers = [b for (_l, b) in bus_mod._brokers.values()]
                if brokers and canal in brokers[0]._subs:
                    break
            await asyncio.sleep(0.2)

            await bus_mod.event_bus.publish(
                canal, "message.out", {"text": "aquí tienes la respuesta"}
            )
            for _ in range(60):
                if ws.sent:
                    break
                await asyncio.sleep(0.05)

            assert ws.sent, "no llegó nada al visitante"
            payload = json.loads(ws.sent[0])
            assert payload["type"] == "message.out"
            assert payload["payload"]["text"] == "aquí tienes la respuesta"

            ws.visitante_se_va()
            await asyncio.wait_for(tarea, timeout=5)
        finally:
            await _limpiar(ctx)
            await bus_mod.close_event_bus()

    asyncio.run(_run())


def test_si_el_bus_se_cae_el_websocket_no_se_queda_abierto_y_mudo():
    from app.api import webchat as wc
    from app.core import events as bus_mod
    from app.services.channel_sender import webchat_channel

    async def _run():
        await bus_mod.close_event_bus()
        ctx = await _montar_conversacion()
        ws = FakeWebSocket()
        try:
            tarea = asyncio.create_task(
                wc.webchat_ws(
                    ws, str(ctx["conv_id"]), token=ctx["token"], v=ctx["visitor_id"]
                )
            )
            canal = webchat_channel(ctx["conv_id"])
            for _ in range(60):
                await asyncio.sleep(0.05)
                brokers = [b for (_l, b) in bus_mod._brokers.values()]
                if brokers and canal in brokers[0]._subs:
                    break
            broker = [b for (_l, b) in bus_mod._brokers.values()][0]

            # El enlace con Redis muere en silencio (lo detecta el ping).
            broker._release_all()

            # El WebSocket se cierra: el widget reconectará con su backoff.
            await asyncio.wait_for(tarea, timeout=5)
            assert ws.closed_with is not None
        finally:
            await _limpiar(ctx)
            await bus_mod.close_event_bus()

    asyncio.run(_run())


def test_token_invalido_o_caducado_ni_siquiera_acepta():
    from app.api import webchat as wc

    async def _run():
        ctx = await _montar_conversacion()
        try:
            for token in ("", "basura", "v1.a.b.c"):
                ws = FakeWebSocket()
                await wc.webchat_ws(
                    ws, str(ctx["conv_id"]), token=token, v=ctx["visitor_id"]
                )
                assert ws.accepted is False
                assert ws.closed_with == 4401

            caducado, _ = wc._make_session_token(
                ctx["visitor_id"],
                ctx["api_key"],
                now=int(time.time()) - wc.WEBCHAT_SESSION_TTL_SECONDS - 10,
            )
            ws = FakeWebSocket()
            await wc.webchat_ws(
                ws, str(ctx["conv_id"]), token=caducado, v=ctx["visitor_id"]
            )
            assert ws.accepted is False
            assert ws.closed_with == 4401
        finally:
            await _limpiar(ctx)

    asyncio.run(_run())


def test_no_se_puede_escuchar_la_conversacion_de_otro():
    """El token va ligado al visitante: la conversación tiene que ser SUYA."""
    from app.api import webchat as wc

    async def _run():
        a = await _montar_conversacion()
        b = await _montar_conversacion()
        try:
            # Token válido de B apuntando a la conversación de A.
            ws = FakeWebSocket()
            await wc.webchat_ws(ws, str(a["conv_id"]), token=b["token"], v=b["visitor_id"])
            assert ws.accepted is False
            assert ws.closed_with == 4404
        finally:
            await _limpiar(a)
            await _limpiar(b)

    asyncio.run(_run())


def test_conversacion_inexistente_o_id_no_uuid():
    from app.api import webchat as wc

    async def _run():
        ctx = await _montar_conversacion()
        try:
            ws = FakeWebSocket()
            await wc.webchat_ws(ws, "no-soy-un-uuid", token=ctx["token"], v=ctx["visitor_id"])
            assert ws.closed_with == 4400

            ws = FakeWebSocket()
            await wc.webchat_ws(
                ws, str(uuid.uuid4()), token=ctx["token"], v=ctx["visitor_id"]
            )
            assert ws.closed_with == 4404
        finally:
            await _limpiar(ctx)

    asyncio.run(_run())


def test_hay_latido_y_vida_maxima():
    """Sin latido, un enlace muerto deja al visitante viendo 'conectado' sin
    recibir nada. Sin vida máxima, una conexión anónima vive para siempre."""
    from app.api import webchat as wc

    assert 0 < wc.WS_HEARTBEAT_SECONDS <= 60
    assert 0 < wc.WS_MAX_LIFETIME_SECONDS <= 24 * 3600


def test_el_handler_lee_del_socket():
    """Guarda: que nadie vuelva a un `async for` sobre el bus sin leer del
    socket. Es la única forma que da Starlette de detectar la marcha."""
    import inspect

    from app.api import webchat as wc

    src = inspect.getsource(wc.webchat_ws)
    assert "websocket.receive()" in src
