"""Tests del canal Email (Gmail) — Fase 5a.

Cobertura:
- parse_message_to_incoming: parseo de un payload Gmail de ejemplo (from_phone
  con prefijo email:, subject, threadId, cuerpo text/plain, marcador de adjunto).
- sync_incoming_emails: con el cliente Gmail mockeado, verifica que llama a
  store_incoming por cada mensaje y persiste el last_history_id.
- store_incoming: agrupación por gmail_thread_id (mismo thread → misma
  conversación; distinto thread → distinta). Requiere DB; se salta si no hay.
"""
from __future__ import annotations

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1) parse_message_to_incoming (puro, sin DB ni red)
# ---------------------------------------------------------------------------


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _sample_gmail_message() -> dict:
    """Payload tipo users.messages.get?format=full con cuerpo text/plain y un
    adjunto (que NO debe descargarse, solo marcarse)."""
    return {
        "id": "msg_abc123",
        "threadId": "thread_xyz789",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Ana Cliente <ana@example.com>"},
                {"name": "To", "value": "negocio@gmail.com"},
                {"name": "Subject", "value": "Consulta de presupuesto"},
                {"name": "Message-ID", "value": "<rfc-msg-1@example.com>"},
                {"name": "In-Reply-To", "value": "<rfc-prev@example.com>"},
                {"name": "References", "value": "<rfc-prev@example.com>"},
                {"name": "Date", "value": "Tue, 03 Jun 2026 10:00:00 +0200"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Hola, quiero un presupuesto.")},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "plano.pdf",
                    "body": {"attachmentId": "att_1", "size": 12345},
                },
            ],
        },
    }


def test_parse_message_to_incoming():
    from app.providers.gmail.client import parse_message_to_incoming

    incoming = parse_message_to_incoming(_sample_gmail_message())

    assert incoming.provider_message_id == "msg_abc123"
    assert incoming.from_phone == "email:ana@example.com"
    assert incoming.to_phone == "email:negocio@gmail.com"
    assert incoming.message_type == "text"
    assert incoming.customer_name == "Ana Cliente"
    # Cuerpo text/plain presente
    assert "quiero un presupuesto" in (incoming.text or "")
    # Adjunto marcado SIN descargar
    assert "[adjunto: plano.pdf]" in (incoming.text or "")
    # raw con los metadatos del hilo
    assert incoming.raw is not None
    assert incoming.raw["threadId"] == "thread_xyz789"
    assert incoming.raw["subject"] == "Consulta de presupuesto"
    assert incoming.raw["rfc822_message_id"] == "<rfc-msg-1@example.com>"
    assert incoming.raw["in_reply_to"] == "<rfc-prev@example.com>"
    assert incoming.raw["references"] == "<rfc-prev@example.com>"
    # Los adjuntos ya no son solo el nombre: llevan lo necesario para poder
    # DESCARGARLOS después (attachment_id, tipo, tamaño) y la marca de si son
    # contenido incrustado (logo de firma) o un fichero de verdad.
    assert incoming.raw["attachments"] == [
        {
            "filename": "plano.pdf",
            "mime_type": "application/pdf",
            "size": 12345,
            "attachment_id": "att_1",
            "part_id": None,
            "content_id": None,
            "inline": False,
        }
    ]
    assert incoming.raw["inline_attachments"] == []


def test_parse_message_html_fallback():
    """Si no hay text/plain, cae a text/html y le hace strip básico."""
    from app.providers.gmail.client import parse_message_to_incoming

    raw = {
        "id": "msg_html",
        "threadId": "t1",
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": "bob@example.com"},
                {"name": "To", "value": "negocio@gmail.com"},
                {"name": "Subject", "value": "Hola"},
            ],
            "body": {"data": _b64url("<p>Hola <b>mundo</b></p><br><div>Adios</div>")},
        },
    }
    incoming = parse_message_to_incoming(raw)
    # Sin display name → from_phone usa el address tal cual
    assert incoming.from_phone == "email:bob@example.com"
    text = incoming.text or ""
    assert "Hola" in text and "mundo" in text and "Adios" in text
    assert "<" not in text  # los tags fueron eliminados


