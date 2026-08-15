"""Lo que faltaba en el panel de administración, comprobado por HTTP.

Todo esto son agujeros que se cerraron a la vez y que comparten una misma
forma: el backend YA sabía hacer la cosa, pero no había manera de configurarla
desde el panel. La consecuencia práctica era siempre la misma — tocar la base
de datos a mano o volver a desplegar con otra variable de entorno.

  1. No existía `POST /channels/whatsapp/provision`, así que el canal principal
     del producto nunca aparecía en Conexiones y no se le podía asignar agente.
  2. `agents.kind` (texto/voz) no se exponía: solo se podían crear agentes de
     texto y el de voz había que meterlo por SQL.
  3. La firma del correo se leía de la config del canal y no se podía escribir.
  4. Los dominios permitidos del widget se comprobaban y no se podían editar.
  5. El fragmento del widget solo emitía la clave y la URL base.
  6. La base de conocimiento estaba abierta a cualquier sesión (eso lo cubre
     `test_roles_permisos_http.py`); aquí se comprueba el rastro en auditoría
     del borrado y el cupo de la búsqueda.
  8. `moderation_status()`, el registro de tools y el horario de atención no
     los pintaba ni los editaba nadie.

Por HTTP y no llamando a la función: los permisos y los límites viven en las
DEPENDENCIAS y en los decoradores, y ninguna de las dos cosas se ejecuta si
invocas la función del endpoint a mano.

DB-gated: se salta si no hay Postgres.
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


class _Admin:
    def __init__(self) -> None:
        self.user_id: uuid.UUID | None = None
        self.token = ""
        # Todo lo creado por el test, para dejar la base como estaba.
        self.channel_ids: list[uuid.UUID] = []
        self.agent_ids: list[uuid.UUID] = []
        self.document_ids: list[uuid.UUID] = []


async def _crear_admin() -> _Admin:
    from app.core.security import create_access_token, hash_password
    from app.db.session import db_session
    from app.models.user import User

    a = _Admin()
    suf = uuid.uuid4().hex[:8]
    async with db_session() as db:
        u = User(
            email=f"panel-{suf}@test.local",
            password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
            nombre="Admin Panel",
            role="admin",
        )
        db.add(u)
        await db.commit()
        a.user_id = u.id
    a.token = create_access_token(subject=str(a.user_id), extra={"role": "admin"})
    return a


async def _limpiar(a: _Admin) -> None:
    from sqlalchemy import text

    from app.db.session import db_session

    async with db_session() as db:
        for cid in a.channel_ids:
            await db.execute(text("DELETE FROM channels WHERE id = :i"), {"i": str(cid)})
        for did in a.document_ids:
            await db.execute(text("DELETE FROM documents WHERE id = :i"), {"i": str(did)})
        for aid in a.agent_ids:
            await db.execute(text("DELETE FROM agents WHERE id = :i"), {"i": str(aid)})
        if a.user_id:
            await db.execute(
                text("DELETE FROM audit_log WHERE user_id = :i"), {"i": str(a.user_id)}
            )
            await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(a.user_id)})
        await db.commit()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _client():
    import httpx

    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# ---------------------------------------------------------------------------
# 1 — Alta de WhatsApp desde el panel
# ---------------------------------------------------------------------------


def test_whatsapp_se_puede_dar_de_alta_desde_el_panel():
    """El agujero: Conexiones nunca enseñaba un WhatsApp al que asignar agente.

    Se comprueba lo que de verdad importa: que la fila de canal SE CREA, que
    queda activada, que se le puede colgar un agente y que las credenciales
    acaban CIFRADAS en la tabla de siempre (que es de donde las lee el proveedor
    YCloud). Y que volver a llamar no duplica el canal: hace upsert.
    """
    from sqlalchemy import select, text

    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType
    from app.models.credential import Credential

    claves = ("ycloud_api_key", "ycloud_webhook_secret", "ycloud_phone_number")

    async def _run():
        a = await _crear_admin()
        # El canal de WhatsApp es singleton por tipo: si la base ya traía uno
        # (de otro test o del seed), se aparta y se restaura al final.
        async with db_session() as db:
            previos = (
                await db.execute(
                    select(Channel.id).where(Channel.type == ChannelType.whatsapp)
                )
            ).scalars().all()
            for cid in previos:
                await db.execute(
                    text("UPDATE channels SET type = 'webchat' WHERE id = :i"), {"i": str(cid)}
                )
            # Las credenciales de YCloud son GLOBALES: si este test las deja
            # puestas, otros tests (preflight de difusiones, onboarding) verían
            # un WhatsApp "configurado" que no lo está. Se anota qué había.
            cred_previas = {
                c.key: c.value_encrypted
                for c in (
                    await db.execute(select(Credential).where(Credential.key.in_(claves)))
                ).scalars().all()
            }
            await db.commit()
        try:
            async with _client() as c:
                h = _headers(a.token)

                # Un agente al que asignar el canal.
                r = await c.post(
                    "/api/v1/admin/agents",
                    headers=h,
                    json={
                        "name": f"WA {uuid.uuid4().hex[:6]}",
                        "prompt_system": "Atiendes WhatsApp.",
                        "model_name": "gpt-5.4-mini",
                        "buffer_seconds": 8,
                        "response_split_max_parts": 3,
                        "context_window": 20,
                        "is_active": True,
                    },
                )
                assert r.status_code == 200, r.text
                agent_id = r.json()["id"]
                a.agent_ids.append(uuid.UUID(agent_id))

                # Alta incompleta: tiene que cantar, no crear un canal a medias.
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=h,
                    json={"api_key": "solo-la-clave"},
                )
                assert r.status_code == 400, (
                    f"deja crear un WhatsApp sin secreto ni número → {r.status_code}"
                )

                # Alta completa.
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=h,
                    json={
                        "api_key": "yc-clave-de-prueba-123456",
                        "webhook_secret": "secreto-de-firma-abcdef",
                        "phone_number": "+34600111222",
                        "agent_id": agent_id,
                    },
                )
                assert r.status_code == 200, r.text
                body = r.json()
                channel_id = uuid.UUID(body["channel_id"])
                a.channel_ids.append(channel_id)
                assert body["webhook_url"].endswith("/api/v1/webhooks/ycloud"), body
                assert body["phone_number"] == "+34600111222"
                assert body["agent_id"] == agent_id

                # Y ahora SÍ sale en Conexiones, activo y con su agente.
                r = await c.get("/api/v1/admin/channels", headers=h)
                assert r.status_code == 200, r.text
                wa = [ch for ch in r.json() if ch["type"] == "whatsapp"]
                assert len(wa) == 1, f"el canal de WhatsApp no aparece o está duplicado: {wa}"
                assert wa[0]["enabled"] is True
                assert wa[0]["agent_id"] == agent_id

                # Segunda llamada: upsert, no un canal nuevo.
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=h,
                    json={"phone_number": "+34600999888"},
                )
                assert r.status_code == 200, r.text
                assert uuid.UUID(r.json()["channel_id"]) == channel_id
                r = await c.get("/api/v1/admin/channels", headers=h)
                assert len([ch for ch in r.json() if ch["type"] == "whatsapp"]) == 1

            # Las credenciales, cifradas y legibles por el runtime.
            from app.core.encryption import get_encryption_service

            async with db_session() as db:
                cred = (
                    await db.execute(
                        select(Credential).where(Credential.key == "ycloud_webhook_secret")
                    )
                ).scalar_one_or_none()
                assert cred is not None, "no se guardó el secreto del webhook"
                assert b"secreto-de-firma" not in cred.value_encrypted, (
                    "el secreto está en claro en la base de datos"
                )
                assert (
                    get_encryption_service().decrypt(cred.value_encrypted)
                    == "secreto-de-firma-abcdef"
                )
        finally:
            await _limpiar(a)
            async with db_session() as db:
                for cid in previos:
                    await db.execute(
                        text("UPDATE channels SET type = 'whatsapp' WHERE id = :i"),
                        {"i": str(cid)},
                    )
                # Credenciales como estaban: las que no había, fuera; las que
                # había, con su valor original.
                for key in claves:
                    anterior = cred_previas.get(key)
                    if anterior is None:
                        await db.execute(
                            text("DELETE FROM credentials WHERE key = :k"), {"k": key}
                        )
                    else:
                        await db.execute(
                            text("UPDATE credentials SET value_encrypted = :v WHERE key = :k"),
                            {"v": anterior, "k": key},
                        )
                await db.commit()
            # La caché distribuida de credenciales también se queda con lo del
            # test si no se invalida.
            from app.services.credentials import invalidate_credential_cache

            for key in claves:
                try:
                    await invalidate_credential_cache(key)
                except Exception:
                    pass

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2 — El tipo de agente (texto / voz)
# ---------------------------------------------------------------------------


def test_se_puede_crear_un_agente_de_voz_desde_el_panel():
    """Antes el API no exponía `kind`: solo salían agentes de texto.

    Además se comprueba lo que protege de un despliegue a medias: un PATCH que
    NO manda `kind` conserva el que tuviera. Si el campo tuviera un default duro
    ("text"), el panel viejo convertiría el agente de voz en uno de texto al
    guardar cualquier otro cambio y las llamadas se quedarían sin nadie.
    """

    async def _run():
        a = await _crear_admin()
        try:
            async with _client() as c:
                h = _headers(a.token)
                base = {
                    "name": f"Voz {uuid.uuid4().hex[:6]}",
                    "prompt_system": "Atiendes llamadas telefónicas.",
                    "model_name": "gpt-5.4-nano",
                    "buffer_seconds": 1,
                    "response_split_max_parts": 1,
                    "context_window": 20,
                    "is_active": True,
                }

                r = await c.post("/api/v1/admin/agents", headers=h, json={**base, "kind": "voice"})
                assert r.status_code == 200, r.text
                agent = r.json()
                agent_id = agent["id"]
                a.agent_ids.append(uuid.UUID(agent_id))
                assert agent["kind"] == "voice", (
                    f"el API no devuelve el tipo de agente: {agent.get('kind')!r}"
                )

                # Se lee igual en el detalle y en la lista.
                r = await c.get(f"/api/v1/admin/agents/{agent_id}", headers=h)
                assert r.json()["kind"] == "voice"
                r = await c.get("/api/v1/admin/agents", headers=h)
                fila = next(x for x in r.json() if x["id"] == agent_id)
                assert fila["kind"] == "voice"

                # PATCH sin `kind`: NO lo degrada a texto.
                r = await c.patch(
                    f"/api/v1/admin/agents/{agent_id}",
                    headers=h,
                    json={**base, "name": "Voz renombrada"},
                )
                assert r.status_code == 200, r.text
                assert r.json()["kind"] == "voice", (
                    "guardar sin mandar el tipo convirtió el agente de voz en uno de texto"
                )

                # Y con `kind` sí cambia.
                r = await c.patch(
                    f"/api/v1/admin/agents/{agent_id}", headers=h, json={**base, "kind": "text"}
                )
                assert r.status_code == 200, r.text
                assert r.json()["kind"] == "text"

                # Un valor inventado se rechaza (no se guarda basura en la columna).
                r = await c.patch(
                    f"/api/v1/admin/agents/{agent_id}", headers=h, json={**base, "kind": "sms"}
                )
                assert r.status_code == 422, r.text

                # Sin decir nada al crear, texto (comportamiento de siempre).
                r = await c.post(
                    "/api/v1/admin/agents",
                    headers=h,
                    json={**base, "name": f"Texto {uuid.uuid4().hex[:6]}"},
                )
                assert r.status_code == 200, r.text
                a.agent_ids.append(uuid.UUID(r.json()["id"]))
                assert r.json()["kind"] == "text"
        finally:
            await _limpiar(a)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3 y 4 — Firma de correo y dominios permitidos del widget
# ---------------------------------------------------------------------------


def test_la_firma_del_correo_se_puede_guardar_y_la_lee_el_envio():
    """La firma se leía de la config del canal y no había forma de escribirla.

    No basta con que el PATCH devuelva 200: se comprueba que el que MANDA los
    correos (`channel_sender.get_email_signature`) lee justo eso.
    """
    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType
    from app.services.channel_sender import get_email_signature

    async def _run():
        a = await _crear_admin()
        async with db_session() as db:
            ch = Channel(
                type=ChannelType.email,
                name=f"Email test {uuid.uuid4().hex[:6]}",
                enabled=True,
                config={},
            )
            db.add(ch)
            await db.commit()
            channel_id = ch.id
        a.channel_ids.append(channel_id)
        try:
            async with _client() as c:
                h = _headers(a.token)
                firma = "Laura · Ejemplo\nsoporte@ejemplo.com"
                r = await c.patch(
                    f"/api/v1/admin/channels/{channel_id}",
                    headers=h,
                    json={"email_signature": firma},
                )
                assert r.status_code == 200, r.text
                assert r.json()["config"]["email_signature"] == firma

                assert await get_email_signature() == firma, (
                    "la firma guardada desde el panel no es la que se manda en los correos"
                )

                # Cadena vacía = limpiar (vuelve al valor de la variable de entorno).
                r = await c.patch(
                    f"/api/v1/admin/channels/{channel_id}",
                    headers=h,
                    json={"email_signature": ""},
                )
                assert r.status_code == 200, r.text
                assert "email_signature" not in r.json()["config"]
        finally:
            await _limpiar(a)

    asyncio.run(_run())


def test_los_dominios_del_widget_se_guardan_normalizados_y_frenan_el_uso():
    """El backend ya comprobaba la lista; faltaba poder EDITARLA.

    Se guarda lo que escribe una persona («https://Ejemplo.COM/precios», con
    esquema, mayúsculas y ruta) y se comprueba que queda normalizado —si no, no
    casaría nunca con el host— y que el widget lo aplica de verdad: desde un
    Origin ajeno, 403.
    """
    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType

    async def _run():
        a = await _crear_admin()
        api_key = f"clave-{uuid.uuid4().hex}"
        async with db_session() as db:
            ch = Channel(
                type=ChannelType.webchat,
                name=f"Widget test {uuid.uuid4().hex[:6]}",
                enabled=True,
                config={"api_key": api_key},
            )
            db.add(ch)
            await db.commit()
            channel_id = ch.id
        a.channel_ids.append(channel_id)
        try:
            async with _client() as c:
                h = _headers(a.token)
                r = await c.patch(
                    f"/api/v1/admin/channels/{channel_id}",
                    headers=h,
                    json={
                        "allowed_domains": [
                            "https://Ejemplo.COM/precios",
                            "*.MiWeb.es:8443",
                            "   ",
                        ]
                    },
                )
                assert r.status_code == 200, r.text
                guardados = r.json()["config"]["allowed_domains"]
                assert guardados == ["ejemplo.com", "*.miweb.es"], (
                    f"la lista no se normalizó al guardarla: {guardados}"
                )

                # Y se aplica: el widget desde otro dominio no arranca sesión.
                r = await c.post(
                    "/api/v1/webchat/sessions",
                    json={"api_key": api_key},
                    headers={"Origin": "https://otraweb.com"},
                )
                assert r.status_code == 403, (
                    f"la lista de dominios no frena a un origen ajeno → {r.status_code}"
                )
                # Desde uno de los suyos, sí.
                r = await c.post(
                    "/api/v1/webchat/sessions",
                    json={"api_key": api_key},
                    headers={"Origin": "https://ejemplo.com"},
                )
                assert r.status_code == 200, (
                    f"la lista deja fuera al dominio legítimo: {r.status_code} {r.text[:200]}"
                )

                # Lista vacía = sin restricción (instalaciones que ya existían).
                r = await c.patch(
                    f"/api/v1/admin/channels/{channel_id}",
                    headers=h,
                    json={"allowed_domains": []},
                )
                assert r.status_code == 200, r.text
                assert r.json()["config"]["allowed_domains"] == []
                r = await c.post(
                    "/api/v1/webchat/sessions",
                    json={"api_key": api_key},
                    headers={"Origin": "https://cualquiera.com"},
                )
                assert r.status_code == 200, r.text
        finally:
            await _limpiar(a)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 5 — El fragmento del widget
# ---------------------------------------------------------------------------


def test_el_fragmento_del_widget_trae_todo_lo_que_el_widget_sabe_leer():
    """El cliente se quedaba con un chat titulado «Chat» sin saber que se podía
    cambiar: el fragmento solo emitía la clave y la URL base, aunque widget.js
    lee siete atributos más desde el primer día."""
    from pathlib import Path

    async def _run():
        a = await _crear_admin()
        try:
            async with _client() as c:
                h = _headers(a.token)
                r = await c.post("/api/v1/admin/channels/webchat/provision", headers=h)
                assert r.status_code == 200, r.text
                snippet = r.json()["snippet"]
                api_key = r.json()["api_key"]

                assert f'data-api-key="{api_key}"' in snippet
                for attr in (
                    "data-title",
                    "data-greeting",
                    "data-brand",
                    "data-subtitle",
                    "data-privacy-url",
                    "data-typing-text",
                    "data-closed-text",
                ):
                    assert f"{attr}=" in snippet, (
                        f"el fragmento no menciona {attr}: el cliente no sabe que existe"
                    )

                # El GET del snippet (copiar sin regenerar la clave) da lo mismo.
                r2 = await c.get("/api/v1/admin/channels/webchat/snippet", headers=h)
                assert r2.status_code == 200, r2.text
                assert r2.json()["snippet"] == snippet

            # Ningún atributo inventado: todos los que emitimos los lee widget.js.
            widget = Path("app/static/widget.js").read_text(encoding="utf-8")
            for linea in snippet.splitlines():
                linea = linea.strip()
                if not linea.startswith("data-"):
                    continue
                attr = linea.split("=", 1)[0]
                assert f'getAttribute("{attr}")' in widget, (
                    f"el fragmento emite {attr} y el widget no lo lee"
                )
        finally:
            await _limpiar(a)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 6 — Rastro del borrado en la base de conocimiento
# ---------------------------------------------------------------------------


def test_borrar_un_documento_de_la_kb_deja_rastro_en_auditoria():
    """Borrar es irreversible (se lleva los chunks y el fichero del disco) y no
    dejaba ni una línea: se veía que la KB había menguado, no quién."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.audit_log import AuditLog
    from app.models.document import Document, DocumentFormato, DocumentStatus

    async def _run():
        a = await _crear_admin()
        async with db_session() as db:
            doc = Document(
                nombre=f"manual-{uuid.uuid4().hex[:6]}.txt",
                formato=DocumentFormato.txt,
                tamano_bytes=5,
                status=DocumentStatus.indexado,
                storage_path=None,
            )
            db.add(doc)
            await db.commit()
            doc_id, doc_nombre = doc.id, doc.nombre
        a.document_ids.append(doc_id)
        try:
            async with _client() as c:
                r = await c.delete(
                    f"/api/v1/kb/documents/{doc_id}", headers=_headers(a.token)
                )
                assert r.status_code == 200, r.text

            async with db_session() as db:
                filas = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.user_id == a.user_id,
                            AuditLog.action == "kb.document.deleted",
                        )
                    )
                ).scalars().all()
            assert filas, "borrar un documento de la KB no deja rastro en audit_log"
            assert (filas[0].after or {}).get("nombre") == doc_nombre
        finally:
            await _limpiar(a)

    asyncio.run(_run())


