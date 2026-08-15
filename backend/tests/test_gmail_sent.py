"""Tests del canal Email (Gmail) — Fase 5b.

Cobertura (sin DB ni red real salvo los gated con DB):
- GmailClient.send_message: construye el MIME correcto (Re:, In-Reply-To/
  References, threadId) con httpx mockeado y devuelve {message_id, thread_id}.
- Rama email de send_text_to_conversation: con cliente mockeado, envía y
  devuelve el id de Gmail; sin email destino → "".
- parse_get_message_result: expone label_ids.
- Sync "Enviados" (gmail_ingest._process_message_ids):
  - self-message con SENT en hilo seguido → store_outgoing_email (rol=assistant).
  - self-message con DRAFT → se salta (no inserta, no encola agente).
  - self-message sin SENT → se salta.
  - entrante → store_incoming + encola agente.
- store_outgoing_email (DB-backed, gated): inserta saliente en hilo seguido;
  hilo no seguido → None; provider_message_id duplicado → None.
"""
from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1) GmailClient.send_message (MIME + httpx mockeado)
# ---------------------------------------------------------------------------


def _decode_raw(raw_b64url: str) -> str:
    padding = "=" * (-len(raw_b64url) % 4)
    return base64.urlsafe_b64decode(raw_b64url + padding).decode("utf-8")


@pytest.mark.asyncio
async def test_send_message_posts_correct_mime_and_returns_id():
    """send_message hace POST a messages/send con {raw, threadId} y el raw lleva
    To, Subject con 'Re: ', In-Reply-To y References. Devuelve message_id."""
    from app.providers.gmail.client import GmailClient

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "sent_1", "threadId": "thread_abc"}

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
        result = await client.send_message(
            thread_id="thread_abc",
            to_addr="ana@example.com",
            subject="Consulta",
            body_text="Hola Ana, aquí va la respuesta.",
            in_reply_to="<rfc-1@x>",
            references="<rfc-0@x>",
        )

    assert result == {"message_id": "sent_1", "thread_id": "thread_abc"}
    assert captured["url"].endswith("/users/me/messages/send")
    assert captured["json"]["threadId"] == "thread_abc"
    decoded = _decode_raw(captured["json"]["raw"])
    assert "To: ana@example.com" in decoded
    assert "Subject: Re: Consulta" in decoded
    assert "In-Reply-To: <rfc-1@x>" in decoded
    assert "References: <rfc-0@x>" in decoded
    assert "text/plain" in decoded
    assert "Hola Ana" in decoded


@pytest.mark.asyncio
async def test_send_message_raises_without_credentials():
    from app.providers.gmail.client import GmailClient

    client = GmailClient()
    with patch.object(client, "_headers", AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError):
            await client.send_message("t", "a@x", "s", "b")


# ---------------------------------------------------------------------------
# 2) Rama email de send_text_to_conversation
# ---------------------------------------------------------------------------


def _email_conv():
    from app.models.conversation import ConversationCanal

    return SimpleNamespace(
        id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        canal=ConversationCanal.email,
        gmail_thread_id="thread_abc",
        subject="Consulta original",
    )


@pytest.mark.asyncio
async def test_send_text_email_sends_and_returns_gmail_id():
    """La rama email envía por Gmail (no es no-op) y devuelve el message_id que
    luego el llamante guarda para idempotencia."""
    import app.services.channel_sender as cs

    conv = _email_conv()
    fake_gmail = MagicMock()
    fake_gmail.send_message = AsyncMock(
        return_value={"message_id": "gmail_sent_99", "thread_id": "thread_abc"}
    )

    headers = {
        "to_addr": "ana@example.com",
        "subject": "Consulta original",
        "in_reply_to": "<rfc-1@x>",
        "references": "<rfc-0@x> <rfc-1@x>",
    }

    with patch.object(cs, "_gather_email_reply_headers", AsyncMock(return_value=headers)), \
        patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
        ext_id = await cs.send_text_to_conversation(conv, "respuesta de la operadora")

    assert ext_id == "gmail_sent_99"
    fake_gmail.send_message.assert_awaited_once()
    kwargs = fake_gmail.send_message.await_args.kwargs
    assert kwargs["thread_id"] == "thread_abc"
    assert kwargs["to_addr"] == "ana@example.com"
    assert kwargs["in_reply_to"] == "<rfc-1@x>"
    assert kwargs["references"] == "<rfc-0@x> <rfc-1@x>"
    assert kwargs["body_text"] == "respuesta de la operadora"


