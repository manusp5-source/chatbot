"""Qué puede hacer cada rol, comprobado POR HTTP contra la app real.

Por qué por HTTP y no llamando a la función del endpoint: los permisos viven en
las DEPENDENCIAS (`Depends(require_admin)`), y una dependencia no se ejecuta si
llamas a la función a mano. Un test que invoca `contacts.export_contacts(...)`
pasa en verde con el endpoint completamente abierto. Por eso aquí se monta
`app.main:app` con transporte ASGI de httpx y un JWT de verdad.

El agujero que cubre: el rol `cliente` (operador de bandeja) era casi un admin
sobre los datos de los clientes finales. Con una sesión de `cliente` se podía
descargar el CSV con toda la base de contactos, volcar el histórico completo de
un contacto, archivar 500 conversaciones de golpe, sacar del antispam y borrar
una etiqueta para TODO el mundo. Lo único vetado era borrar contactos y /admin.

DB-gated: se salta si no hay Postgres (en CI sí lo hay).
"""
from __future__ import annotations

import asyncio
import uuid

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


class _Escenario:
    """Datos mínimos para probar permisos: dos usuarios, un contacto, una
    conversación en cuarentena, una etiqueta y una nota."""

    def __init__(self) -> None:
        self.admin_token = ""
        self.cliente_token = ""
        self.contact_id: uuid.UUID | None = None
        self.conv_id: uuid.UUID | None = None
        self.tag_id: uuid.UUID | None = None
        self.ids: list[uuid.UUID] = []


async def _crear_escenario() -> _Escenario:
    from datetime import datetime, timezone

    from app.core.security import create_access_token, hash_password
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.tag import Tag
    from app.models.user import User

    e = _Escenario()
    suf = uuid.uuid4().hex[:8]
    async with db_session() as db:
        admin = User(
            email=f"admin-{suf}@test.local",
            password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
            nombre="Admin Test",
            role="admin",
        )
        cliente = User(
            email=f"operador-{suf}@test.local",
            password_hash=hash_password("OtraClaveLargaDeVerdad2026"),
            nombre="Operador Test",
            role="cliente",
        )
        contact = Contact(telefono=f"+34{uuid.uuid4().int % 10**9:09d}", origen=ContactOrigen.whatsapp)
        tag = Tag(nombre=f"etiqueta-{suf}", color="#3b82f6")
        db.add_all([admin, cliente, contact, tag])
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.whatsapp,
            session_id=f"sess-{suf}",
            status=ConversationStatus.humano,
            quarantined_at=datetime.now(timezone.utc),
            quarantine_reason="spam sospechoso",
        )
        db.add(conv)
        await db.commit()
        e.admin_token = create_access_token(subject=str(admin.id), extra={"role": "admin"})
        e.cliente_token = create_access_token(subject=str(cliente.id), extra={"role": "cliente"})
        e.contact_id, e.conv_id, e.tag_id = contact.id, conv.id, tag.id
        e.ids = [admin.id, cliente.id]
    return e