def test_la_busqueda_en_la_kb_tiene_cupo_diario_por_usuario():
    """Cada búsqueda es una llamada de PAGO a embeddings. El cupo tiene que
    cortar ANTES de gastar, no después."""
    from app.api import knowledge_base as kb_api

    async def _run(monkeypatch_limit: int):
        a = await _crear_admin()
        llamadas: list[str] = []

        async def _embed_falso(textos):
            llamadas.append(textos[0])
            return [[0.0] * 1536]

        original_embed = kb_api.embed_texts
        original_limit = kb_api.KB_SEARCH_DAILY_LIMIT_PER_USER
        kb_api.embed_texts = _embed_falso
        kb_api.KB_SEARCH_DAILY_LIMIT_PER_USER = monkeypatch_limit
        try:
            async with _client() as c:
                h = _headers(a.token)
                body = {"query": "horario de apertura"}
                r1 = await c.post("/api/v1/kb/search", headers=h, json=body)
                assert r1.status_code == 200, r1.text
                r2 = await c.post("/api/v1/kb/search", headers=h, json=body)
                assert r2.status_code == 429, (
                    "el cupo diario no corta: una sesión puede quemar el "
                    f"presupuesto de embeddings en bucle (→ {r2.status_code})"
                )
                assert len(llamadas) == 1, (
                    "la petición cortada llegó a llamar a embeddings igualmente"
                )
        finally:
            kb_api.embed_texts = original_embed
            kb_api.KB_SEARCH_DAILY_LIMIT_PER_USER = original_limit
            try:
                from datetime import datetime, timezone

                from app.core.redis import get_redis

                ymd = datetime.now(timezone.utc).strftime("%Y%m%d")
                await get_redis().delete(f"userquota:kb_search:{a.user_id}:{ymd}")
            except Exception:
                pass
            await _limpiar(a)

    asyncio.run(_run(1))


