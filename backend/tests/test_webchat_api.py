"""Canal web de punta a punta (ASGI real + Postgres). El canal más expuesto del
producto no tenía NI UN test en el repo.

Fallos que cubre (auditoría del canal web):

  B1  — el chat web no tenía historial. No existía ningún endpoint GET: solo
        POST /sessions, POST /messages y el WebSocket. La entrega era Redis
        pub/sub puro, así que si el visitante cerraba la burbuja mientras el
        agente pensaba (buffer + latencia del modelo = 15-20 s), la respuesta
        se publicaba sin suscriptores y se perdía. Y como el identificador de
        conversación SÍ sobrevivía en localStorage, el bot contestaba "como te
        comentaba antes…" a alguien que miraba una pantalla en blanco.

  B5  — desactivar el canal o regenerar la api_key no cortaba nada.

  I7  — cerrar la conversación desde el panel duplicaba el contacto: el widget
        recibía un 409, lo trataba como sesión caducada y borraba también el
        visitor_id, así que el mismo visitante aparecía como dos personas.

  I8  — solo abrir la burbuja ya creaba un contacto en el CRM y una
        conversación vacía. La bandeja y Contactos quedaban inservibles en una
        semana de tráfico normal.

  I16 — `allowed_domains` estaba documentado en el modelo y no existía.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
import uuid
from contextlib import asynccontextmanager

import pytest


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


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


# ---------------------------------------------------------------------------
# Andamiaje
# ---------------------------------------------------------------------------


def _build_app():
    """App mínima con SOLO el canal web (no arrastramos el resto del backend).

    El rate-limit se apaga: aquí interesa la lógica, y los cubos por IP son
    compartidos en Redis entre tests (se probarían unos a otros).
    """
    from fastapi import FastAPI
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from app.api import webchat as wc

    wc.limiter.enabled = False
    app = FastAPI()
    app.state.limiter = wc.limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(wc.router, prefix="/api/v1")
    app.include_router(wc.ws_router, prefix="/api/v1")
    return app


@asynccontextmanager
async def _client():
    import httpx

    from app.core.events import close_event_bus

    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    finally:
        # Cada test abre su propio event loop (asyncio.run): sin esto, el
        # cliente Redis cacheado sobrevive al loop y ensucia la salida con
        # "Event loop is closed" al recogerlo el GC.
        await close_event_bus()


@asynccontextmanager
async def _canal(api_key: str, enabled: bool = True, allowed_domains=None):
    """Canal webchat temporal. Se borra al salir pase lo que pase."""
    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType

    config: dict = {"api_key": api_key}
    if allowed_domains is not None:
        config["allowed_domains"] = allowed_domains
    async with db_session() as db:
        ch = Channel(
            type=ChannelType.webchat,
            name=f"Widget test {api_key[:6]}",
            enabled=enabled,
            config=config,
        )
        db.add(ch)
        await db.commit()
        await db.refresh(ch)
        ch_id = ch.id
    try:
        yield ch_id
    finally:
        async with db_session() as db:
            row = await db.get(Channel, ch_id)
            if row:
                await db.delete(row)
                await db.commit()


async def _set_channel(ch_id, **campos):
    from app.db.session import db_session
    from app.models.channel import Channel

    async with db_session() as db:
        ch = await db.get(Channel, ch_id)
        for k, v in campos.items():
            setattr(ch, k, v)
        await db.commit()


async def _borrar_visitante(visitor_id: str) -> None:
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact

    async with db_session() as db:
        c = (
            await db.execute(
                select(Contact).where(Contact.telefono == f"web:{visitor_id}")
            )
        ).scalar_one_or_none()
        if c:
            await db.delete(c)  # conversaciones y mensajes van en cascada
            await db.commit()


async def _contacto(visitor_id: str):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact

    async with db_session() as db:
        return (
            await db.execute(
                select(Contact).where(Contact.telefono == f"web:{visitor_id}")
            )
        ).scalar_one_or_none()


async def _conversaciones(contact_id):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation

    async with db_session() as db:
        return (
            (
                await db.execute(
                    select(Conversation).where(Conversation.contact_id == contact_id)
                )
            )
            .scalars()
            .all()
        )


@pytest.fixture(autouse=True)
def _sin_pipeline_de_agente(monkeypatch):
    """`POST /messages` encola en el agente. Aquí solo nos importa el canal, y
    además `app.services.conversation` arrastra medio backend: lo sustituimos
    por un doble que solo apunta a quién se llamó."""
    llamadas: list = []

    modulo = types.ModuleType("app.services.conversation")

    async def _handle(message_id):
        llamadas.append(message_id)

    modulo.handle_incoming_message = _handle  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.conversation", modulo)
    return llamadas


async def _abrir_sesion(client, api_key, visitor_id=None, headers=None, **extra):
    body = {"api_key": api_key, "visitor_id": visitor_id, **extra}
    return await client.post("/api/v1/webchat/sessions", json=body, headers=headers or {})


async def _enviar(client, visitor_id, token, text, headers=None):
    return await client.post(
        "/api/v1/webchat/messages",
        json={"visitor_id": visitor_id, "token": token, "text": text},
        headers=headers or {},
    )


async def _historial(client, visitor_id, token, after=None):
    params = {"visitor_id": visitor_id, "token": token}
    if after:
        params["after"] = after
    return await client.get("/api/v1/webchat/history", params=params)


# ---------------------------------------------------------------------------
# I8 — abrir la burbuja no puede crear nada
# ---------------------------------------------------------------------------


def test_abrir_la_burbuja_no_crea_contacto_ni_conversacion():
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            res = await _abrir_sesion(client, api_key)
            assert res.status_code == 200
            data = res.json()
            visitor_id = data["visitor_id"]
            try:
                # Ni ficha en el CRM ni fila vacía en la bandeja.
                assert await _contacto(visitor_id) is None
                assert data["conversation_id"] is None
                assert data["ws_path"] is None
                # Y el historial de un visitante sin mensajes está vacío, no 404.
                h = await _historial(client, visitor_id, data["session_token"])
                assert h.status_code == 200
                assert h.json()["messages"] == []
                assert h.json()["conversation_id"] is None
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


def test_el_primer_mensaje_crea_la_identidad_y_fuera_del_crm(_sin_pipeline_de_agente):
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            s = (await _abrir_sesion(client, api_key)).json()
            visitor_id, token = s["visitor_id"], s["session_token"]
            try:
                res = await _enviar(client, visitor_id, token, "hola, ¿qué tal?")
                assert res.status_code == 200, res.text
                assert res.json()["conversation_id"]
                assert res.json()["ws_path"].startswith("/api/v1/webchat/ws/")

                contacto = await _contacto(visitor_id)
                assert contacto is not None
                # Anónimo → FUERA del CRM, igual que Instagram y voz. Antes se
                # creaba con in_crm=True por el default del modelo.
                assert contacto.in_crm is False
                assert len(await _conversaciones(contacto.id)) == 1
                # Y se encoló en el pipeline del agente.
                assert len(_sin_pipeline_de_agente) == 1
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


def test_si_la_web_da_nombre_o_email_eso_si_es_un_lead():
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            s = (
                await _abrir_sesion(
                    client, api_key, name="Marta", email="marta@ejemplo.com"
                )
            ).json()
            visitor_id = s["visitor_id"]
            try:
                contacto = await _contacto(visitor_id)
                assert contacto is not None
                assert contacto.in_crm is True
                assert contacto.nombre == "Marta"
                # Pero sigue sin haber conversación vacía en la bandeja.
                assert len(await _conversaciones(contacto.id)) == 0
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# B1 — historial
# ---------------------------------------------------------------------------


def test_el_visitante_recupera_su_hilo():
    async def _run():
        from app.db.session import db_session
        from app.models.message import Message, MessageRole

        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            s = (await _abrir_sesion(client, api_key)).json()
            visitor_id, token = s["visitor_id"], s["session_token"]
            try:
                r = await _enviar(client, visitor_id, token, "primera pregunta")
                conv_id = uuid.UUID(r.json()["conversation_id"])

                # La respuesta del agente se persiste como cualquier saliente.
                async with db_session() as db:
                    db.add(
                        Message(
                            conversation_id=conv_id,
                            rol=MessageRole.assistant,
                            contenido="te contesto",
                        )
                    )
                    db.add(
                        Message(
                            conversation_id=conv_id,
                            rol=MessageRole.system,
                            contenido="fontanería interna",
                        )
                    )
                    await db.commit()

                h = (await _historial(client, visitor_id, token)).json()
                assert h["conversation_id"] == str(conv_id)
                textos = [(m["role"], m["text"]) for m in h["messages"]]
                assert textos == [
                    ("user", "primera pregunta"),
                    ("bot", "te contesto"),
                ]
                # El rol `system` NUNCA se le enseña al visitante.
                assert all("fontanería" not in t for _r, t in textos)
                assert h["ws_path"] and str(conv_id) in h["ws_path"]
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


def test_historial_incremental_con_after():
    """Es lo que cierra la ventana entre la foto del historial y la suscripción
    al WebSocket, y lo que recupera lo publicado con la pestaña de fondo."""

    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            s = (await _abrir_sesion(client, api_key)).json()
            visitor_id, token = s["visitor_id"], s["session_token"]
            try:
                await _enviar(client, visitor_id, token, "uno")
                await _enviar(client, visitor_id, token, "dos")
                todo = (await _historial(client, visitor_id, token)).json()["messages"]
                assert [m["text"] for m in todo] == ["uno", "dos"]

                nuevos = (
                    await _historial(client, visitor_id, token, after=todo[0]["id"])
                ).json()["messages"]
                assert [m["text"] for m in nuevos] == ["dos"]

                # Ya al día: nada que traer.
                assert (
                    await _historial(client, visitor_id, token, after=todo[-1]["id"])
                ).json()["messages"] == []
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


def test_el_historial_de_otro_visitante_no_se_puede_leer():
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            a = (await _abrir_sesion(client, api_key)).json()
            b = (await _abrir_sesion(client, api_key)).json()
            try:
                await _enviar(client, a["visitor_id"], a["session_token"], "secreto")
                # El token de B con el visitor_id de A: no cuela.
                r = await _historial(client, a["visitor_id"], b["session_token"])
                assert r.status_code == 401
                # Y con su propio token, B ve lo suyo (nada).
                r = await _historial(client, b["visitor_id"], b["session_token"])
                assert r.status_code == 200
                assert r.json()["messages"] == []
            finally:
                await _borrar_visitante(a["visitor_id"])
                await _borrar_visitante(b["visitor_id"])

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# B5 — apagar el canal y regenerar la clave cortan de verdad
# ---------------------------------------------------------------------------


def test_desactivar_el_canal_corta_las_sesiones_abiertas():
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key) as ch_id, _client() as client:
            s = (await _abrir_sesion(client, api_key)).json()
            visitor_id, token = s["visitor_id"], s["session_token"]
            try:
                assert (await _enviar(client, visitor_id, token, "hola")).status_code == 200

                await _set_channel(ch_id, enabled=False)

                # Antes seguía escribiendo y gastando modelo indefinidamente.
                assert (await _enviar(client, visitor_id, token, "sigo")).status_code == 401
                assert (await _historial(client, visitor_id, token)).status_code == 401
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


def test_regenerar_la_api_key_invalida_las_sesiones_ya_emitidas():
    """Es lo que promete el docstring del endpoint del panel: 'la vieja deja de
    funcionar'. Antes no era verdad para las sesiones ya abiertas."""

    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key) as ch_id, _client() as client:
            s = (await _abrir_sesion(client, api_key)).json()
            visitor_id, token = s["visitor_id"], s["session_token"]
            try:
                assert (await _enviar(client, visitor_id, token, "hola")).status_code == 200

                await _set_channel(ch_id, config={"api_key": f"k-{uuid.uuid4().hex}"})

                assert (await _enviar(client, visitor_id, token, "sigo")).status_code == 401
                # Y la clave vieja tampoco abre sesiones nuevas.
                assert (await _abrir_sesion(client, api_key)).status_code == 401
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


