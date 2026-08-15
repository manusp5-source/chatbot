"""Descarga de adjuntos de correo desde el panel.

Antes no existía ningún endpoint: el proveedor de Gmail ya guardaba la ficha de
cada adjunto (nombre, tipo, tamaño y el `attachment_id` que pide la API), pero
la operadora tenía que salirse a Gmail para abrir lo que le mandaba un cliente.

Se prueba POR HTTP contra la app real (como test_roles_permisos_http): la
comprobación de propiedad y la autenticación viven en las dependencias, y un
test que llama a la función a mano pasa en verde con el endpoint abierto.

Lo que cubre:
  - descarga correcta: bytes, tipo y nombre de fichero (también con tildes).
  - propiedad: un mensaje de OTRA conversación no se descarga.
  - un `attachment_id` que no está en la ficha de ESE mensaje → 404 (si no,
    valdría para bajar ficheros de cualquier otro correo del buzón).
  - correos ingeridos ANTES de que se guardara la ficha (`attachments` es una
    lista de CADENAS): 409 explicando qué hacer, nunca un 500.
  - Gmail caído o sin credenciales → 502.
  - el esquema de salida del mensaje expone los adjuntos con su bandera de
    descargable, que es de lo que tira el panel para pintar el enlace.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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

FICHA_PDF = {
    "filename": "presupuesto añejo.pdf",
    "mime_type": "application/pdf",
    "size": 1234,
    "attachment_id": "ANGjdJ-adjunto-1",
    "part_id": "1",
    "content_id": None,
    "inline": False,
}


class _Escenario:
    def __init__(self) -> None:
        self.token = ""
        self.user_id: uuid.UUID | None = None
        self.contact_id: uuid.UUID | None = None
        self.conv_id: uuid.UUID | None = None
        self.otra_conv_id: uuid.UUID | None = None
        self.msg_id: uuid.UUID | None = None
        self.msg_legacy_id: uuid.UUID | None = None


async def _crear_escenario() -> _Escenario:
    """Operador + contacto + dos conversaciones de email; en la primera, un
    mensaje con ficha completa de adjunto y otro con el formato viejo."""
    from app.core.security import create_access_token, hash_password
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.models.user import User

    e = _Escenario()
    suf = uuid.uuid4().hex[:8]
    async with db_session() as db:
        user = User(
            email=f"adj-{suf}@test.local",
            password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
            nombre="Operador Adjuntos",
            role="cliente",
        )
        contact = Contact(
            telefono=f"email:adj-{suf}@cliente.com",
            email=f"adj-{suf}@cliente.com",
            origen=ContactOrigen.email,
            in_crm=False,
        )
        db.add_all([user, contact])
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.email,
            session_id=f"sess-{suf}",
            status=ConversationStatus.humano,
        )
        otra = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.email,
            session_id=f"sess-otra-{suf}",
            status=ConversationStatus.humano,
        )
        db.add_all([conv, otra])
        await db.flush()
        msg = Message(
            conversation_id=conv.id,
            rol=MessageRole.user,
            contenido="Te mando el presupuesto.\n\n[adjunto: presupuesto añejo.pdf]",
            extra={
                "provider_message_id": "gmail-msg-1",
                "attachments": [FICHA_PDF],
                # El logo de la firma va aparte y NO se ofrece como adjunto.
                "inline_attachments": [
                    {"filename": "logo.png", "attachment_id": "logo-1", "inline": True}
                ],
            },
        )
        legacy = Message(
            conversation_id=conv.id,
            rol=MessageRole.user,
            contenido="Correo viejo con adjunto.",
            extra={
                "provider_message_id": "gmail-msg-viejo",
                # Formato ANTIGUO: solo el nombre, sin nada con lo que pedirlo.
                "attachments": ["contrato.pdf"],
            },
        )
        db.add_all([msg, legacy])
        await db.commit()
        e.user_id, e.contact_id = user.id, contact.id
        e.conv_id, e.otra_conv_id = conv.id, otra.id
        e.msg_id, e.msg_legacy_id = msg.id, legacy.id
    e.token = create_access_token(subject=str(e.user_id), extra={"role": "cliente"})
    return e


async def _borrar_escenario(e: _Escenario) -> None:
    from sqlalchemy import text

    from app.db.session import db_session

    async with db_session() as db:
        if e.contact_id:
            await db.execute(
                text("DELETE FROM contacts WHERE id = :i"), {"i": str(e.contact_id)}
            )
        if e.user_id:
            await db.execute(
                text("DELETE FROM users WHERE id = :i"), {"i": str(e.user_id)}
            )
        await db.commit()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _url(conv_id, msg_id, attachment_id: str) -> str:
    return (
        f"/api/v1/conversations/{conv_id}/messages/{msg_id}"
        f"/attachments/{attachment_id}"
    )


def _fake_gmail(**kwargs) -> MagicMock:
    fake = MagicMock()
    fake.get_attachment = AsyncMock(**kwargs)
    return fake


def test_la_operadora_descarga_el_adjunto_sin_salir_del_panel():
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        gmail = _fake_gmail(return_value=b"%PDF-1.4 contenido binario")
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                with patch(
                    "app.providers.gmail.get_gmail_provider", return_value=gmail
                ):
                    r = await c.get(
                        _url(e.conv_id, e.msg_id, FICHA_PDF["attachment_id"]),
                        headers=_headers(e.token),
                    )
            assert r.status_code == 200, r.text
            assert r.content == b"%PDF-1.4 contenido binario"
            assert r.headers["content-type"].startswith("application/pdf")
            disp = r.headers["content-disposition"]
            # Nombre en las dos formas (RFC 6266): ASCII saneado + UTF-8 real.
            assert disp.startswith("attachment;")
            assert "presupuesto a_ejo.pdf" in disp
            assert "filename*=UTF-8''presupuesto%20a%C3%B1ejo.pdf" in disp
            gmail.get_attachment.assert_awaited_once_with(
                "gmail-msg-1", FICHA_PDF["attachment_id"]
            )
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_sin_sesion_no_se_descarga_nada():
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.get(_url(e.conv_id, e.msg_id, FICHA_PDF["attachment_id"]))
            assert r.status_code in (401, 403), r.text
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_el_mensaje_tiene_que_ser_de_esa_conversacion():
    """Misma comprobación de propiedad que `recover`: si el mensaje no cuelga de
    la conversación de la URL, no se descarga (ni se llama a Gmail)."""
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        gmail = _fake_gmail(return_value=b"no deberia llegar aqui")
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                with patch(
                    "app.providers.gmail.get_gmail_provider", return_value=gmail
                ):
                    # Mensaje real, pero colgado de OTRA conversación.
                    r = await c.get(
                        _url(e.otra_conv_id, e.msg_id, FICHA_PDF["attachment_id"]),
                        headers=_headers(e.token),
                    )
                    assert r.status_code == 404, r.text

                    # Conversación que no existe.
                    r = await c.get(
                        _url(uuid.uuid4(), e.msg_id, FICHA_PDF["attachment_id"]),
                        headers=_headers(e.token),
                    )
                    assert r.status_code == 404, r.text
            gmail.get_attachment.assert_not_called()
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_un_id_de_adjunto_que_no_es_de_ese_mensaje_no_vale():
    """El id tiene que estar en la ficha DE ESE mensaje. Si no, bastaría con un
    id suelto para bajarse ficheros de cualquier otro correo del buzón."""
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        gmail = _fake_gmail(return_value=b"no deberia llegar aqui")
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                with patch(
                    "app.providers.gmail.get_gmail_provider", return_value=gmail
                ):
                    r = await c.get(
                        _url(e.conv_id, e.msg_id, "id-de-otro-correo"),
                        headers=_headers(e.token),
                    )
                    assert r.status_code == 404, r.text

                    # El logo incrustado de la firma tampoco es un adjunto.
                    r = await c.get(
                        _url(e.conv_id, e.msg_id, "logo-1"),
                        headers=_headers(e.token),
                    )
                    assert r.status_code == 404, r.text
            gmail.get_attachment.assert_not_called()
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_los_correos_viejos_explican_que_hay_que_releerlos_de_gmail():
    """Ingesta anterior al cambio: `attachments` es una lista de CADENAS, sin
    `attachment_id`. No es un fallo del servidor (500): es un 409 que dice qué
    hacer."""
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        gmail = _fake_gmail(return_value=b"no deberia llegar aqui")
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                with patch(
                    "app.providers.gmail.get_gmail_provider", return_value=gmail
                ):
                    r = await c.get(
                        _url(e.conv_id, e.msg_legacy_id, "contrato.pdf"),
                        headers=_headers(e.token),
                    )
            assert r.status_code == 409, r.text
            detalle = r.json()["detail"]
            assert "Gmail" in detalle
            gmail.get_attachment.assert_not_called()
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_si_gmail_no_responde_se_dice_claro_y_no_revienta():
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                # Gmail lanza.
                gmail = _fake_gmail(side_effect=RuntimeError("500 de Gmail"))
                with patch(
                    "app.providers.gmail.get_gmail_provider", return_value=gmail
                ):
                    r = await c.get(
                        _url(e.conv_id, e.msg_id, FICHA_PDF["attachment_id"]),
                        headers=_headers(e.token),
                    )
                assert r.status_code == 502, r.text

                # Sin credenciales de Gmail el cliente devuelve None.
                gmail = _fake_gmail(return_value=None)
                with patch(
                    "app.providers.gmail.get_gmail_provider", return_value=gmail
                ):
                    r = await c.get(
                        _url(e.conv_id, e.msg_id, FICHA_PDF["attachment_id"]),
                        headers=_headers(e.token),
                    )
                assert r.status_code == 502, r.text
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_el_panel_recibe_los_adjuntos_en_el_mensaje():
    """Sin esto el panel no tiene con qué pintar el enlace de descarga."""
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.get(
                    f"/api/v1/conversations/{e.conv_id}/messages",
                    headers=_headers(e.token),
                )
            assert r.status_code == 200, r.text
            por_id = {m["id"]: m for m in r.json()}

            nuevo = por_id[str(e.msg_id)]["attachments"]
            assert len(nuevo) == 1, "el logo incrustado NO cuenta como adjunto"
            assert nuevo[0]["filename"] == "presupuesto añejo.pdf"
            assert nuevo[0]["mime_type"] == "application/pdf"
            assert nuevo[0]["size"] == 1234
            assert nuevo[0]["attachment_id"] == FICHA_PDF["attachment_id"]
            assert nuevo[0]["downloadable"] is True

            viejo = por_id[str(e.msg_legacy_id)]["attachments"]
            assert viejo[0]["filename"] == "contrato.pdf"
            assert viejo[0]["attachment_id"] is None
            assert viejo[0]["downloadable"] is False
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_un_mensaje_sin_adjuntos_devuelve_lista_vacia():
    """Unitario del normalizador: nada de reventar con extras raros."""
    from app.api.conversations import _attachments_out

    assert _attachments_out({}) == []
    assert _attachments_out({"attachments": None}) == []
    # Basura en el extra: se ignora en vez de tumbar la bandeja entera.
    assert _attachments_out({"attachments": [42, None]}) == []
