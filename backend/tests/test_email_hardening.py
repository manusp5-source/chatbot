"""Endurecimiento del canal Email + cerrojo del agente (E1–E11).

Cada test reproduce el fallo tal y como se verificó en la auditoría:

  E1  Un correo que revienta al procesarse ya no se pierde: va a una cola de
      reintentos persistente, y los datos externos se recortan a lo que cabe en
      la columna en vez de tumbar el INSERT.
  E2  Dos hilos del mismo remitente ya no comparten buffer.
  E3  Una cuenta de Google desconectada deja constancia visible (y se marca
      inactiva si el fallo es definitivo).
  E4  La sincronización incremental ya no mete SPAM ni Promociones.
  E5  Retención 0 = no purgar; mínimo con suelo; los borradores pendientes no
      se vacían.
  E6  Adjunto real vs logo incrustado de una firma, con datos para descargarlo.
  E7  El recorte de longitud ya no se come la pregunta del bottom-posting.
  E8  Los correos salen con firma.
  E9  Liberar de cuarentena ya no desactiva el filtro de correo automático.
  E10 Dos canales de email activos ya no dejan el sondeo muerto.
  E11 Se responde al remitente real del hilo, no al email del CRM.
  +   Cerrojo por conversación y respuesta vacía del modelo.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _db_available() -> bool:
    try:
        import asyncio

        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("select 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


class _FakeRedis:
    """Redis mínimo en memoria (SET NX/EX, GET, DEL, EVAL del script de suelta)."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        return self.store.pop(key, None) is not None

    async def eval(self, _script, _n, key, value):
        if self.store.get(key) == value:
            del self.store[key]
            return 1
        return 0


# ---------------------------------------------------------------------------
# E6 — adjuntos reales vs contenido incrustado
# ---------------------------------------------------------------------------


def _msg_con_firma_y_adjunto():
    """Correo con firma corporativa: un logo incrustado + un PDF de verdad."""
    import base64

    def b64(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

    return {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Ana <ana@example.com>"},
                {"name": "To", "value": "negocio@gmail.com"},
                {"name": "Subject", "value": "Presupuesto"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("Te paso el presupuesto.")}},
                {
                    "mimeType": "image/png",
                    "filename": "logo-firma.png",
                    "headers": [
                        {"name": "Content-Disposition", "value": 'inline; filename="logo-firma.png"'},
                        {"name": "Content-ID", "value": "<logo123>"},
                    ],
                    "body": {"attachmentId": "att_logo", "size": 4096},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "presupuesto.pdf",
                    "headers": [
                        {"name": "Content-Disposition", "value": 'attachment; filename="presupuesto.pdf"'},
                    ],
                    "body": {"attachmentId": "att_pdf", "size": 90210},
                },
            ],
        },
    }


def test_e6_logo_de_firma_no_cuenta_como_adjunto():
    from app.providers.gmail.client import parse_get_message_result

    parsed = parse_get_message_result(_msg_con_firma_y_adjunto())

    nombres = [a["filename"] for a in parsed["attachments"]]
    assert nombres == ["presupuesto.pdf"]
    assert [a["filename"] for a in parsed["inline_attachments"]] == ["logo-firma.png"]
    # El agente ya no lee "[adjunto: logo-firma.png]".
    assert "[adjunto: presupuesto.pdf]" in parsed["body"]
    assert "logo-firma.png" not in parsed["body"]


def test_e6_el_adjunto_trae_lo_necesario_para_descargarlo():
    from app.providers.gmail.client import parse_get_message_result

    pdf = parse_get_message_result(_msg_con_firma_y_adjunto())["attachments"][0]
    assert pdf["attachment_id"] == "att_pdf"
    assert pdf["mime_type"] == "application/pdf"
    assert pdf["size"] == 90210
    assert pdf["inline"] is False


def test_e6_un_pdf_con_content_id_sigue_siendo_adjunto():
    """`Content-Disposition: attachment` manda sobre la presencia de Content-ID."""
    from app.providers.gmail.client import _part_is_inline

    assert (
        _part_is_inline(
            {"content-disposition": "attachment; filename=x.pdf", "content-id": "<x>"}
        )
        is False
    )
    assert _part_is_inline({"content-id": "<logo>"}) is True
    assert _part_is_inline({}) is False


@pytest.mark.asyncio
async def test_e6_get_attachment_pide_el_binario_a_gmail():
    import base64

    from app.providers.gmail.client import GmailClient

    contenido = b"%PDF-1.4 fake"
    respuesta = MagicMock()
    respuesta.status_code = 200
    respuesta.json = MagicMock(
        return_value={"data": base64.urlsafe_b64encode(contenido).decode().rstrip("=")}
    )
    cliente_http = MagicMock()
    cliente_http.get = AsyncMock(return_value=respuesta)
    cliente_http.__aenter__ = AsyncMock(return_value=cliente_http)
    cliente_http.__aexit__ = AsyncMock(return_value=False)

    gmail = GmailClient()
    with patch.object(GmailClient, "_headers", AsyncMock(return_value={"Authorization": "x"})), \
        patch("httpx.AsyncClient", return_value=cliente_http):
        data = await gmail.get_attachment("m1", "att_pdf")

    assert data == contenido
    assert "attachments/att_pdf" in cliente_http.get.await_args.args[0]


# ---------------------------------------------------------------------------
# E7 — el recorte no se puede comer la pregunta real
# ---------------------------------------------------------------------------