def test_el_token_caduca():
    async def _run():
        from app.api import webchat as wc

        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            visitor_id = str(uuid.uuid4())
            caducado, _exp = wc._make_session_token(
                visitor_id, api_key, now=int(time.time()) - wc.WEBCHAT_SESSION_TTL_SECONDS - 10
            )
            assert (await _enviar(client, visitor_id, caducado, "hola")).status_code == 401
            assert (await _historial(client, visitor_id, caducado)).status_code == 401
            # Y nada se ha creado por el intento.
            assert await _contacto(visitor_id) is None

    asyncio.run(_run())


def test_token_manipulado_no_cuela():
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            s = (await _abrir_sesion(client, api_key)).json()
            vid, token = s["visitor_id"], s["session_token"]
            v, fp, exp, sig = token.split(".")
            alargado = f"{v}.{fp}.{int(exp) + 999999}.{sig}"
            assert (await _enviar(client, vid, alargado, "hola")).status_code == 401
            assert (await _enviar(client, vid, "basura", "hola")).status_code == 401
            assert (await _enviar(client, vid, f"{v}.{fp}.{exp}.0" * 1, "hola")).status_code == 401
            assert await _contacto(vid) is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# I16 — dominios permitidos
# ---------------------------------------------------------------------------


def test_dominios_permitidos_frenan_el_snippet_copiado():
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key, allowed_domains=["ejemplo.com", "*.otro.com"]), _client() as client:
            # Origen autorizado: pasa.
            ok = await _abrir_sesion(
                client, api_key, headers={"Origin": "https://ejemplo.com"}
            )
            assert ok.status_code == 200
            visitor_id, token = ok.json()["visitor_id"], ok.json()["session_token"]
            try:
                assert (
                    await _enviar(
                        client, visitor_id, token, "hola",
                        headers={"Origin": "https://ejemplo.com"},
                    )
                ).status_code == 200

                # Snippet copiado a otra web: 403.
                assert (
                    await _abrir_sesion(
                        client, api_key, headers={"Origin": "https://ladron.com"}
                    )
                ).status_code == 403
                assert (
                    await _enviar(
                        client, visitor_id, token, "gasto tu presupuesto",
                        headers={"Origin": "https://ladron.com"},
                    )
                ).status_code == 403

                # Sin Origin (curl / script): con lista configurada, tampoco.
                assert (await _abrir_sesion(client, api_key)).status_code == 403

                # Referer como respaldo cuando no hay Origin.
                assert (
                    await _abrir_sesion(
                        client, api_key, headers={"Referer": "https://www.otro.com/blog"}
                    )
                ).status_code == 200
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


