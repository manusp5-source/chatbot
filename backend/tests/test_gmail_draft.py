"""Tests del canal Email (Gmail) — Fase 5c: limpieza del cuerpo + borradores.

Cobertura (sin DB ni red real):
- clean_email_body: firma "-- ", cita ">", "El … escribió:", footer legal,
  correo solo-firma → devuelve original.
- build_raw_message / create_draft: construcción correcta del MIME (To, Subject
  con Re:, In-Reply-To, References, base64url, threadId) con httpx mockeado.
- Camino de salida email→borrador: con runtime/cliente mockeado, verifica que
  se crea el draft y se persiste el Message con is_draft=True y NO se envía.
- Endpoint send: con cliente mockeado, verifica send_draft y que el Message
  queda draft_sent=True.
"""
from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1) clean_email_body (puro)
# ---------------------------------------------------------------------------


def test_clean_signature_and_legal_footer():
    """Caso real: pregunta arriba + '-- ' + firma + descargo legal larguísimo."""
    from app.providers.gmail.client import clean_email_body

    body = (
        "Hola, ¿cuánto cuesta el plan premium?\n"
        "\n"
        "Gracias.\n"
        "-- \n"
        "Juan Pérez\n"
        "Director General | Empresa SL\n"
        "Tel: 600 123 456\n"
        "\n"
        "AVISO LEGAL: Este mensaje y sus archivos adjuntos van dirigidos "
        "exclusivamente a su destinatario y pueden contener información "
        "confidencial sujeta a secreto profesional..."
    )
    cleaned = clean_email_body(body)
    assert "plan premium" in cleaned
    assert "Gracias." in cleaned
    # Todo lo que va tras la firma desaparece.
    assert "Juan Pérez" not in cleaned
    assert "AVISO LEGAL" not in cleaned


def test_clean_signature_double_dash_no_space():
    """También corta con '--' (sin espacio)."""
    from app.providers.gmail.client import clean_email_body

    cleaned = clean_email_body("Mi pregunta.\n--\nFirma corta")
    assert cleaned == "Mi pregunta."


def test_clean_quoted_history_gt():
    """Líneas que empiezan por '>' se consideran historial citado."""
    from app.providers.gmail.client import clean_email_body

    cleaned = clean_email_body("Respuesta nueva.\n\n> texto citado\n> más cita")
    assert cleaned == "Respuesta nueva."


def test_clean_quoted_history_es_header():
    """Cabecera de cita en español 'El … escribió:'."""
    from app.providers.gmail.client import clean_email_body

    body = (
        "Vale, perfecto.\n\n"
        "El 3 jun 2026, a las 10:00, Ana <ana@x.com> escribió:\n"
        "> hola, te escribo por..."
    )
    assert clean_email_body(body) == "Vale, perfecto."


def test_clean_quoted_history_en_header():
    """Cabecera de cita en inglés 'On … wrote:'."""
    from app.providers.gmail.client import clean_email_body

    body = "Sure, thanks.\n\nOn Tue, 3 Jun 2026 at 10:00, Ana <ana@x.com> wrote:\n> hi"
    assert clean_email_body(body) == "Sure, thanks."


def test_clean_outlook_original_message():
    """Bloque clásico de Outlook -----Original Message-----."""
    from app.providers.gmail.client import clean_email_body

    body = "Confirmado.\n\n-----Original Message-----\nFrom: x\nSent: ayer"
    assert clean_email_body(body) == "Confirmado."


def test_clean_collapses_blank_lines():
    """Colapsa líneas en blanco múltiples y hace trim."""
    from app.providers.gmail.client import clean_email_body

    cleaned = clean_email_body("\n\nHola\n\n\n\nadiós\n\n")
    assert cleaned == "Hola\n\nadiós"


def test_clean_only_signature_returns_original():
    """Si tras limpiar queda vacío (correo solo-firma) → devuelve el original."""
    from app.providers.gmail.client import clean_email_body

    body = "-- \nUn saludo,\nPedro"
    # No perdemos el mensaje: devuelve el texto original tal cual.
    assert clean_email_body(body) == body


def test_clean_no_signature_no_quote_untouched():
    """Un correo limpio (sin firma ni cita) solo se le hace trim."""
    from app.providers.gmail.client import clean_email_body

    body = "Una sola línea de consulta sin nada más."
    assert clean_email_body(body) == body