def test_e7_bottom_posting_la_pregunta_sobrevive_al_recorte():
    """Correo de 5.555 caracteres con la respuesta DEBAJO de la cita (Outlook).

    Con el recorte de antes (`texto[:4000]`) la pregunta real no llegaba nunca
    al modelo.
    """
    from app.providers.gmail.client import prepare_email_text_for_agent

    cita = "\n".join(f"> línea citada número {i} con relleno de sobra" for i in range(120))
    correo = (
        "El 3 jun 2026, a las 10:00, Soporte <soporte@x> escribió:\n"
        + cita
        + "\n\nPerfecto, ¿me lo podéis enviar el martes?\n"
    )
    assert len(correo) > 5000

    preparado = prepare_email_text_for_agent(correo, limit=4000)
    assert "¿me lo podéis enviar el martes?" in preparado
    assert len(preparado) <= 4000


def test_e7_top_posting_se_queda_solo_con_lo_nuevo():
    from app.providers.gmail.client import prepare_email_text_for_agent

    correo = (
        "Sí, adelante con el pedido.\n\n"
        "-- \nJuan Pérez\nDirector\n\n"
        "El 3 jun 2026, Soporte escribió:\n> texto anterior larguísimo\n"
    )
    preparado = prepare_email_text_for_agent(correo)
    assert preparado.strip() == "Sí, adelante con el pedido."


def test_e7_los_adjuntos_no_se_pierden_al_limpiar():
    from app.providers.gmail.client import prepare_email_text_for_agent

    correo = "Aquí va el plano.\n-- \nFirma\n\n[adjunto: plano.pdf]"
    preparado = prepare_email_text_for_agent(correo)
    assert "Aquí va el plano." in preparado
    assert "[adjunto: plano.pdf]" in preparado


def test_e7_el_recorte_conserva_principio_y_final():
    from app.providers.gmail.client import truncate_email_for_agent

    texto = "INICIO" + ("x" * 5000) + "FINAL"
    recortado = truncate_email_for_agent(texto, 1000)
    assert recortado.startswith("INICIO")
    assert recortado.endswith("FINAL")
    assert len(recortado) <= 1000