# ---------------------------------------------------------------------------
# 8 — Salud, registro de tools, horario y prompt efectivo
# ---------------------------------------------------------------------------


def test_la_salud_del_panel_dice_si_la_moderacion_esta_operativa():
    """`moderation_status()` existía y no lo consumía nadie: sin clave, TODO
    mensaje entrante pasa sin revisar y el panel no lo decía en ninguna parte."""

    async def _run():
        a = await _crear_admin()
        try:
            async with _client() as c:
                r = await c.get("/api/v1/admin/health", headers=_headers(a.token))
                assert r.status_code == 200, r.text
                salud = r.json()
                assert "moderacion" in salud, (
                    f"la salud del panel no dice nada de la moderación: {sorted(salud)}"
                )
                assert set(salud["moderacion"]) == {"ok", "detail"}
                assert isinstance(salud["moderacion"]["ok"], bool)
                assert salud["moderacion"]["detail"]
        finally:
            await _limpiar(a)

    asyncio.run(_run())


def test_las_tools_se_leen_del_registro_de_verdad():
    """La pantalla de Agentes tenía la lista escrita a mano. Este endpoint la
    saca del registro, que es lo único que el orquestador respeta."""

    async def _run():
        a = await _crear_admin()
        try:
            async with _client() as c:
                r = await c.get("/api/v1/admin/tools", headers=_headers(a.token))
                assert r.status_code == 200, r.text
                tools = r.json()
                assert tools, "el registro de tools llega vacío"
                from app.agents.tools import ALL_TOOLS

                assert {t["name"] for t in tools} == set(ALL_TOOLS), (
                    "lo que devuelve el endpoint no es el registro real"
                )
                assert all(t["description"].strip() for t in tools), (
                    "alguna tool llega sin descripción y no se puede explicar en el panel"
                )
                # Las que el panel tenía escritas a mano siguen estando.
                assert {"consultar_kb", "derivar_humano"} <= {t["name"] for t in tools}
        finally:
            await _limpiar(a)

    asyncio.run(_run())