@pytest.mark.asyncio
async def test_send_text_email_no_recipient_avisa_en_vez_de_callar():
    """Sin dirección de respuesta no se puede enviar, y hay que DECIRLO.

    Antes se devolvía "" y el llamante persistía el mensaje como enviado: la
    operadora veía su respuesta en el hilo y el cliente no recibía nada."""
    import app.services.channel_sender as cs

    conv = _email_conv()
    fake_gmail = MagicMock()
    fake_gmail.send_message = AsyncMock()

    headers = {"to_addr": "", "subject": "x", "in_reply_to": None, "references": None}

    with patch.object(cs, "_gather_email_reply_headers", AsyncMock(return_value=headers)), \
        patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
        with pytest.raises(cs.SendNotConfirmed):
            await cs.send_text_to_conversation(conv, "texto")

    fake_gmail.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_text_email_gmail_error_avisa_en_vez_de_callar():
    """Si Gmail falla, el panel tiene que enterarse: con "" se guardaba el
    mensaje como enviado y la conversación quedaba dada por contestada."""
    import app.services.channel_sender as cs

    conv = _email_conv()
    fake_gmail = MagicMock()
    fake_gmail.send_message = AsyncMock(side_effect=RuntimeError("boom"))

    headers = {
        "to_addr": "ana@example.com",
        "subject": "x",
        "in_reply_to": None,
        "references": None,
    }

    with patch.object(cs, "_gather_email_reply_headers", AsyncMock(return_value=headers)), \
        patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
        with pytest.raises(cs.SendNotConfirmed):
            await cs.send_text_to_conversation(conv, "texto")


# ---------------------------------------------------------------------------
# 3) parse_get_message_result expone label_ids
# ---------------------------------------------------------------------------


def test_parse_exposes_label_ids():
    from app.providers.gmail.client import parse_get_message_result

    raw = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["SENT", "INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "negocio@gmail.com"},
                {"name": "To", "value": "ana@x.com"},
                {"name": "Subject", "value": "Re: hola"},
            ],
            "body": {"data": base64.urlsafe_b64encode(b"hola").decode().rstrip("=")},
        },
    }
    result = parse_get_message_result(raw)
    assert result["label_ids"] == ["SENT", "INBOX"]


def test_parse_label_ids_empty_when_absent():
    from app.providers.gmail.client import parse_get_message_result

    raw = {"id": "m1", "threadId": "t1", "payload": {"headers": []}}
    result = parse_get_message_result(raw)
    assert result["label_ids"] == []


# ---------------------------------------------------------------------------
# 4) Ruteo SENT vs DRAFT vs entrante en _process_message_ids
# ---------------------------------------------------------------------------


def _normalized(mid: str, sender: str, labels: list[str]):
    """Dict normalizado tal y como lo devuelve gmail.get_message."""
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "from_addr": sender,
        "from_name": None,
        "to_addr": "x@x.com",
        "subject": "asunto",
        "rfc822_message_id": f"<{mid}@x>",
        "in_reply_to": None,
        "references": None,
        "body": f"cuerpo {mid}",
        "html": "",
        "attachments": [],
        "label_ids": labels,
    }


@pytest.mark.asyncio
async def test_process_routes_sent_draft_and_incoming():
    """SENT (de la cuenta) → store_outgoing_email; DRAFT → se salta; sin SENT →
    se salta; entrante → store_incoming + encola agente."""
    import app.services.gmail_ingest as ingest

    account = "negocio@gmail.com"
    messages = {
        "sent1": _normalized("sent1", account, ["SENT"]),
        "draft1": _normalized("draft1", account, ["DRAFT"]),
        "unsent1": _normalized("unsent1", account, []),  # de la cuenta pero sin SENT
        "cli1": _normalized("cli1", "cliente@example.com", ["INBOX"]),
    }

    fake_gmail = MagicMock()
    fake_gmail.get_message = AsyncMock(side_effect=lambda mid: messages[mid])

    outgoing_calls: list = []
    incoming_calls: list = []

    async def fake_outgoing(incoming, label_ids):
        outgoing_calls.append((incoming, label_ids))
        return uuid.uuid4()

    async def fake_incoming(incoming):
        incoming_calls.append(incoming)
        return uuid.uuid4()

    fake_task = MagicMock()

    with patch.object(ingest, "get_gmail_provider", return_value=fake_gmail), \
        patch.object(ingest, "store_outgoing_email", side_effect=fake_outgoing), \
        patch.object(ingest, "store_incoming", side_effect=fake_incoming), \
        patch.object(ingest, "task_process", fake_task):
        stored, failed = await ingest._process_message_ids(
            ["sent1", "draft1", "unsent1", "cli1"], account
        )

    # Se almacenan: 1 saliente (sent1) + 1 entrante (cli1) = 2.
    assert stored == 2
    # Ninguno falló, así que no hay nada para la cola de reintentos.
    assert failed == {}
    # store_outgoing_email se llamó SOLO con el SENT.
    assert len(outgoing_calls) == 1
    assert outgoing_calls[0][0].provider_message_id == "sent1"
    assert "SENT" in outgoing_calls[0][1]
    # store_incoming se llamó SOLO con el entrante del cliente.
    assert len(incoming_calls) == 1
    assert incoming_calls[0].from_phone == "email:cliente@example.com"
    # El agente se encola SOLO para el entrante (1 vez), NUNCA para salientes.
    fake_task.delay.assert_called_once()


