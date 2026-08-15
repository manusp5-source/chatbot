"""Tests de la retención/purga del canal Email (Gmail).

Modelo: Gmail es el ARCHIVO, la BD una CACHÉ. A los N meses se purga el
CONTENIDO del correo de la BD (contenido="", sin html_body, purged=true)
conservando los metadatos ligeros (provider_message_id, subject, cabeceras) para
el histórico y la recuperación bajo demanda desde Gmail.

Cobertura (DB-gated con skipif, como el resto de tests con DB real):
  - purge_old_emails:
      * correo email VIEJO con contenido → purgado (vacío, sin html_body,
        purged=true, conserva provider_message_id + subject).
      * correo email RECIENTE → intacto.
      * correo NO-email viejo → intacto (solo afecta a email).
      * correo ya purgado → se salta (idempotencia).
  - endpoint recover:
      * mensaje purgado, Gmail mockeado → devuelve contenido y NO modifica la
        fila (sigue purged en BD, contenido sigue vacío).
      * mensaje NO purgado → 409.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_gmail_draft.py / test_gmail_sent.py)
# ---------------------------------------------------------------------------


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


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


async def _seed_email_message(
    *,
    canal,
    created_at: datetime,
    contenido: str | None,
    extra: dict,
):
    """Siembra contact + conversation (del canal dado) + un Message y devuelve
    (conv_id, message_id). created_at se fija explícitamente (la columna tiene
    server_default=now, así que lo seteamos tras el flush)."""
    from sqlalchemy import update

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.message import Message, MessageRole

    sender = f"ret-{uuid.uuid4().hex[:8]}@example.com"
    async with db_session() as db:
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
            canal=canal,
            session_id=f"sess:{sender}",
            status=ConversationStatus.humano,
            subject="Asunto de prueba",
        )
        db.add(conv)
        await db.flush()
        msg = Message(
            conversation_id=conv.id,
            rol=MessageRole.user,
            contenido=contenido,
            extra=extra,
        )
        db.add(msg)
        await db.flush()
        # Fijamos created_at explícitamente (sortear el server_default).
        await db.execute(
            update(Message).where(Message.id == msg.id).values(created_at=created_at)
        )
        await db.commit()
        return conv.id, msg.id


@pytestmark_db
@pytest.mark.asyncio
async def test_purge_old_email_with_content():
    """Correo email VIEJO con contenido → queda purgado: contenido vacío, sin
    html_body, purged=true y conserva provider_message_id + subject."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import ConversationCanal
    from app.models.message import Message
    from app.services.gmail_retention import purge_old_emails

    old = datetime.now(timezone.utc) - timedelta(days=400)
    _conv_id, msg_id = await _seed_email_message(
        canal=ConversationCanal.email,
        created_at=old,
        contenido="Cuerpo confidencial del correo que debe purgarse.",
        extra={
            "provider_message_id": "gmail_msg_123",
            "subject": "Consulta importante",
            "rfc822_message_id": "<rfc-1@x>",
            "in_reply_to": "<rfc-0@x>",
            "references": "<rfc-0@x>",
            "html_body": "<p>Cuerpo <b>HTML</b> confidencial</p>",
        },
    )

    result = await purge_old_emails()
    assert result["purged"] >= 1

    async with db_session() as db:
        msg = (await db.execute(select(Message).where(Message.id == msg_id))).scalar_one()

    extra = msg.extra or {}
    # Contenido vaciado y sin html_body.
    assert msg.contenido == ""
    assert "html_body" not in extra
    # Marcado como purgado con timestamp.
    assert extra.get("purged") is True
    assert isinstance(extra.get("purged_at"), str) and extra["purged_at"]
    # Metadatos ligeros conservados (para histórico + recuperación).
    assert extra.get("provider_message_id") == "gmail_msg_123"
    assert extra.get("subject") == "Consulta importante"
    assert extra.get("rfc822_message_id") == "<rfc-1@x>"


@pytestmark_db
@pytest.mark.asyncio
async def test_recent_email_not_purged():
    """Correo email RECIENTE (dentro del umbral) → NO se toca."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import ConversationCanal
    from app.models.message import Message
    from app.services.gmail_retention import purge_old_emails

    recent = datetime.now(timezone.utc) - timedelta(days=10)
    _conv_id, msg_id = await _seed_email_message(
        canal=ConversationCanal.email,
        created_at=recent,
        contenido="Correo reciente, NO purgar.",
        extra={
            "provider_message_id": "gmail_recent",
            "subject": "Reciente",
            "html_body": "<p>html reciente</p>",
        },
    )

    await purge_old_emails()

    async with db_session() as db:
        msg = (await db.execute(select(Message).where(Message.id == msg_id))).scalar_one()

    assert msg.contenido == "Correo reciente, NO purgar."
    extra = msg.extra or {}
    assert extra.get("purged") is not True
    assert extra.get("html_body") == "<p>html reciente</p>"


@pytestmark_db
@pytest.mark.asyncio
async def test_non_email_old_not_purged():
    """Correo NO-email viejo (p. ej. WhatsApp) → intacto: la retención solo
    afecta al canal email."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import ConversationCanal
    from app.models.message import Message
    from app.services.gmail_retention import purge_old_emails

    old = datetime.now(timezone.utc) - timedelta(days=400)
    _conv_id, msg_id = await _seed_email_message(
        canal=ConversationCanal.whatsapp,
        created_at=old,
        contenido="Mensaje de WhatsApp antiguo, NO purgar.",
        extra={"provider_message_id": "wa_old"},
    )

    await purge_old_emails()

    async with db_session() as db:
        msg = (await db.execute(select(Message).where(Message.id == msg_id))).scalar_one()

    assert msg.contenido == "Mensaje de WhatsApp antiguo, NO purgar."
    assert (msg.extra or {}).get("purged") is not True