def test_el_horario_de_atencion_se_edita_desde_el_panel():
    """Sin este endpoint había que tocar la base de datos a mano para que un
    comercio que abre sábados o un negocio en otro huso horario ajustara su
    horario.

    Se comprueba que lo guardado es lo que LEE el agente (mismo parser) y que lo
    inválido se rechaza en vez de corregirse en silencio.
    """
    from app.agents.tools.schedule_config import get_schedule_config

    async def _run():
        a = await _crear_admin()
        previo = None
        try:
            async with _client() as c:
                h = _headers(a.token)
                r = await c.get("/api/v1/admin/settings/calendar", headers=h)
                assert r.status_code == 200, r.text
                previo = r.json()
                assert previo["timezone"], previo
                assert previo["work_days"], previo

                nuevo = {
                    "timezone": "Atlantic/Canary",
                    "work_days": [1, 2, 3, 4, 5, 6],
                    "work_hours": ["16:00-20:00", "09:00-14:00"],
                    "holidays": ["2026-12-25", "2026-01-06"],
                    "slot_step_min": 15,
                    "min_notice_min": 120,
                }
                r = await c.put("/api/v1/admin/settings/calendar", headers=h, json=nuevo)
                assert r.status_code == 200, r.text
                out = r.json()
                assert out["timezone"] == "Atlantic/Canary"
                assert out["work_days"] == [1, 2, 3, 4, 5, 6]
                # Los tramos se devuelven ordenados, no en el orden que llegaron.
                assert out["work_hours"] == ["09:00-14:00", "16:00-20:00"]
                assert out["holidays"] == ["2026-01-06", "2026-12-25"]
                assert out["slot_step_min"] == 15
                assert out["min_notice_min"] == 120

                # Lo que lee el AGENTE es exactamente eso.
                cfg = await get_schedule_config()
                assert cfg.tz_name == "Atlantic/Canary"
                assert cfg.work_days == {1, 2, 3, 4, 5, 6}
                assert cfg.slot_step_min == 15
                assert not cfg.is_working_day(__import__("datetime").date(2026, 12, 25)), (
                    "el festivo guardado no cierra el negocio"
                )

                # Lo inválido se rechaza, no se corrige por lo bajo.
                for malo, que in (
                    ({**nuevo, "timezone": "Marte/Olympus"}, "zona horaria inventada"),
                    ({**nuevo, "work_days": [0, 9]}, "días fuera de 1-7"),
                    ({**nuevo, "work_hours": ["de 9 a 6"]}, "tramo sin formato"),
                    ({**nuevo, "work_hours": ["18:00-09:00"]}, "tramo que acaba antes de empezar"),
                    ({**nuevo, "holidays": ["25/12/2026"]}, "festivo con formato raro"),
                ):
                    r = await c.put("/api/v1/admin/settings/calendar", headers=h, json=malo)
                    assert r.status_code == 422, (
                        f"se acepta un(a) {que} y se corrige en silencio → {r.status_code}"
                    )
        finally:
            if previo:
                async with _client() as c:
                    await c.put(
                        "/api/v1/admin/settings/calendar",
                        headers=_headers(a.token),
                        json={
                            "timezone": previo["timezone"],
                            "work_days": previo["work_days"],
                            "work_hours": previo["work_hours"],
                            "holidays": previo["holidays"],
                            "slot_step_min": previo["slot_step_min"],
                            "min_notice_min": previo["min_notice_min"],
                        },
                    )
            await _limpiar(a)

    asyncio.run(_run())