# ---------------------------------------------------------------------------
# 2) sync_incoming_emails con cliente Gmail mockeado
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Redis fake mínimo para el lock SET NX y delete."""

    def __init__(self):
        self.store: dict = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


class _FakeChannel:
    def __init__(self, config):
        self.id = uuid.uuid4()
        self.config = dict(config)


@pytest.mark.asyncio
async def test_sync_incoming_emails_bootstrap_calls_store_and_persists_history():
    import app.services.gmail_ingest as ingest

    channel = _FakeChannel(config={})  # sin last_history_id → bootstrap

    fake_gmail = AsyncMock()
    fake_gmail.get_profile = AsyncMock(
        return_value={"email_address": "negocio@gmail.com", "history_id": "1000"}
    )
    fake_gmail.list_messages = AsyncMock(return_value=["m1", "m2", "m3"])
    # get_message devuelve dicts ya normalizados (from_addr distinto de la cuenta)
    fake_gmail.get_message = AsyncMock(
        side_effect=lambda mid: {
            "id": mid,
            "threadId": f"t-{mid}",
            "from_addr": "cliente@example.com",
            "from_name": "Cliente",
            "to_addr": "negocio@gmail.com",
            "subject": "Asunto",
            "rfc822_message_id": f"<{mid}@x>",
            "in_reply_to": None,
            "references": None,
            "body": f"cuerpo {mid}",
            "attachments": [],
        }
    )

    saved_history: dict = {}

    async def fake_save(channel_id, history_id, account_email=None, retry_queue=None):
        saved_history["history_id"] = history_id
        saved_history["email"] = account_email
        saved_history["retry_queue"] = retry_queue

    store_calls: list = []

    async def fake_store(incoming):
        store_calls.append(incoming)
        return uuid.uuid4()

    with patch.object(ingest, "_load_email_channel", AsyncMock(return_value=channel)), \
        patch.object(ingest, "get_gmail_provider", return_value=fake_gmail), \
        patch.object(ingest, "get_redis", return_value=_FakeRedis()), \
        patch.object(ingest, "store_incoming", side_effect=fake_store), \
        patch.object(ingest, "task_process", MagicMock()), \
        patch.object(ingest, "_save_history_id", side_effect=fake_save):
        result = await ingest.sync_incoming_emails()

    assert result["status"] == "bootstrap"
    assert result["stored"] == 3
    # store_incoming llamado una vez por mensaje
    assert len(store_calls) == 3
    assert all(inc.from_phone == "email:cliente@example.com" for inc in store_calls)
    # historyId actual persistido (anclaje para el incremental futuro)
    assert saved_history["history_id"] == "1000"
    assert saved_history["email"] == "negocio@gmail.com"


@pytest.mark.asyncio
async def test_sync_incoming_emails_skips_self_echo():
    """Los correos cuyo From es la propia cuenta conectada se ignoran."""
    import app.services.gmail_ingest as ingest

    channel = _FakeChannel(config={"last_history_id": "500"})

    fake_gmail = AsyncMock()
    fake_gmail.get_profile = AsyncMock(
        return_value={"email_address": "negocio@gmail.com", "history_id": "600"}
    )
    fake_gmail.list_history = AsyncMock(
        return_value={"message_ids": ["self1", "cli1"], "history_id": "600", "expired": False}
    )

    def _msg(mid):
        sender = "negocio@gmail.com" if mid == "self1" else "cliente@example.com"
        return {
            "id": mid,
            "threadId": f"t-{mid}",
            "from_addr": sender,
            "from_name": None,
            "to_addr": "negocio@gmail.com",
            "subject": "x",
            "rfc822_message_id": f"<{mid}>",
            "in_reply_to": None,
            "references": None,
            "body": "hola",
            "attachments": [],
        }

    fake_gmail.get_message = AsyncMock(side_effect=lambda mid: _msg(mid))

    store_calls: list = []

    async def fake_store(incoming):
        store_calls.append(incoming)
        return uuid.uuid4()

    with patch.object(ingest, "_load_email_channel", AsyncMock(return_value=channel)), \
        patch.object(ingest, "get_gmail_provider", return_value=fake_gmail), \
        patch.object(ingest, "get_redis", return_value=_FakeRedis()), \
        patch.object(ingest, "store_incoming", side_effect=fake_store), \
        patch.object(ingest, "task_process", MagicMock()), \
        patch.object(ingest, "_save_history_id", AsyncMock()):
        result = await ingest.sync_incoming_emails()

    assert result["status"] == "incremental"
    # Solo el del cliente se almacena; el auto-eco se ignora.
    assert len(store_calls) == 1
    assert store_calls[0].from_phone == "email:cliente@example.com"


# ---------------------------------------------------------------------------
# 3) Agrupación por gmail_thread_id en store_incoming (DB-backed)
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    """True si podemos abrir una sesión de DB (deps + servidor disponibles)."""
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


def _email_incoming(thread_id: str, msg_id: str, sender: str, subject: str):
    from app.providers.whatsapp.base import IncomingMessage

    return IncomingMessage(
        provider_message_id=msg_id,
        from_phone=f"email:{sender}",
        to_phone="email:negocio@gmail.com",
        message_type="text",
        text=f"cuerpo {msg_id}",
        customer_name=None,
        raw={
            "threadId": thread_id,
            "subject": subject,
            "rfc822_message_id": f"<{msg_id}@x>",
            "in_reply_to": None,
            "references": None,
            "attachments": [],
        },
    )


@pytestmark_db
@pytest.mark.asyncio
async def test_store_incoming_groups_by_gmail_thread():
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.services.conversation import store_incoming

    sender = f"thread-test-{uuid.uuid4().hex[:8]}@example.com"
    thread_a = f"thr-A-{uuid.uuid4().hex[:8]}"
    thread_b = f"thr-B-{uuid.uuid4().hex[:8]}"

    # Dos mensajes del MISMO hilo → misma conversación
    id1 = await store_incoming(_email_incoming(thread_a, f"m1-{uuid.uuid4().hex}", sender, "Hilo A"))
    id2 = await store_incoming(_email_incoming(thread_a, f"m2-{uuid.uuid4().hex}", sender, "Hilo A"))
    # Un mensaje de OTRO hilo → conversación distinta
    id3 = await store_incoming(_email_incoming(thread_b, f"m3-{uuid.uuid4().hex}", sender, "Hilo B"))

    assert id1 and id2 and id3

    async with db_session() as db:
        conv_a = (
            await db.execute(select(Conversation).where(Conversation.gmail_thread_id == thread_a))
        ).scalars().all()
        conv_b = (
            await db.execute(select(Conversation).where(Conversation.gmail_thread_id == thread_b))
        ).scalars().all()

    # Una sola conversación por hilo
    assert len(conv_a) == 1
    assert len(conv_b) == 1
    # Y son distintas
    assert conv_a[0].id != conv_b[0].id
    assert conv_a[0].subject == "Hilo A"
    assert conv_b[0].subject == "Hilo B"