def test_sin_lista_configurada_se_acepta_cualquier_origen():
    """Compatibilidad con las instalaciones que ya existen."""

    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            r = await _abrir_sesion(
                client, api_key, headers={"Origin": "https://cualquiera.com"}
            )
            assert r.status_code == 200
            await _borrar_visitante(r.json()["visitor_id"])

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# I7 — cerrar desde el panel no puede duplicar el contacto
# ---------------------------------------------------------------------------


def test_cerrar_la_conversacion_no_duplica_el_contacto():
    async def _run():
        from app.db.session import db_session
        from app.models.conversation import Conversation, ConversationStatus

        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            s = (await _abrir_sesion(client, api_key, name="Ana")).json()
            visitor_id, token = s["visitor_id"], s["session_token"]
            try:
                r1 = await _enviar(client, visitor_id, token, "primera consulta")
                conv1 = r1.json()["conversation_id"]

                # La operadora cierra desde el panel.
                async with db_session() as db:
                    conv = await db.get(Conversation, uuid.UUID(conv1))
                    conv.status = ConversationStatus.cerrada
                    await db.commit()

                # El historial sigue enseñándole lo hablado, marcado como cerrada.
                h = (await _historial(client, visitor_id, token)).json()
                assert h["status"] == "cerrada"
                assert [m["text"] for m in h["messages"]] == ["primera consulta"]

                # Escribe otra vez: ya NO recibe un 409 (era lo que disparaba el
                # borrado del visitor_id en el widget). Se abre conversación
                # nueva sobre el MISMO contacto.
                r2 = await _enviar(client, visitor_id, token, "otra cosa")
                assert r2.status_code == 200
                assert r2.json()["conversation_id"] != conv1

                contacto = await _contacto(visitor_id)
                assert contacto.nombre == "Ana"  # no se pierde la identidad
                convs = await _conversaciones(contacto.id)
                assert len(convs) == 2  # dos conversaciones, UN contacto
            finally:
                await _borrar_visitante(visitor_id)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Validación de entrada (lo que ya estaba bien y no puede romperse)
# ---------------------------------------------------------------------------


def test_visitor_id_no_uuid_da_422_y_no_500():
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            r = await _abrir_sesion(client, api_key, visitor_id="../../etc/passwd")
            assert r.status_code == 422
            r = await _enviar(client, "x" * 60, "v1.a.b.c", "hola")
            assert r.status_code == 422

    asyncio.run(_run())


def test_mensaje_vacio_o_kilometrico():
    async def _run():
        api_key = f"k-{uuid.uuid4().hex}"
        async with _canal(api_key), _client() as client:
            s = (await _abrir_sesion(client, api_key)).json()
            vid, token = s["visitor_id"], s["session_token"]
            try:
                assert (await _enviar(client, vid, token, "   ")).status_code == 400
                assert (await _enviar(client, vid, token, "x" * 4001)).status_code == 400
                # Y nada de eso creó identidad.
                assert await _contacto(vid) is None
            finally:
                await _borrar_visitante(vid)

    asyncio.run(_run())


def test_api_key_desconocida_no_abre_sesion():
    async def _run():
        async with _client() as client:
            r = await _abrir_sesion(client, f"inventada-{uuid.uuid4().hex}")
            assert r.status_code == 401

    asyncio.run(_run())