def test_sanitize_email_html_document_strips_dangerous_blocks():
    """sanitize_email_html_document (primera barrera): quita script/style/head/
    link/meta/base y comentarios; conserva el contenido formateado."""
    from app.providers.gmail.client import sanitize_email_html_document

    html = (
        "<head><meta charset='utf-8'><base href='http://x'><style>a{}</style></head>"
        "<link rel='stylesheet' href='http://x/a.css'>"
        "<p>Texto <strong>importante</strong></p>"
        "<script>document.cookie</script>"
        "<!-- comentario --><!--[if mso]>cond<![endif]-->"
    )
    out = sanitize_email_html_document(html)
    assert "<p>Texto <strong>importante</strong></p>" in out
    for needle in ("<script", "<style", "<head", "<link", "<meta", "<base", "<!--"):
        assert needle not in out.lower()
    assert "document.cookie" not in out
    assert "cond" not in out


def test_sanitize_email_html_document_empty():
    from app.providers.gmail.client import sanitize_email_html_document

    assert sanitize_email_html_document("") == ""
    assert sanitize_email_html_document(None) == ""  # type: ignore[arg-type]


def test_parse_does_not_trim_signature_or_quote():
    """5d — Decisión de producto: parse_get_message_result NO recorta el cuerpo
    por firma/cita. Se almacena el texto plano COMPLETO (la dueña no quiere
    perder contenido por heurísticas). Los marcadores de adjunto SÍ se añaden."""
    from app.providers.gmail.client import parse_get_message_result

    def _b64(t: str) -> str:
        return base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")

    raw = {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "ana@x.com"},
                {"name": "To", "value": "negocio@gmail.com"},
                {"name": "Subject", "value": "Consulta"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Mi pregunta.\n-- \nFirma larga\nLegal...")},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "doc.pdf",
                    "body": {"attachmentId": "a1"},
                },
            ],
        },
    }
    result = parse_get_message_result(raw)
    # El cuerpo queda COMPLETO: la firma y el footer legal NO se recortan.
    assert "Mi pregunta." in result["body"]
    assert "Firma larga" in result["body"]
    assert "Legal..." in result["body"]
    # El marcador de adjunto se conserva (se añade al final del texto plano).
    assert "[adjunto: doc.pdf]" in result["body"]


def test_parse_captures_html_and_sanitizes_document():
    """parse_get_message_result captura el HTML de la mejor parte text/html y le
    aplica la primera barrera regex (script/style/head/comentarios fuera). El
    saneado real (lista blanca) lo hace el frontend."""
    from app.providers.gmail.client import parse_get_message_result

    def _b64(t: str) -> str:
        return base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")

    html = (
        "<html><head><style>p{color:red}</style></head>"
        "<body><p>Hola <b>mundo</b> <a href='https://x.com'>enlace</a></p>"
        "<script>alert('xss')</script>"
        "<!--[if mso]><p>solo outlook</p><![endif]--></body></html>"
    )
    raw = {
        "id": "m_html",
        "threadId": "t1",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "ana@x.com"},
                {"name": "To", "value": "negocio@gmail.com"},
                {"name": "Subject", "value": "Con formato"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Hola mundo enlace")}},
                {"mimeType": "text/html", "body": {"data": _b64(html)}},
            ],
        },
    }
    result = parse_get_message_result(raw)
    h = result["html"]
    # Contenido formateado conservado.
    assert "<b>mundo</b>" in h
    assert "href" in h and "x.com" in h
    # Primera barrera: script/style/head/comentarios condicionales eliminados.
    assert "<script" not in h.lower()
    assert "alert(" not in h
    assert "<style" not in h.lower()
    assert "<head" not in h.lower()
    assert "solo outlook" not in h  # comentario condicional de Outlook fuera
    # El texto plano sigue presente para canales/agente sin HTML.
    assert "Hola mundo" in result["body"]


def test_parse_html_empty_when_no_html_part():
    """Si el correo no trae parte text/html, html queda en "" (no None)."""
    from app.providers.gmail.client import parse_get_message_result

    def _b64(t: str) -> str:
        return base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")

    raw = {
        "id": "m_plain",
        "threadId": "t1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "ana@x.com"},
                {"name": "To", "value": "negocio@gmail.com"},
                {"name": "Subject", "value": "Solo texto"},
            ],
            "body": {"data": _b64("Solo texto plano, sin firma recortada.")},
        },
    }
    result = parse_get_message_result(raw)
    assert result["html"] == ""
    assert "Solo texto plano" in result["body"]