@pytest.mark.asyncio
async def test_process_draft_does_not_store_or_enqueue():
    """Un borrador del agente (DRAFT) NO se ingiere ni encola al agente."""
    import app.services.gmail_ingest as ingest

    account = "negocio@gmail.com"
    fake_gmail = MagicMock()
    fake_gmail.get_message = AsyncMock(
        return_value=_normalized("draft1", account, ["DRAFT"])
    )

    fake_task = MagicMock()

    with patch.object(ingest, "get_gmail_provider", return_value=fake_gmail), \
        patch.object(ingest, "store_outgoing_email", AsyncMock()) as so, \
        patch.object(ingest, "store_incoming", AsyncMock()) as si, \
        patch.object(ingest, "task_process", fake_task):
        stored, failed = await ingest._process_message_ids(["draft1"], account)

    assert stored == 0
    assert failed == {}
    so.assert_not_called()
    si.assert_not_called()
    fake_task.delay.assert_not_called()


# ---------------------------------------------------------------------------
# 5) store_outgoing_email (DB-backed, gated)
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


def _outgoing_incoming(thread_id: str, msg_id: str):
    """IncomingMessage que representa un SALIENTE de la cuenta (from = cuenta)."""
    from app.providers.whatsapp.base import IncomingMessage

    return IncomingMessage(
        provider_message_id=msg_id,
        from_phone="email:negocio@gmail.com",
        to_phone="email:cliente@example.com",
        message_type="text",
        text=f"respuesta enviada {msg_id}",
        customer_name=None,
        raw={
            "threadId": thread_id,
            "subject": "Re: Consulta",
            "rfc822_message_id": f"<{msg_id}@x>",
            "in_reply_to": "<rfc-in@x>",
            "references": "<rfc-in@x>",
            "attachments": [],
            "html": "",
        },
    )


@pytestmark_db
@pytest.mark.asyncio
async def test_store_outgoing_inserts_assistant_in_followed_thread():
    """SENT en hilo YA SEGUIDO → inserta Message rol=assistant, sent_by=gmail."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.services.conversation import store_outgoing_email

    sender = f"out-test-{uuid.uuid4().hex[:8]}@example.com"
    thread = f"thr-{uuid.uuid4().hex[:8]}"

    # Hilo seguido: sembramos contact + conv con ese gmail_thread_id.
    async with db_session() as db:
        contact = Contact(
            telefono=f"email:{sender}", email=sender, origen=ContactOrigen.email, in_crm=False
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.email,
            session_id=f"email:{sender}",
            status=ConversationStatus.bot,
            gmail_thread_id=thread,
            subject="Consulta",
        )
        db.add(conv)
        await db.commit()
        conv_id = conv.id

    msg_id = await store_outgoing_email(
        _outgoing_incoming(thread, f"sent-{uuid.uuid4().hex}"), ["SENT"]
    )
    assert msg_id is not None

    async with db_session() as db:
        out = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conv_id, Message.rol == MessageRole.assistant)
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    assert out is not None
    assert (out.extra or {}).get("sent_by") == "gmail"
    assert "respuesta enviada" in (out.contenido or "")


@pytestmark_db
@pytest.mark.asyncio
async def test_store_outgoing_unknown_thread_returns_none():
    """Hilo NO seguido → None (no creamos conversación desde saliente suelto)."""
    from app.services.conversation import store_outgoing_email

    thread = f"thr-unknown-{uuid.uuid4().hex}"
    msg_id = await store_outgoing_email(
        _outgoing_incoming(thread, f"sent-{uuid.uuid4().hex}"), ["SENT"]
    )
    assert msg_id is None


@pytestmark_db
@pytest.mark.asyncio
async def test_store_outgoing_idempotent_on_provider_message_id():
    """provider_message_id ya existente (p. ej. guardado al enviar desde el
    panel) → store_outgoing_email NO duplica (devuelve None)."""
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.services.conversation import store_outgoing_email

    sender = f"idem-test-{uuid.uuid4().hex[:8]}@example.com"
    thread = f"thr-{uuid.uuid4().hex[:8]}"
    gmail_id = f"gmail-{uuid.uuid4().hex}"

    # Sembramos hilo seguido + un Message saliente que YA tiene ese
    # provider_message_id (simula la respuesta enviada desde el panel).
    async with db_session() as db:
        contact = Contact(
            telefono=f"email:{sender}", email=sender, origen=ContactOrigen.email, in_crm=False
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.email,
            session_id=f"email:{sender}",
            status=ConversationStatus.bot,
            gmail_thread_id=thread,
            subject="Consulta",
        )
        db.add(conv)
        await db.flush()
        db.add(
            Message(
                conversation_id=conv.id,
                rol=MessageRole.operator,
                contenido="Respuesta desde el panel",
                extra={"sent_by": "operator", "provider_message_id": gmail_id},
            )
        )
        await db.commit()
        conv_id = conv.id

    # El poller ve el mismo correo en "Enviados" con ese provider_message_id.
    msg_id = await store_outgoing_email(_outgoing_incoming(thread, gmail_id), ["SENT"])
    assert msg_id is None  # no duplica

    # Sigue habiendo un único saliente en el hilo.
    from sqlalchemy import func, select

    async with db_session() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conv_id,
                    Message.extra["provider_message_id"].astext == gmail_id,
                )
            )
        ).scalar_one()
    assert count == 1