@pytestmark_db
@pytest.mark.asyncio
async def test_e7_dos_correos_en_la_misma_rafaga_no_se_pisan():
    """La limpieza va mensaje a mensaje. Si se hiciera sobre el texto ya unido,
    el corte por cita del PRIMER correo se llevaría por delante el segundo."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.providers.whatsapp.base import IncomingMessage
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages, store_incoming
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    remitente = f"raf-{uuid.uuid4().hex[:8]}@example.com"
    hilo = f"thr-{uuid.uuid4().hex[:8]}"

    def _correo(texto):
        return IncomingMessage(
            provider_message_id=f"m-{uuid.uuid4().hex}",
            from_phone=f"email:{remitente}",
            to_phone="email:negocio@gmail.com",
            message_type="text",
            text=texto,
            customer_name="Ana",
            raw={"threadId": hilo, "subject": "Hilo", "rfc822_message_id": f"<{hilo}@x>"},
        )

    id1 = await store_incoming(
        _correo("PRIMERA PREGUNTA sobre el plazo.\n\nEl 3 jun 2026, Soporte escribió:\n> bla")
    )
    id2 = await store_incoming(_correo("SEGUNDA PREGUNTA sobre el precio."))
    assert id1 and id2

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.gmail_thread_id == hilo))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.spam_reviewed = True
        conv_id = conv.id
        await db.commit()

    clave = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(clave, str(id1))
    await message_buffer.push_message(clave, str(id2))

    visto: dict = {}

    async def fake_agent(**kw):
        visto["user_message"] = kw["user_message"]
        return "ok"

    with patch("app.services.conversation.run_agent", AsyncMock(side_effect=fake_agent)), \
        patch("app.services.channel_sender.create_email_draft_for_conversation", AsyncMock(return_value={"draft_id": "d", "message_id": "m"})), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch("app.services.conversation.moderate", AsyncMock(return_value=SimpleNamespace(flagged=False, categories=[]))), \
        patch("app.services.conversation.can_call_llm", AsyncMock(return_value=True)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(clave)

    texto = visto["user_message"]
    assert "PRIMERA PREGUNTA" in texto
    assert "SEGUNDA PREGUNTA" in texto
    assert "bla" not in texto  # la cita sí se va


def test_e7_nunca_devuelve_vacio():
    from app.providers.gmail.client import prepare_email_text_for_agent

    solo_cita = "> todo citado\n> nada más"
    assert prepare_email_text_for_agent(solo_cita).strip()


# ---------------------------------------------------------------------------
# E4 — el incremental no puede meter spam ni promociones
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "labels,esperado",
    [
        ({"INBOX"}, True),
        ({"INBOX", "UNREAD", "IMPORTANT"}, True),
        ({"SPAM"}, False),
        ({"INBOX", "SPAM"}, False),
        ({"TRASH"}, False),
        ({"INBOX", "CATEGORY_PROMOTIONS"}, False),
        ({"CATEGORY_SOCIAL"}, False),  # sin INBOX
        (set(), True),  # sin información: no descartamos
    ],
)
def test_e4_filtro_de_etiquetas_del_entrante(labels, esperado):
    from app.services.gmail_ingest import _is_relevant_inbound

    assert _is_relevant_inbound(labels) is esperado


@pytest.mark.asyncio
async def test_e4_un_spam_del_historial_no_crea_conversacion():
    import app.services.gmail_ingest as ingest

    def _norm(mid, labels):
        return {
            "id": mid,
            "threadId": f"t-{mid}",
            "from_addr": "promo@marketing.example",
            "from_name": "Promo",
            "to_addr": "negocio@gmail.com",
            "subject": "OFERTA",
            "body": "compra ya",
            "label_ids": labels,
        }

    mensajes = {
        "spam1": _norm("spam1", ["SPAM"]),
        "promo1": _norm("promo1", ["INBOX", "CATEGORY_PROMOTIONS"]),
        "bueno1": _norm("bueno1", ["INBOX"]),
    }
    fake_gmail = MagicMock()
    fake_gmail.get_message = AsyncMock(side_effect=lambda mid: mensajes[mid])
    store = AsyncMock(return_value=uuid.uuid4())

    with patch.object(ingest, "get_gmail_provider", return_value=fake_gmail), \
        patch.object(ingest, "store_incoming", store), \
        patch.object(ingest, "task_process", MagicMock()):
        stored, failed = await ingest._process_message_ids(
            ["spam1", "promo1", "bueno1"], "negocio@gmail.com"
        )

    assert stored == 1
    assert failed == {}
    assert store.await_count == 1


# ---------------------------------------------------------------------------
# E1 — cola de reintentos + recorte de datos externos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e1_un_correo_que_falla_va_a_la_cola_de_reintentos():
    """El bueno se guarda, el malo NO se pierde: queda encolado para reintento."""
    import app.services.gmail_ingest as ingest

    canal = SimpleNamespace(id=uuid.uuid4(), config={"last_history_id": "500"})

    fake_gmail = MagicMock()
    fake_gmail.get_profile = AsyncMock(
        return_value={"email_address": "negocio@gmail.com", "history_id": "900"}
    )
    fake_gmail.list_history = AsyncMock(
        return_value={"message_ids": ["ok1", "malo1"], "history_id": "900", "expired": False}
    )
    fake_gmail.get_message = AsyncMock(
        side_effect=lambda mid: {
            "id": mid,
            "threadId": f"t-{mid}",
            "from_addr": "cliente@example.com",
            "to_addr": "negocio@gmail.com",
            "body": "hola",
            "label_ids": ["INBOX"],
        }
    )

    async def store(incoming):
        if incoming.provider_message_id == "malo1":
            raise ValueError("value too long for type character varying(120)")
        return uuid.uuid4()

    guardado: dict = {}

    async def fake_save(channel_id, history_id, account_email=None, retry_queue=None):
        guardado["history_id"] = history_id
        guardado["retry_queue"] = retry_queue

    with patch.object(ingest, "_load_email_channel", AsyncMock(return_value=canal)), \
        patch.object(ingest, "get_gmail_provider", return_value=fake_gmail), \
        patch.object(ingest, "get_redis", return_value=_FakeRedis()), \
        patch.object(ingest, "store_incoming", side_effect=store), \
        patch.object(ingest, "task_process", MagicMock()), \
        patch.object(ingest, "_save_history_id", side_effect=fake_save):
        resultado = await ingest.sync_incoming_emails()

    assert resultado["stored"] == 1
    # El ancla avanza igual (Gmail no permite otra cosa)…
    assert guardado["history_id"] == "900"
    # …pero el correo malo NO se ha perdido.
    assert [e["id"] for e in guardado["retry_queue"]] == ["malo1"]
    assert guardado["retry_queue"][0]["attempts"] == 1


@pytest.mark.asyncio
async def test_e1_el_reintento_recupera_el_correo_y_lo_saca_de_la_cola():
    import app.services.gmail_ingest as ingest

    canal = SimpleNamespace(
        id=uuid.uuid4(),
        config={
            "last_history_id": "900",
            "ingest_retry_queue": [{"id": "malo1", "attempts": 1, "last_error": "x"}],
        },
    )
    fake_gmail = MagicMock()
    fake_gmail.get_profile = AsyncMock(
        return_value={"email_address": "negocio@gmail.com", "history_id": "950"}
    )
    fake_gmail.list_history = AsyncMock(
        return_value={"message_ids": [], "history_id": "950", "expired": False}
    )
    fake_gmail.get_message = AsyncMock(
        return_value={
            "id": "malo1",
            "threadId": "t-malo1",
            "from_addr": "cliente@example.com",
            "to_addr": "negocio@gmail.com",
            "body": "hola",
            "label_ids": ["INBOX"],
        }
    )
    guardado: dict = {}

    async def fake_save(channel_id, history_id, account_email=None, retry_queue=None):
        guardado["retry_queue"] = retry_queue

    with patch.object(ingest, "_load_email_channel", AsyncMock(return_value=canal)), \
        patch.object(ingest, "get_gmail_provider", return_value=fake_gmail), \
        patch.object(ingest, "get_redis", return_value=_FakeRedis()), \
        patch.object(ingest, "store_incoming", AsyncMock(return_value=uuid.uuid4())), \
        patch.object(ingest, "task_process", MagicMock()), \
        patch.object(ingest, "_save_history_id", side_effect=fake_save):
        resultado = await ingest.sync_incoming_emails()

    assert resultado["stored"] == 1
    assert guardado["retry_queue"] == []


@pytest.mark.asyncio
async def test_e1_al_agotar_los_intentos_se_avisa_y_se_descarta():
    import app.services.gmail_ingest as ingest
    from app.core.config import settings

    previa = [{"id": "malo1", "attempts": settings.GMAIL_INGEST_MAX_ATTEMPTS - 1}]
    logs: list = []

    async def fake_log(**kwargs):
        logs.append(kwargs)

    with patch.object(ingest, "push_runtime_log", side_effect=fake_log):
        nueva = await ingest._merge_retry_queue(previa, ["malo1"], {"malo1": "sigue fallando"})

    assert nueva == []  # se descarta
    assert any(l["event"] == "gmail.retry.exhausted" for l in logs)
    assert any(l["level"] == "error" for l in logs)


@pytest.mark.asyncio
@pytestmark_db
async def test_e1_un_remitente_con_nombre_larguisimo_no_tumba_la_ingesta():
    """La columna `contacts.nombre` es String(120). Un nombre más largo hacía
    reventar el INSERT y ese correo se perdía para siempre."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact
    from app.providers.whatsapp.base import IncomingMessage
    from app.services.conversation import store_incoming

    remitente = f"largo-{uuid.uuid4().hex[:8]}@example.com"
    msg_id = await store_incoming(
        IncomingMessage(
            provider_message_id=f"m-{uuid.uuid4().hex}",
            from_phone=f"email:{remitente}",
            to_phone="email:negocio@gmail.com",
            message_type="text",
            text="Hola",
            customer_name="N" * 400,
            raw={"threadId": f"thr-{uuid.uuid4().hex[:8]}", "subject": "Asunto"},
        )
    )
    assert msg_id is not None

    async with db_session() as db:
        contacto = (
            await db.execute(
                select(Contact).where(Contact.telefono == f"email:{remitente}")
            )
        ).scalar_one()
    assert len(contacto.nombre) == 120