# ---------------------------------------------------------------------------
# 2) build_raw_message + create_draft (MIME + httpx mockeado)
# ---------------------------------------------------------------------------


def _decode_raw(raw_b64url: str) -> str:
    padding = "=" * (-len(raw_b64url) % 4)
    return base64.urlsafe_b64decode(raw_b64url + padding).decode("utf-8")


def test_build_raw_message_headers_and_re_prefix():
    from app.providers.gmail.client import build_raw_message

    raw = build_raw_message(
        to_addr="ana@example.com",
        subject="Consulta de presupuesto",
        body_text="Hola Ana, aquí va la info.",
        in_reply_to="<rfc-1@x>",
        references="<rfc-0@x>",
    )
    decoded = _decode_raw(raw)
    assert "To: ana@example.com" in decoded
    assert "Subject: Re: Consulta de presupuesto" in decoded
    assert "In-Reply-To: <rfc-1@x>" in decoded
    assert "References: <rfc-0@x>" in decoded
    assert "text/plain" in decoded
    assert "utf-8" in decoded.lower()
    assert "Hola Ana" in decoded


def test_build_raw_message_no_double_re():
    from app.providers.gmail.client import build_raw_message

    raw = build_raw_message("a@x", "Re: ya tiene re", "hola")
    decoded = _decode_raw(raw)
    assert "Subject: Re: ya tiene re" in decoded
    assert "Re: Re:" not in decoded


@pytest.mark.asyncio
async def test_create_draft_posts_correct_payload():
    """create_draft construye el body {message:{raw, threadId}} y hace POST a
    drafts. Verifica el threadId y que el raw lleva las cabeceras de hilo."""
    from app.providers.gmail.client import GmailClient

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "draft_123", "message": {"id": "msg_456"}}

        def raise_for_status(self):
            pass

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse()

    client = GmailClient()
    with patch.object(client, "_headers", AsyncMock(return_value={"Authorization": "Bearer x"})), \
        patch("app.providers.gmail.client.httpx.AsyncClient", _FakeAsyncClient):
        result = await client.create_draft(
            thread_id="thread_abc",
            to_addr="ana@example.com",
            subject="Consulta",
            body_text="respuesta del agente",
            in_reply_to="<rfc-1@x>",
            references="<rfc-0@x>",
        )

    assert result == {"draft_id": "draft_123", "message_id": "msg_456"}
    assert captured["url"].endswith("/users/me/drafts")
    msg = captured["json"]["message"]
    assert msg["threadId"] == "thread_abc"
    decoded = _decode_raw(msg["raw"])
    assert "To: ana@example.com" in decoded
    assert "Subject: Re: Consulta" in decoded
    assert "In-Reply-To: <rfc-1@x>" in decoded


@pytest.mark.asyncio
async def test_send_draft_posts_id():
    from app.providers.gmail.client import GmailClient

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "sent_msg_1", "threadId": "thread_abc"}

        def raise_for_status(self):
            pass

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse()

    client = GmailClient()
    with patch.object(client, "_headers", AsyncMock(return_value={"Authorization": "Bearer x"})), \
        patch("app.providers.gmail.client.httpx.AsyncClient", _FakeAsyncClient):
        result = await client.send_draft("draft_123")

    assert result == {"message_id": "sent_msg_1", "thread_id": "thread_abc"}
    assert captured["url"].endswith("/users/me/drafts/send")
    assert captured["json"] == {"id": "draft_123"}