def test_ver_prompt_efectivo_incluye_el_resumen_rodante_de_la_conversacion():
    """En conversaciones largas, lo que enseñaba el panel NO era lo que recibía
    el modelo: faltaba el resumen rodante, que es justo lo que condensa todo lo
    que ya se salió de la ventana de contexto."""
    from sqlalchemy import text

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal

    async def _run():
        a = await _crear_admin()
        resumen = "El cliente se llama Marta y pregunta por el bono de 10 sesiones."
        contact_id = conv_id = None
        try:
            async with db_session() as db:
                contact = Contact(
                    telefono=f"+34{uuid.uuid4().int % 10**9:09d}", origen=ContactOrigen.whatsapp
                )
                db.add(contact)
                await db.flush()
                conv = Conversation(
                    contact_id=contact.id,
                    canal=ConversationCanal.whatsapp,
                    session_id=f"sess-{uuid.uuid4().hex[:8]}",
                    rolling_summary=resumen,
                    rolling_summary_upto=40,
                )
                db.add(conv)
                await db.commit()
                contact_id, conv_id = contact.id, conv.id

            async with _client() as c:
                h = _headers(a.token)
                r = await c.post(
                    "/api/v1/admin/agents",
                    headers=h,
                    json={
                        "name": f"Largo {uuid.uuid4().hex[:6]}",
                        "prompt_system": "Eres el asistente del centro.",
                        "model_name": "gpt-5.4-mini",
                        "buffer_seconds": 8,
                        "response_split_max_parts": 3,
                        "context_window": 20,
                        "is_active": True,
                    },
                )
                assert r.status_code == 200, r.text
                agent_id = r.json()["id"]
                a.agent_ids.append(uuid.UUID(agent_id))

                # Sin conversación: como siempre, sin resumen.
                r = await c.get(
                    f"/api/v1/admin/agents/{agent_id}/effective-prompt", headers=h
                )
                assert r.status_code == 200, r.text
                assert r.json()["rolling_summary"] == ""
                assert resumen not in r.json()["effective"]

                # Con conversación: el resumen aparece, y detrás del prompt del
                # agente (misma posición que en el runtime).
                r = await c.get(
                    f"/api/v1/admin/agents/{agent_id}/effective-prompt",
                    headers=h,
                    params={"conversation_id": str(conv_id)},
                )
                assert r.status_code == 200, r.text
                out = r.json()
                assert out["rolling_summary"] == resumen
                efectivo = out["effective"]
                assert resumen in efectivo, (
                    "el prompt que enseña el panel sigue sin el resumen rodante"
                )
                assert efectivo.index(out["base"]) < efectivo.index(resumen), (
                    "el resumen no va detrás del prompt del agente, como en el runtime"
                )
                assert "RESUMEN DE LO ANTERIOR" in efectivo, (
                    "falta el envoltorio que marca el resumen como contexto no confiable"
                )

                # Una conversación que no existe: 404, no un prompt a medias.
                r = await c.get(
                    f"/api/v1/admin/agents/{agent_id}/effective-prompt",
                    headers=h,
                    params={"conversation_id": str(uuid.uuid4())},
                )
                assert r.status_code == 404, r.text
        finally:
            async with db_session() as db:
                if conv_id:
                    await db.execute(
                        text("DELETE FROM conversations WHERE id = :i"), {"i": str(conv_id)}
                    )
                if contact_id:
                    await db.execute(
                        text("DELETE FROM contacts WHERE id = :i"), {"i": str(contact_id)}
                    )
                await db.commit()
            await _limpiar(a)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 7 — La máscara de secretos
# ---------------------------------------------------------------------------


def test_la_mascara_no_ensena_medio_secreto():
    """Con `value[:4]…value[-4:]`, una contraseña de correo de 12 caracteres se
    enseñaba a la mitad. Ahora solo los cuatro últimos, como en Proveedores LLM."""
    from app.api.admin import _mask

    # Contraseña corta y realista: nada del principio puede salir.
    clave = "Verano2026!x"
    masked = _mask(clave)
    assert clave[:4] not in masked, f"la máscara sigue enseñando el principio: {masked}"
    assert masked.endswith(clave[-4:])
    assert _mask("") == ""
    # Un valor tan corto que ni los cuatro últimos dicen nada útil: tapado entero.
    assert _mask("abc") == "••••"