def test_las_cabeceras_guardadas_activan_el_filtro_gratis():
    """El provider guarda `auto_submitted` (guion bajo) y el clasificador busca
    `auto-submitted` (guion). Con esa discrepancia, el filtro determinista solo
    disparaba por Precedence y por remitente no-reply: las newsletters y las
    autorespuestas se colaban y gastaban modelo.

    El desajuste ya está arreglado DE RAÍZ en `email_is_automated`, que acepta
    las dos formas: por eso las cabeceras tal cual las guarda el provider ya
    disparan el filtro sin normalizar. Se comprueban las dos vías porque el
    llamante sigue normalizando y ninguna de las dos puede romperse."""
    from app.services.classifier import email_is_automated
    from app.services.conversation import _normalize_email_header_keys

    guardadas = {
        "list_unsubscribe": "<mailto:baja@x>",
        "list_id": None,
        "precedence": None,
        "auto_submitted": None,
    }
    assert email_is_automated("ana@x.com", guardadas)[0] is True
    assert email_is_automated(
        "ana@x.com", _normalize_email_header_keys(guardadas)
    )[0] is True


def test_e1_el_recorte_respeta_lo_que_ya_cabe():
    from app.services.conversation import _fit

    assert _fit("corto", 120) == "corto"
    assert _fit(None, 120) is None
    assert len(_fit("x" * 500, 120)) == 120


# ---------------------------------------------------------------------------
# E10 — dos canales de email activos
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_e10_dos_canales_de_email_no_matan_el_sondeo():
    """`scalar_one_or_none()` LANZA con dos filas: el sondeo fallaba en cada
    ciclo y no entraba ni un correo. Ahora se elige el más antiguo y se avisa."""
    from sqlalchemy import delete

    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType
    from app.services.gmail_ingest import _load_email_channel

    async with db_session() as db:
        await db.execute(delete(Channel).where(Channel.type == ChannelType.email))
        primero = Channel(type=ChannelType.email, name="Email viejo", enabled=True, config={})
        db.add(primero)
        await db.flush()
        primero_id = primero.id
        await db.commit()
    async with db_session() as db:
        db.add(Channel(type=ChannelType.email, name="Email nuevo", enabled=True, config={}))
        await db.commit()

    try:
        canal = await _load_email_channel()
        assert canal is not None
        assert canal.id == primero_id  # el más antiguo, de forma determinista
    finally:
        async with db_session() as db:
            await db.execute(delete(Channel).where(Channel.type == ChannelType.email))
            await db.commit()


# ---------------------------------------------------------------------------
# E5 — retención
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_e5_retencion_cero_no_purga_nada():
    """0 significa NO PURGAR. Antes el corte era "ahora" y vaciaba todo hoy."""
    from app.core.config import settings
    from app.services.gmail_retention import purge_old_emails

    original = settings.EMAIL_RETENTION_MONTHS
    settings.EMAIL_RETENTION_MONTHS = 0
    try:
        resultado = await purge_old_emails()
    finally:
        settings.EMAIL_RETENTION_MONTHS = original

    assert resultado == {"purged": 0, "status": "disabled"}