# ---------------------------------------------------------------------------
# 3) create_email_draft_for_conversation (gathering de cabeceras)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_email_draft_for_conversation_gathers_headers():
    """Verifica que el helper saca el to/subject/in-reply-to del último mensaje
    entrante y llama create_draft. DB mockeada vía sesión fake."""
    import app.services.channel_sender as cs
    from app.models.conversation import ConversationCanal

    conv = SimpleNamespace(
        id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        canal=ConversationCanal.email,
        gmail_thread_id="thread_abc",
        subject="Consulta original",
    )
    contact = SimpleNamespace(id=conv.contact_id, email="ana@example.com")
    last_in = SimpleNamespace(
        extra={
            "rfc822_message_id": "<rfc-1@x>",
            "references": "<rfc-0@x>",
            "subject": "Consulta original",
        }
    )

    # Sesión fake: devuelve contact en la 1ª query, last_in en la 2ª.
    class _Result:
        def __init__(self, value, many=False):
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
            return _Result(contact if self._calls == 1 else last_in)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake_gmail = MagicMock()
    fake_gmail.create_draft = AsyncMock(
        return_value={"draft_id": "draft_xyz", "message_id": "msg_xyz"}
    )

    with patch("app.db.session.db_session", lambda: _FakeDB()), \
        patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
        result = await cs.create_email_draft_for_conversation(conv, "respuesta del agente")

    assert result is not None
    assert result["draft_id"] == "draft_xyz"
    fake_gmail.create_draft.assert_awaited_once()
    kwargs = fake_gmail.create_draft.await_args.kwargs
    assert kwargs["to_addr"] == "ana@example.com"
    assert kwargs["thread_id"] == "thread_abc"
    assert kwargs["in_reply_to"] == "<rfc-1@x>"
    assert "<rfc-0@x>" in kwargs["references"] and "<rfc-1@x>" in kwargs["references"]
    assert kwargs["body_text"] == "respuesta del agente"


@pytest.mark.asyncio
async def test_create_email_draft_no_recipient_returns_none():
    """Sin email del contacto → no se crea borrador (None), no se llama Gmail."""
    import app.services.channel_sender as cs
    from app.models.conversation import ConversationCanal

    conv = SimpleNamespace(
        id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        canal=ConversationCanal.email,
        gmail_thread_id="thread_abc",
        subject="x",
    )
    contact = SimpleNamespace(id=conv.contact_id, email=None)  # sin email

    class _Result:
        def scalar_one(self):
            return contact

        def scalar_one_or_none(self):
            return None

    class _FakeDB:
        async def execute(self, *_a, **_k):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake_gmail = MagicMock()
    fake_gmail.create_draft = AsyncMock()

    with patch("app.db.session.db_session", lambda: _FakeDB()), \
        patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
        result = await cs.create_email_draft_for_conversation(conv, "texto")

    assert result is None
    fake_gmail.create_draft.assert_not_called()


# ---------------------------------------------------------------------------
# 4) Camino de salida email→borrador en process_buffered_messages (DB-backed)
# ---------------------------------------------------------------------------


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


@pytestmark_db
@pytest.mark.asyncio
async def test_email_output_creates_draft_not_send():
    """Camino completo: un correo entrante en bot → el runtime genera un BORRADOR
    (no envía) y persiste un Message assistant con is_draft=True. 5d: la
    conversación NO cambia de status al crear el borrador (se queda en 'bot');
    la señal de "borrador pendiente" la da has_pending_draft, no el status.

    Este test SIEMBRA lo que necesita (un agente de texto activo). Antes no lo
    hacía: con la base limpia `get_runtime_for_channel` devolvía None, el
    runtime salía en silencio y el test fallaba. Solo pasaba si otro test había
    dejado un agente por ahí, así que el único camino de punta a punta de
    "entra un correo, sale un borrador, nunca se envía" no se estaba validando.
    """
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.providers.whatsapp.base import IncomingMessage
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages, store_incoming
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    sender = f"draft-test-{uuid.uuid4().hex[:8]}@example.com"
    phone = f"email:{sender}"
    thread = f"thr-{uuid.uuid4().hex[:8]}"

    incoming = IncomingMessage(
        provider_message_id=f"m-{uuid.uuid4().hex}",
        from_phone=phone,
        to_phone="email:negocio@gmail.com",
        message_type="text",
        text="¿Cuánto cuesta el plan premium?",
        customer_name="Ana",
        raw={
            "threadId": thread,
            "subject": "Precios",
            "rfc822_message_id": "<rfc-in@x>",
            "in_reply_to": None,
            "references": None,
            "attachments": [],
        },
    )
    msg_id = await store_incoming(incoming)
    assert msg_id is not None

    # Aseguramos que la conv quede en bot (depende del settings por defecto).
    async with db_session() as db:
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.gmail_thread_id == thread)
            )
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.spam_reviewed = True  # saltar clasificador
        await db.commit()
        conv_id = conv.id

    # Empujamos el id al buffer. La clave es la CONVERSACIÓN, no el contacto
    # (ver services/message_buffer): dos hilos del mismo remitente tienen que
    # drenarse por separado.
    buffer_key = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(buffer_key, str(msg_id))

    fake_gmail = MagicMock()
    fake_gmail.create_draft = AsyncMock(
        return_value={"draft_id": "draft_real", "message_id": "draft_msg"}
    )
    fake_gmail.send_draft = AsyncMock()  # NO debe llamarse

    # Mock del LLM (run_agent) y de Gmail + pausa OFF + should_process True.
    with patch("app.services.conversation.run_agent", AsyncMock(return_value="Hola Ana, el plan premium cuesta X.")), \
        patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch("app.services.conversation.moderate", AsyncMock(return_value=SimpleNamespace(flagged=False, categories=[]))), \
        patch("app.services.conversation.can_call_llm", AsyncMock(return_value=True)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(buffer_key)

    # Se creó el borrador y NO se envió.
    fake_gmail.create_draft.assert_awaited_once()
    fake_gmail.send_draft.assert_not_called()

    async with db_session() as db:
        draft_msg = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conv_id, Message.rol == MessageRole.assistant)
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()

    assert draft_msg is not None
    assert (draft_msg.extra or {}).get("is_draft") is True
    assert (draft_msg.extra or {}).get("draft_sent") is False
    assert (draft_msg.extra or {}).get("gmail_draft_id") == "draft_real"
    assert "plan premium" in (draft_msg.contenido or "")
    # 5d: el borrador NO marca la conversación como humano (se queda en bot).
    assert conv.status == ConversationStatus.bot