async def _borrar_escenario(e: _Escenario) -> None:
    from sqlalchemy import text

    from app.db.session import db_session

    async with db_session() as db:
        if e.contact_id:
            await db.execute(text("DELETE FROM contacts WHERE id = :i"), {"i": str(e.contact_id)})
        if e.tag_id:
            await db.execute(text("DELETE FROM tags WHERE id = :i"), {"i": str(e.tag_id)})
        for uid in e.ids:
            await db.execute(text("DELETE FROM audit_log WHERE user_id = :i"), {"i": str(uid)})
            await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(uid)})
        await db.commit()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_el_rol_cliente_no_puede_con_lo_irreversible_lo_masivo_ni_lo_caro():
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                op = _headers(e.cliente_token)
                base = "/api/v1"

                # --- Vetado al operador de bandeja -------------------------
                vetado = [
                    ("GET", f"{base}/contacts/export", None,
                     "el CSV con toda la base de contactos"),
                    ("GET", f"{base}/contacts/{e.contact_id}/export", None,
                     "el volcado RGPD de un contacto (histórico + transcripciones)"),
                    ("DELETE", f"{base}/contacts/{e.contact_id}", None,
                     "el borrado RGPD de un contacto"),
                    ("POST", f"{base}/conversations/bulk-archive", {"ids": [str(e.conv_id)]},
                     "archivar en lote (hasta 500)"),
                    ("POST", f"{base}/conversations/{e.conv_id}/release-quarantine", {},
                     "sacar del antispam y re-disparar el bot"),
                    ("DELETE", f"{base}/tags/{e.tag_id}", None,
                     "borrar una etiqueta para TODAS las conversaciones"),
                    ("GET", f"{base}/admin/users", None, "el área de administración"),
                ]
                for metodo, url, body, que in vetado:
                    r = await c.request(metodo, url, headers=op, json=body)
                    assert r.status_code == 403, (
                        f"un rol 'cliente' puede {que} → {metodo} {url} devolvió {r.status_code}"
                    )

                # --- El día a día de la bandeja SIGUE funcionando ----------
                # El arreglo no vale si deja al operador sin poder trabajar.
                permitido = [
                    ("GET", f"{base}/conversations", None),
                    ("GET", f"{base}/conversations/counts", None),
                    ("GET", f"{base}/contacts", None),
                    ("GET", f"{base}/contacts/{e.contact_id}", None),
                    ("GET", f"{base}/tags", None),
                    ("GET", f"{base}/contacts/{e.contact_id}/notes", None),
                    ("GET", f"{base}/contacts/{e.contact_id}/activity", None),
                    ("POST", f"{base}/conversations/{e.conv_id}/archive", None),
                    ("DELETE", f"{base}/conversations/{e.conv_id}/archive", None),
                    ("POST", f"{base}/contacts/{e.contact_id}/notes", {"texto": "nota de prueba"}),
                ]
                for metodo, url, body in permitido:
                    r = await c.request(metodo, url, headers=op, json=body)
                    assert r.status_code < 400, (
                        f"el operador ya no puede trabajar: {metodo} {url} → "
                        f"{r.status_code} {r.text[:200]}"
                    )
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_el_admin_si_puede_con_todo_eso():
    """El arreglo no puede romper el trabajo legítimo del admin."""
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                ad = _headers(e.admin_token)
                base = "/api/v1"

                r = await c.get(f"{base}/contacts/export", headers=ad)
                assert r.status_code == 200, r.text
                assert "telefono" in r.text.splitlines()[0]

                r = await c.get(f"{base}/contacts/{e.contact_id}/export", headers=ad)
                assert r.status_code == 200, r.text

                r = await c.post(
                    f"{base}/conversations/bulk-archive",
                    headers=ad,
                    json={"ids": [str(e.conv_id)]},
                )
                assert r.status_code == 200, r.text

                r = await c.delete(f"{base}/tags/{e.tag_id}", headers=ad)
                assert r.status_code == 200, r.text
                e.tag_id = None  # ya borrada
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_las_acciones_destructivas_dejan_rastro_en_auditoria():
    """Borrar etiqueta, archivar en lote y sacar de cuarentena se registran.

    Antes no dejaban ni una línea: se sabía que la etiqueta había desaparecido
    de todas las conversaciones, pero no quién se la había llevado por delante.
    """
    import httpx
    from sqlalchemy import select

    from app.db.session import db_session
    from app.main import app
    from app.models.audit_log import AuditLog

    async def _run():
        e = await _crear_escenario()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                ad = _headers(e.admin_token)
                base = "/api/v1"
                assert (
                    await c.post(
                        f"{base}/conversations/{e.conv_id}/release-quarantine", headers=ad
                    )
                ).status_code == 200
                assert (
                    await c.post(
                        f"{base}/conversations/bulk-archive",
                        headers=ad,
                        json={"ids": [str(e.conv_id)]},
                    )
                ).status_code == 200
                assert (await c.delete(f"{base}/tags/{e.tag_id}", headers=ad)).status_code == 200
                e.tag_id = None

                # Nota creada por el admin y borrada por el admin.
                r = await c.post(
                    f"{base}/contacts/{e.contact_id}/notes", headers=ad, json={"texto": "hola"}
                )
                assert r.status_code == 201, r.text
                note_id = r.json()["id"]
                assert (
                    await c.delete(
                        f"{base}/contacts/{e.contact_id}/notes/{note_id}", headers=ad
                    )
                ).status_code == 200

            async with db_session() as db:
                acciones = set(
                    (
                        await db.execute(
                            select(AuditLog.action).where(AuditLog.user_id == e.ids[0])
                        )
                    ).scalars().all()
                )
            for esperada in (
                "tag.deleted",
                "conversation.bulk_archived",
                "conversation.quarantine_released",
                "contact.note_deleted",
            ):
                assert esperada in acciones, (
                    f"la acción destructiva '{esperada}' no deja rastro en audit_log "
                    f"(hay: {sorted(acciones)})"
                )
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_un_adjunto_solo_se_descarga_si_pertenece_a_una_conversacion():
    """I6: antes bastaba con saber el nombre del fichero.

    Los nombres son aleatorios y no se adivinan, pero "no se adivina" no es un
    control de acceso: un fichero suelto en el volumen (subida abortada, resto
    de un contacto medio borrado) se servía a cualquier sesión válida.
    """
    import os

    import httpx

    from app.core.config import settings
    from app.db.session import db_session
    from app.main import app
    from app.models.message import Message, MessageRole

    async def _run():
        e = await _crear_escenario()
        os.makedirs(settings.UPLOADS_PATH, exist_ok=True)
        huerfano = f"{uuid.uuid4().hex}.txt"
        legitimo = f"{uuid.uuid4().hex}.txt"
        for nombre in (huerfano, legitimo):
            with open(os.path.join(settings.UPLOADS_PATH, nombre), "wb") as fh:
                fh.write(b"contenido")
        async with db_session() as db:
            db.add(
                Message(
                    conversation_id=e.conv_id,
                    rol=MessageRole.user,
                    contenido="adjunto",
                    media_url=f"/uploads/{legitimo}",
                )
            )
            await db.commit()

        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                op = _headers(e.cliente_token)
                r = await c.get(f"/api/v1/uploads/{legitimo}", headers=op)
                assert r.status_code == 200, (
                    f"el adjunto de una conversación real ya no se puede ver: {r.status_code}"
                )
                r = await c.get(f"/api/v1/uploads/{huerfano}", headers=op)
                assert r.status_code == 404, (
                    "un fichero que no cuelga de ninguna conversación se sigue "
                    f"descargando con solo saber el nombre (→ {r.status_code})"
                )
                # Sin sesión, ni uno ni otro.
                assert (await c.get(f"/api/v1/uploads/{legitimo}")).status_code == 401
        finally:
            for nombre in (huerfano, legitimo):
                try:
                    os.remove(os.path.join(settings.UPLOADS_PATH, nombre))
                except OSError:
                    pass
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_los_audios_tambien_se_comprueban_contra_la_conversacion():
    """Mismo agujero en `/audios/{filename}` (app/main.py)."""
    import os

    import httpx

    from app.core.config import settings
    from app.db.session import db_session
    from app.main import app
    from app.models.message import Message, MessageRole

    async def _run():
        e = await _crear_escenario()
        os.makedirs(settings.AUDIO_STORAGE_PATH, exist_ok=True)
        huerfano = f"{uuid.uuid4().hex}.ogg"
        legitimo = f"{uuid.uuid4().hex}.ogg"
        for nombre in (huerfano, legitimo):
            with open(os.path.join(settings.AUDIO_STORAGE_PATH, nombre), "wb") as fh:
                fh.write(b"AUDIO")
        async with db_session() as db:
            db.add(
                Message(
                    conversation_id=e.conv_id,
                    rol=MessageRole.user,
                    audio_url=f"/audios/{legitimo}",
                    audio_transcript="hola",
                )
            )
            await db.commit()

        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                op = _headers(e.cliente_token)
                assert (await c.get(f"/audios/{legitimo}", headers=op)).status_code == 200
                r = await c.get(f"/audios/{huerfano}", headers=op)
                assert r.status_code == 404, (
                    "un audio que no cuelga de ninguna conversación se descarga "
                    f"con solo saber el nombre (→ {r.status_code})"
                )
                assert (await c.get(f"/audios/{legitimo}")).status_code == 401
        finally:
            for nombre in (huerfano, legitimo):
                try:
                    os.remove(os.path.join(settings.AUDIO_STORAGE_PATH, nombre))
                except OSError:
                    pass
            await _borrar_escenario(e)

    asyncio.run(_run())


def test_la_base_de_conocimiento_es_de_admin():
    """Arreglado en app/api/knowledge_base.py: leer la KB y buscar en ella son de
    admin. La lectura porque ahí vive lo interno del negocio; la búsqueda porque
    además gasta embeddings de pago en cada petición."""
    import httpx

    from app.main import app

    async def _run():
        e = await _crear_escenario()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                op = _headers(e.cliente_token)
                assert (await c.get("/api/v1/kb/documents", headers=op)).status_code == 403
                r = await c.post("/api/v1/kb/search", headers=op, json={"query": "hola"})
                assert r.status_code == 403
        finally:
            await _borrar_escenario(e)

    asyncio.run(_run())