@pytestmark_db
@pytest.mark.asyncio
async def test_e5_un_borrador_sin_enviar_no_se_vacia():
    """La purga no puede dejar en blanco un borrador pendiente de revisión."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select, text

    from app.core.config import settings
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import (
        Conversation,
        ConversationCanal,
        ConversationStatus,
    )
    from app.models.message import Message, MessageRole
    from app.services.gmail_retention import purge_old_emails

    viejo = datetime.now(timezone.utc) - timedelta(days=400)
    async with db_session() as db:
        contacto = Contact(
            telefono=f"email:purga-{uuid.uuid4().hex[:8]}@example.com",
            origen=ContactOrigen.email,
        )
        db.add(contacto)
        await db.flush()
        conv = Conversation(
            contact_id=contacto.id,
            canal=ConversationCanal.email,
            session_id=contacto.telefono,
            status=ConversationStatus.bot,
        )
        db.add(conv)
        await db.flush()
        borrador = Message(
            conversation_id=conv.id,
            rol=MessageRole.assistant,
            contenido="Borrador sin revisar",
            extra={"is_draft": True, "draft_sent": False},
        )
        enviado = Message(
            conversation_id=conv.id,
            rol=MessageRole.assistant,
            contenido="Borrador ya enviado",
            extra={"is_draft": True, "draft_sent": True},
        )
        entrante = Message(
            conversation_id=conv.id, rol=MessageRole.user, contenido="Pregunta vieja", extra={}
        )
        db.add_all([borrador, enviado, entrante])
        await db.flush()
        ids = (borrador.id, enviado.id, entrante.id)
        # Los envejecemos por SQL: created_at tiene server_default.
        for mid in ids:
            await db.execute(
                text("UPDATE messages SET created_at = :t WHERE id = :i"),
                {"t": viejo, "i": str(mid)},
            )
        await db.commit()

    original = settings.EMAIL_RETENTION_MONTHS
    settings.EMAIL_RETENTION_MONTHS = 6
    try:
        await purge_old_emails()
    finally:
        settings.EMAIL_RETENTION_MONTHS = original

    async with db_session() as db:
        filas = {
            m.id: m
            for m in (
                await db.execute(select(Message).where(Message.id.in_(ids)))
            ).scalars().all()
        }

    assert filas[ids[0]].contenido == "Borrador sin revisar"  # intacto
    assert filas[ids[0]].extra.get("purged") is not True
    assert filas[ids[1]].contenido == ""  # ya enviado: es histórico
    assert filas[ids[2]].contenido == ""  # entrante viejo: se purga


# ---------------------------------------------------------------------------
# E8 — firma
# ---------------------------------------------------------------------------


def test_e8_la_firma_se_pega_con_el_separador_estandar():
    from app.services.channel_sender import append_signature

    salida = append_signature("Buenos días.", "Ana\nAtención al cliente")
    assert salida == "Buenos días.\n\n-- \nAna\nAtención al cliente"


def test_e8_la_firma_no_se_duplica_ni_se_inventa():
    from app.services.channel_sender import append_signature

    ya_firmado = "Texto\n\n-- \nAna"
    assert append_signature(ya_firmado, "Ana") == ya_firmado
    assert append_signature("Texto", "") == "Texto"
    assert append_signature("Texto", "   ") == "Texto"


@pytest.mark.asyncio
async def test_e8_el_borrador_del_agente_sale_firmado():
    import app.services.channel_sender as cs
    from app.models.conversation import ConversationCanal

    conv = SimpleNamespace(
        id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        canal=ConversationCanal.email,
        gmail_thread_id="thread_abc",
        subject="Consulta",
        session_id="email:ana@example.com",
    )
    fake_gmail = MagicMock()
    fake_gmail.create_draft = AsyncMock(
        return_value={"draft_id": "d1", "message_id": "m1"}
    )

    with patch.object(
        cs,
        "_gather_email_reply_headers",
        AsyncMock(
            return_value={
                "to_addr": "ana@example.com",
                "subject": "Consulta",
                "in_reply_to": None,
                "references": None,
            }
        ),
    ), patch.object(cs, "get_email_signature", AsyncMock(return_value="Ana\nSoporte")), \
        patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
        await cs.create_email_draft_for_conversation(conv, "Le confirmo el precio.")

    cuerpo = fake_gmail.create_draft.await_args.kwargs["body_text"]
    assert cuerpo.endswith("-- \nAna\nSoporte")


# ---------------------------------------------------------------------------
# E11 — se responde al remitente REAL del hilo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e11_se_responde_al_remitente_del_ultimo_correo_no_al_crm():
    """Si alguien edita el email del contacto en el CRM, la respuesta tiene que
    seguir yendo a la dirección desde la que el cliente escribe."""
    import app.services.channel_sender as cs
    from app.models.conversation import ConversationCanal

    conv = SimpleNamespace(
        id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        canal=ConversationCanal.email,
        gmail_thread_id="t1",
        subject="Factura",
        session_id="email:ana@example.com",
    )
    contacto = SimpleNamespace(id=conv.contact_id, email="OTRA-direccion@example.com")
    ultimo_entrante = SimpleNamespace(
        extra={
            "rfc822_message_id": "<rfc-1@x>",
            "subject": "Factura",
            "from_addr": "ana@example.com",
            "reply_to_addr": "ana@example.com",
        }
    )

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one(self):
            return self._value

        def scalar_one_or_none(self):
            return self._value

    class _FakeDB:
        def __init__(self):
            self._calls = 0

        async def execute(self, *_a, **_k):
            self._calls += 1
            return _Result(contacto if self._calls == 1 else ultimo_entrante)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    with patch("app.db.session.db_session", lambda: _FakeDB()):
        headers = await cs._gather_email_reply_headers(conv)

    assert headers["to_addr"] == "ana@example.com"


@pytest.mark.asyncio
async def test_e11_sin_remitente_guardado_cae_a_la_direccion_del_hilo():
    """Mensajes antiguos (sin from_addr en metadata): manda `session_id`."""
    import app.services.channel_sender as cs
    from app.models.conversation import ConversationCanal

    conv = SimpleNamespace(
        id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        canal=ConversationCanal.email,
        gmail_thread_id="t1",
        subject="Factura",
        session_id="email:viejo@example.com",
    )
    contacto = SimpleNamespace(id=conv.contact_id, email="editado@example.com")
    ultimo_entrante = SimpleNamespace(extra={"rfc822_message_id": "<rfc-1@x>"})

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one(self):
            return self._value

        def scalar_one_or_none(self):
            return self._value

    class _FakeDB:
        def __init__(self):
            self._calls = 0

        async def execute(self, *_a, **_k):
            self._calls += 1
            return _Result(contacto if self._calls == 1 else ultimo_entrante)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    with patch("app.db.session.db_session", lambda: _FakeDB()):
        headers = await cs._gather_email_reply_headers(conv)

    assert headers["to_addr"] == "viejo@example.com"


# ---------------------------------------------------------------------------
# E3 — la conexión rota deja constancia
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_e3_un_refresh_rechazado_desconecta_y_deja_el_motivo():
    import httpx
    from sqlalchemy import delete, select

    from app.core.encryption import get_encryption_service
    from app.db.session import db_session
    from app.models.external_api import ExternalAPI
    from app.services import google_oauth

    provider = "google_gmail"
    enc = get_encryption_service()
    import json
    from datetime import datetime, timedelta, timezone

    blob = json.dumps(
        {
            "access_token": "viejo",
            "refresh_token": "rt",
            # Caducado: fuerza el refresh.
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "scope": "",
        }
    )
    async with db_session() as db:
        await db.execute(delete(ExternalAPI).where(ExternalAPI.provider == provider))
        db.add(
            ExternalAPI(
                provider=provider,
                name=provider,
                credentials_encrypted=enc.encrypt(blob),
                is_active=True,
                extra={},
            )
        )
        await db.commit()

    respuesta = httpx.Response(400, json={"error": "invalid_grant"}, request=httpx.Request("POST", "https://x"))
    error = httpx.HTTPStatusError("invalid_grant", request=respuesta.request, response=respuesta)

    try:
        with patch.object(google_oauth, "refresh_access_token", AsyncMock(side_effect=error)):
            token = await google_oauth.get_access_token(provider)

        assert token is None
        async with db_session() as db:
            api = (
                await db.execute(
                    select(ExternalAPI).where(ExternalAPI.provider == provider)
                )
            ).scalar_one()
        # Ya NO se queda "Conectado" en el panel…
        assert api.is_active is False
        # …y queda escrito POR QUÉ.
        assert api.extra["auth_error"]["code"] == google_oauth.AUTH_ERROR_REFRESH_REJECTED
        assert "conectar" in api.extra["auth_error"]["message"].lower()
    finally:
        async with db_session() as db:
            await db.execute(delete(ExternalAPI).where(ExternalAPI.provider == provider))
            await db.commit()


@pytestmark_db
@pytest.mark.asyncio
async def test_e3_un_fallo_de_red_no_desconecta_la_cuenta():
    """Un timeout es pasajero: se anota, pero la integración sigue conectada."""
    import json
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete, select

    from app.core.encryption import get_encryption_service
    from app.db.session import db_session
    from app.models.external_api import ExternalAPI
    from app.services import google_oauth

    provider = "google_gmail"
    enc = get_encryption_service()
    blob = json.dumps(
        {
            "access_token": "viejo",
            "refresh_token": "rt",
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "scope": "",
        }
    )
    async with db_session() as db:
        await db.execute(delete(ExternalAPI).where(ExternalAPI.provider == provider))
        db.add(
            ExternalAPI(
                provider=provider,
                name=provider,
                credentials_encrypted=enc.encrypt(blob),
                is_active=True,
                extra={},
            )
        )
        await db.commit()

    try:
        with patch.object(
            google_oauth, "refresh_access_token", AsyncMock(side_effect=TimeoutError("red"))
        ):
            assert await google_oauth.get_access_token(provider) is None

        async with db_session() as db:
            api = (
                await db.execute(
                    select(ExternalAPI).where(ExternalAPI.provider == provider)
                )
            ).scalar_one()
        assert api.is_active is True
        assert api.extra["auth_error"]["code"] == google_oauth.AUTH_ERROR_REFRESH_ERROR
    finally:
        async with db_session() as db:
            await db.execute(delete(ExternalAPI).where(ExternalAPI.provider == provider))
            await db.commit()


@pytestmark_db
@pytest.mark.asyncio
async def test_e3_sin_refresh_token_tambien_deja_constancia():
    import json
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete, select

    from app.core.encryption import get_encryption_service
    from app.db.session import db_session
    from app.models.external_api import ExternalAPI
    from app.services import google_oauth

    provider = "google_gmail"
    enc = get_encryption_service()
    blob = json.dumps(
        {
            "access_token": "viejo",
            "refresh_token": None,
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "scope": "",
        }
    )
    async with db_session() as db:
        await db.execute(delete(ExternalAPI).where(ExternalAPI.provider == provider))
        db.add(
            ExternalAPI(
                provider=provider,
                name=provider,
                credentials_encrypted=enc.encrypt(blob),
                is_active=True,
                extra={},
            )
        )
        await db.commit()

    try:
        assert await google_oauth.get_access_token(provider) is None
        async with db_session() as db:
            api = (
                await db.execute(
                    select(ExternalAPI).where(ExternalAPI.provider == provider)
                )
            ).scalar_one()
        assert api.extra["auth_error"]["code"] == google_oauth.AUTH_ERROR_NO_REFRESH_TOKEN
        assert api.is_active is False
    finally:
        async with db_session() as db:
            await db.execute(delete(ExternalAPI).where(ExternalAPI.provider == provider))
            await db.commit()


@pytest.mark.asyncio
async def test_e3_el_sondeo_avisa_cuando_no_hay_credenciales():
    import app.services.gmail_ingest as ingest

    canal = SimpleNamespace(id=uuid.uuid4(), config={"last_history_id": "1"})
    fake_gmail = MagicMock()
    fake_gmail.get_profile = AsyncMock(return_value=None)
    logs: list = []

    async def fake_log(**kwargs):
        logs.append(kwargs)

    with patch.object(ingest, "_load_email_channel", AsyncMock(return_value=canal)), \
        patch.object(ingest, "get_gmail_provider", return_value=fake_gmail), \
        patch.object(ingest, "get_redis", return_value=_FakeRedis()), \
        patch.object(ingest, "push_runtime_log", side_effect=fake_log):
        resultado = await ingest.sync_incoming_emails()

    assert resultado["status"] == "no_credentials"
    assert any(l["event"] == "gmail.sync.no_credentials" for l in logs)


# ---------------------------------------------------------------------------
# E2 — buffer por conversación
# ---------------------------------------------------------------------------


def test_e2_la_clave_del_buffer_es_la_conversacion():
    from app.services import message_buffer

    cid = uuid.uuid4()
    clave = message_buffer.conversation_key(cid)
    assert clave == f"conv:{cid}"
    assert message_buffer.conversation_id_from_key(clave) == cid
    # Las claves heredadas (identificador de contacto) se reconocen como tales.
    assert message_buffer.conversation_id_from_key("+34600111222") is None
    assert message_buffer.conversation_id_from_key("email:ana@example.com") is None
    assert message_buffer.conversation_id_from_key("conv:no-es-un-uuid") is None


@pytestmark_db
@pytest.mark.asyncio
async def test_e2_dos_hilos_del_mismo_remitente_no_se_mezclan():
    """Ana pregunta por una factura (hilo A) y hace un pedido (hilo B). Tienen
    que salir DOS borradores, uno en cada hilo. Antes salía uno solo, en el B,
    con las dos cosas mezcladas, y el A se quedaba sin respuesta para siempre."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.providers.whatsapp.base import IncomingMessage
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages, store_incoming
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    remitente = f"ana-{uuid.uuid4().hex[:8]}@example.com"
    phone = f"email:{remitente}"
    hilo_a = f"thrA-{uuid.uuid4().hex[:8]}"
    hilo_b = f"thrB-{uuid.uuid4().hex[:8]}"

    def _correo(thread, texto, asunto):
        return IncomingMessage(
            provider_message_id=f"m-{uuid.uuid4().hex}",
            from_phone=phone,
            to_phone="email:negocio@gmail.com",
            message_type="text",
            text=texto,
            customer_name="Ana",
            raw={
                "threadId": thread,
                "subject": asunto,
                "rfc822_message_id": f"<{thread}@x>",
                "attachments": [],
            },
        )

    id_a = await store_incoming(_correo(hilo_a, "¿Me mandáis la factura de marzo?", "Factura"))
    id_b = await store_incoming(_correo(hilo_b, "Quiero pedir 20 unidades del A3.", "Pedido"))
    assert id_a and id_b

    async with db_session() as db:
        convs = {}
        for hilo in (hilo_a, hilo_b):
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.gmail_thread_id == hilo)
                )
            ).scalar_one()
            conv.status = ConversationStatus.bot
            conv.spam_reviewed = True
            convs[hilo] = conv.id
        await db.commit()

    # Son conversaciones DISTINTAS (store_incoming ya agrupaba bien por hilo).
    assert convs[hilo_a] != convs[hilo_b]

    clave_a = message_buffer.conversation_key(convs[hilo_a])
    clave_b = message_buffer.conversation_key(convs[hilo_b])
    await message_buffer.push_message(clave_a, str(id_a))
    await message_buffer.push_message(clave_b, str(id_b))

    borradores: list = []

    async def fake_draft(conv, texto):
        borradores.append((conv.id, texto))
        return {"draft_id": f"d-{conv.id}", "message_id": "m"}

    def _respuesta(user_message, **_kw):
        return f"Respondo a: {user_message}"

    with patch("app.services.conversation.run_agent", AsyncMock(side_effect=lambda **kw: _respuesta(kw["user_message"]))), \
        patch("app.services.channel_sender.create_email_draft_for_conversation", side_effect=fake_draft), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch("app.services.conversation.moderate", AsyncMock(return_value=SimpleNamespace(flagged=False, categories=[]))), \
        patch("app.services.conversation.can_call_llm", AsyncMock(return_value=True)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(clave_a)
        await process_buffered_messages(clave_b)

    # DOS borradores, uno por hilo, cada uno con SU pregunta.
    assert len(borradores) == 2
    por_conv = dict(borradores)
    assert "factura" in por_conv[convs[hilo_a]].lower()
    assert "20 unidades" in por_conv[convs[hilo_b]]
    assert "20 unidades" not in por_conv[convs[hilo_a]]


# ---------------------------------------------------------------------------
# Cerrojo por conversación
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cerrojo_impide_dos_ejecuciones_a_la_vez():
    """Con el cerrojo tomado, el segundo drenado NO drena ni ejecuta el agente:
    se reprograma para más tarde (los mensajes siguen en la cola)."""
    import app.services.conversation as conv_mod

    redis = _FakeRedis()
    conv_id = uuid.uuid4()
    clave = f"conv:{conv_id}"

    with patch.object(conv_mod, "get_redis", return_value=redis):
        primero = await conv_mod._acquire_conv_lock(conv_id)
        assert primero
        # Un segundo intento se la encuentra ocupada.
        assert await conv_mod._acquire_conv_lock(conv_id) is None

        interior = AsyncMock()
        tarea = MagicMock()
        with patch.object(conv_mod, "_process_buffered_locked", interior), \
            patch("app.tasks.drain_buffer.drain_buffer", tarea):
            await conv_mod.process_buffered_messages(clave)
        interior.assert_not_awaited()
        tarea.apply_async.assert_called_once()

        # Al soltarlo, el siguiente sí entra.
        await conv_mod._release_conv_lock(conv_id, primero)
        assert await conv_mod._acquire_conv_lock(conv_id)


@pytest.mark.asyncio
async def test_el_cerrojo_se_suelta_aunque_el_runtime_reviente():
    import app.services.conversation as conv_mod

    redis = _FakeRedis()
    conv_id = uuid.uuid4()

    with patch.object(conv_mod, "get_redis", return_value=redis), \
        patch.object(
            conv_mod, "_process_buffered_locked", AsyncMock(side_effect=RuntimeError("boom"))
        ):
        with pytest.raises(RuntimeError):
            await conv_mod.process_buffered_messages(f"conv:{conv_id}")

    # No queda bloqueada: el siguiente drenado puede entrar.
    with patch.object(conv_mod, "get_redis", return_value=redis):
        assert await conv_mod._acquire_conv_lock(conv_id)


@pytest.mark.asyncio
async def test_una_clave_heredada_sigue_funcionando_sin_cerrojo():
    """Lo que ya estuviera encolado en Redis al desplegar no se puede perder."""
    import app.services.conversation as conv_mod

    interior = AsyncMock()
    with patch.object(conv_mod, "_process_buffered_locked", interior):
        await conv_mod.process_buffered_messages("+34600111222")
    interior.assert_awaited_once()
    assert interior.await_args.args[0] == "+34600111222"


# ---------------------------------------------------------------------------
# E9 + respuesta vacía (camino completo con base de datos)
# ---------------------------------------------------------------------------


async def _correo_en_bot(texto: str, cabeceras: dict | None = None, remitente: str | None = None):
    """Crea un correo entrante con la conversación en `bot`. Devuelve
    (conversation_id, message_id, buffer_key)."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.providers.whatsapp.base import IncomingMessage
    from app.services import message_buffer
    from app.services.conversation import store_incoming

    remitente = remitente or f"cli-{uuid.uuid4().hex[:8]}@example.com"
    hilo = f"thr-{uuid.uuid4().hex[:8]}"
    msg_id = await store_incoming(
        IncomingMessage(
            provider_message_id=f"m-{uuid.uuid4().hex}",
            from_phone=f"email:{remitente}",
            to_phone="email:negocio@gmail.com",
            message_type="text",
            text=texto,
            customer_name="Cliente",
            raw={
                "threadId": hilo,
                "subject": "Asunto",
                "rfc822_message_id": f"<{hilo}@x>",
                "attachments": [],
                "email_headers": cabeceras or {},
            },
        )
    )
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.gmail_thread_id == hilo))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv_id = conv.id
        await db.commit()
    clave = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(clave, str(msg_id))
    return conv_id, msg_id, clave


@pytestmark_db
@pytest.mark.asyncio
async def test_e9_un_rebote_automatico_en_hilo_liberado_no_gasta_modelo():
    """Liberar de cuarentena marcaba el hilo como revisado y a partir de ahí NO
    se volvía a filtrar nunca: cada rebote automático posterior gastaba una
    llamada al modelo y generaba un borrador de ruido."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    conv_id, _, clave = await _correo_en_bot(
        "Su mensaje no ha podido entregarse.",
        cabeceras={"auto_submitted": "auto-replied"},
    )
    # El hilo YA fue revisado y liberado por una persona.
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        conv.spam_reviewed = True
        await db.commit()

    agente = AsyncMock(return_value="No debería llamarse")
    clasificador = AsyncMock()
    with patch("app.services.conversation.run_agent", agente), \
        patch("app.services.conversation.classify_message", clasificador), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch("app.services.conversation.can_call_llm", AsyncMock(return_value=True)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(clave)

    # Ni agente ni clasificador: cero tokens.
    agente.assert_not_awaited()
    clasificador.assert_not_awaited()
    # Y NO se re-cuarentena (eso sí sería el bucle que se quería evitar).
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
    assert conv.quarantined_at is None


@pytestmark_db
@pytest.mark.asyncio
async def test_una_respuesta_vacia_del_modelo_deriva_a_humano():
    """Antes: no se enviaba ni se guardaba nada, se logueaba "0 parte(s)" y la
    conversación seguía en bot. El cliente no recibía respuesta nunca."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    conv_id, _, clave = await _correo_en_bot("¿Tenéis stock del A3?")

    draft = AsyncMock()
    with patch("app.services.conversation.run_agent", AsyncMock(return_value="   ")), \
        patch("app.services.channel_sender.create_email_draft_for_conversation", draft), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch("app.services.conversation.moderate", AsyncMock(return_value=SimpleNamespace(flagged=False, categories=[]))), \
        patch("app.services.conversation.can_call_llm", AsyncMock(return_value=True)), \
        patch("app.services.conversation.classify_message", AsyncMock(return_value=SimpleNamespace(is_spam=False, reason=""))), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(clave)

    # No se ha creado ningún borrador vacío…
    draft.assert_not_awaited()
    # …y la conversación queda en manos de una persona, no muda en `bot`.
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
    assert conv.status == ConversationStatus.humano