# ---------------------------------------------------------------------------
# 5) Endpoint send (DB-backed): envía el draft y marca draft_sent=True
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_send_draft_endpoint_marks_sent():
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.models.user import User, UserRole

    sender = f"send-test-{uuid.uuid4().hex[:8]}@example.com"

    # Sembramos user (el endpoint registra la corrección con created_by → FK
    # real contra users) + contact + conv + message borrador.
    async with db_session() as db:
        operator = User(
            email=f"op-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=UserRole.admin,
        )
        db.add(operator)
        await db.flush()
        operator_id = operator.id
        contact = Contact(
            telefono=f"email:{sender}",
            email=sender,
            origen=ContactOrigen.email,
            in_crm=False,
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.email,
            session_id=f"email:{sender}",
            status=ConversationStatus.humano,
            gmail_thread_id=f"thr-{uuid.uuid4().hex[:8]}",
            subject="Precios",
        )
        db.add(conv)
        await db.flush()
        # Mensaje entrante (para cabeceras) + borrador.
        db.add(
            Message(
                conversation_id=conv.id,
                rol=MessageRole.user,
                contenido="¿precio?",
                extra={"rfc822_message_id": "<rfc-in@x>", "subject": "Precios"},
            )
        )
        draft = Message(
            conversation_id=conv.id,
            rol=MessageRole.assistant,
            contenido="Borrador original del agente.",
            extra={"is_draft": True, "draft_sent": False, "gmail_draft_id": "draft_real"},
        )
        db.add(draft)
        await db.commit()
        conv_id, draft_id = conv.id, draft.id

    from app.api import conversations as conv_api

    fake_gmail = MagicMock()
    fake_gmail.update_draft = AsyncMock(return_value={"draft_id": "draft_real", "message_id": "m"})
    fake_gmail.send_draft = AsyncMock(
        return_value={"message_id": "sent_final", "thread_id": "t"}
    )

    body = conv_api.SendDraftBody(text="Texto final editado por la operadora.")

    async with db_session() as db:
        with patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
            result = await conv_api.send_draft(
                conversation_id=conv_id,
                message_id=draft_id,
                body=body,
                db=db,
                current_user=SimpleNamespace(id=operator_id),
            )

    # El texto cambió → se actualizó el draft y luego se envió.
    fake_gmail.update_draft.assert_awaited_once()
    fake_gmail.send_draft.assert_awaited_once_with("draft_real")
    assert result.draft_sent is True
    assert result.is_draft is False
    assert result.contenido == "Texto final editado por la operadora."

    async with db_session() as db:
        msg = (
            await db.execute(select(Message).where(Message.id == draft_id))
        ).scalar_one()
    assert (msg.extra or {}).get("draft_sent") is True
    assert (msg.extra or {}).get("is_draft") is False
    assert (msg.extra or {}).get("provider_message_id") == "sent_final"