@pytestmark_db
@pytest.mark.asyncio
async def test_already_purged_is_skipped_idempotent():
    """Correo email viejo YA purgado → la purga lo salta (idempotencia): no
    cambia purged_at ni vuelve a contarlo en el segundo pase."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import ConversationCanal
    from app.models.message import Message
    from app.services.gmail_retention import purge_old_emails

    old = datetime.now(timezone.utc) - timedelta(days=400)
    prev_purged_at = "2025-01-01T00:00:00+00:00"
    _conv_id, msg_id = await _seed_email_message(
        canal=ConversationCanal.email,
        created_at=old,
        contenido="",  # ya vaciado
        extra={
            "provider_message_id": "gmail_done",
            "subject": "Ya purgado",
            "purged": True,
            "purged_at": prev_purged_at,
        },
    )

    await purge_old_emails()

    async with db_session() as db:
        msg = (await db.execute(select(Message).where(Message.id == msg_id))).scalar_one()

    extra = msg.extra or {}
    # No se re-purga: el purged_at original se conserva tal cual.
    assert extra.get("purged") is True
    assert extra.get("purged_at") == prev_purged_at


# ---------------------------------------------------------------------------
# Endpoint recover
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_recover_purged_returns_content_without_repersisting():
    """recover: con Gmail mockeado, un mensaje purgado devuelve el contenido
    traído y NO modifica la fila (sigue purged y con contenido vacío en BD)."""
    from sqlalchemy import select

    from app.api import conversations as conv_api
    from app.db.session import db_session
    from app.models.conversation import ConversationCanal
    from app.models.message import Message
    from app.services.gmail_retention import purge_old_emails

    old = datetime.now(timezone.utc) - timedelta(days=400)
    conv_id, msg_id = await _seed_email_message(
        canal=ConversationCanal.email,
        created_at=old,
        contenido="Contenido original que se purgará.",
        extra={
            "provider_message_id": "gmail_recover_1",
            "subject": "Recuperable",
            "html_body": "<p>html original</p>",
        },
    )
    # Purgamos primero.
    await purge_old_emails()

    fake_gmail = MagicMock()
    fake_gmail.get_message = AsyncMock(
        return_value={
            "body": "Cuerpo recuperado desde Gmail.",
            "html": "<p>html recuperado</p>",
            "subject": "Recuperable",
        }
    )

    async with db_session() as db:
        with patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
            result = await conv_api.recover_email(
                conversation_id=conv_id,
                message_id=msg_id,
                db=db,
                _=SimpleNamespace(id=uuid.uuid4()),
            )

    # Se pidió el mensaje correcto a Gmail y se devolvió el contenido recuperado.
    fake_gmail.get_message.assert_awaited_once_with("gmail_recover_1")
    assert result.contenido == "Cuerpo recuperado desde Gmail."
    assert result.html_body is not None and "html recuperado" in result.html_body

    # La fila NO se re-persiste: sigue purgada y con contenido vacío en BD.
    async with db_session() as db:
        msg = (await db.execute(select(Message).where(Message.id == msg_id))).scalar_one()
    assert msg.contenido == ""
    assert (msg.extra or {}).get("purged") is True
    assert "html_body" not in (msg.extra or {})


@pytestmark_db
@pytest.mark.asyncio
async def test_recover_non_purged_returns_409():
    """recover sobre un mensaje NO purgado → 409 (no es un mensaje archivado)."""
    from fastapi import HTTPException

    from app.api import conversations as conv_api
    from app.db.session import db_session
    from app.models.conversation import ConversationCanal

    recent = datetime.now(timezone.utc) - timedelta(days=5)
    conv_id, msg_id = await _seed_email_message(
        canal=ConversationCanal.email,
        created_at=recent,
        contenido="Correo normal, sin purgar.",
        extra={"provider_message_id": "gmail_live"},
    )

    fake_gmail = MagicMock()
    fake_gmail.get_message = AsyncMock()

    async with db_session() as db:
        with patch("app.providers.gmail.get_gmail_provider", return_value=fake_gmail):
            with pytest.raises(HTTPException) as exc:
                await conv_api.recover_email(
                    conversation_id=conv_id,
                    message_id=msg_id,
                    db=db,
                    _=SimpleNamespace(id=uuid.uuid4()),
                )
    assert exc.value.status_code == 409
    # No se llega a llamar a Gmail si no está purgado.
    fake_gmail.get_message.assert_not_called()
