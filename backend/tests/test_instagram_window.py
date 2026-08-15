"""Ventana de mensajería de Instagram (política de Meta) — evita el baneo.

Cubre:
  - El provider construye el payload correcto: dentro de 24h va como respuesta
    estándar; entre 24h y 7 días usa la etiqueta HUMAN_AGENT.
  - `send_text_to_conversation` corta el envío (MessagingWindowClosed) pasados
    los 7 días en vez de enviar y arriesgar una restricción de la cuenta.
  - `instagram_window_state` clasifica bien standard / human_agent / closed
    según la antigüedad del último mensaje entrante (DB-gated).
"""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers.instagram.meta import IGCredentials, InstagramProvider


def _creds(business: bool) -> IGCredentials:
    return IGCredentials(
        page_id="123",
        page_access_token="TOKEN",
        verify_token="v",
        app_secret="s",
        is_business_login=business,
    )


def _mock_httpx_post(captured: dict):
    """Devuelve un AsyncClient falso que captura el JSON del POST."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"message_id": "mid_out_1"}

    async def _post(url, params=None, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return resp

    client = MagicMock()
    client.post = AsyncMock(side_effect=_post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_send_standard_uses_response_type_legacy():
    """API antigua, dentro de 24h → messaging_type=RESPONSE, sin tag."""
    captured: dict = {}
    with patch("app.providers.instagram.meta.get_ig_credentials",
               AsyncMock(return_value=_creds(business=False))), \
        patch("httpx.AsyncClient", return_value=_mock_httpx_post(captured)):
        out = await InstagramProvider().send_text("ig:PSID1", "hola", human_agent=False)

    assert out == "mid_out_1"
    assert captured["json"]["messaging_type"] == "RESPONSE"
    assert "tag" not in captured["json"]


@pytest.mark.asyncio
async def test_send_human_agent_uses_tag_legacy():
    """API antigua, entre 24h y 7 días → MESSAGE_TAG + HUMAN_AGENT."""
    captured: dict = {}
    with patch("app.providers.instagram.meta.get_ig_credentials",
               AsyncMock(return_value=_creds(business=False))), \
        patch("httpx.AsyncClient", return_value=_mock_httpx_post(captured)):
        await InstagramProvider().send_text("ig:PSID1", "hola", human_agent=True)

    assert captured["json"]["messaging_type"] == "MESSAGE_TAG"
    assert captured["json"]["tag"] == "HUMAN_AGENT"


@pytest.mark.asyncio
async def test_send_human_agent_business_login_tag_only():
    """API nueva (Instagram Login) → tag HUMAN_AGENT sin messaging_type."""
    captured: dict = {}
    with patch("app.providers.instagram.meta.get_ig_credentials",
               AsyncMock(return_value=_creds(business=True))), \
        patch("httpx.AsyncClient", return_value=_mock_httpx_post(captured)):
        await InstagramProvider().send_text("ig:PSID1", "hola", human_agent=True)

    assert captured["json"]["tag"] == "HUMAN_AGENT"
    assert "messaging_type" not in captured["json"]


@pytest.mark.asyncio
async def test_send_text_to_conversation_closed_raises():
    """Instagram pasados 7 días → MessagingWindowClosed (no se envía). El corte
    ocurre ANTES de tocar la BD del contacto, así que no requiere Postgres."""
    import app.services.channel_sender as cs
    from app.models.conversation import ConversationCanal

    conv = SimpleNamespace(
        id=uuid.uuid4(), contact_id=uuid.uuid4(), canal=ConversationCanal.instagram_dm
    )
    with patch.object(cs, "instagram_window_state", AsyncMock(return_value="closed")):
        with pytest.raises(cs.MessagingWindowClosed):
            await cs.send_text_to_conversation(conv, "texto tardío")


# ---------------------------------------------------------------------------
# instagram_window_state (DB-gated): clasifica por antigüedad del entrante.
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    try:
        async def _check():
            from sqlalchemy import text
            from app.db.session import db_session
            async with db_session() as db:
                await db.execute(text("SELECT 1"))
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
@pytest.mark.parametrize(
    "age_hours,expected",
    [(1, "standard"), (48, "human_agent"), (24 * 8, "closed")],
)
async def test_window_state_by_age(age_hours, expected):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select, update

    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.providers.whatsapp import IncomingMessage
    from app.services.channel_sender import instagram_window_state
    from app.services.conversation import store_incoming

    customer_id = f"ig:WIN{uuid.uuid4().hex[:10]}"
    msg_id = await store_incoming(
        IncomingMessage(
            provider_message_id=f"mid.{uuid.uuid4().hex}",
            from_phone=customer_id,
            to_phone="ig:BIZ",
            message_type="text",
            text="Hola",
            audio_url=None,
            audio_mime=None,
            customer_name=None,
            raw={},
        )
    )
    assert msg_id is not None

    # Envejecemos el mensaje entrante al escenario pedido.
    old_ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    async with db_session() as db:
        await db.execute(update(Message).where(Message.id == msg_id).values(created_at=old_ts))
        await db.commit()
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.id == (
                    await db.execute(select(Message.conversation_id).where(Message.id == msg_id))
                ).scalar_one())
            )
        ).scalar_one()

    assert await instagram_window_state(conv) == expected
