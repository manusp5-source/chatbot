"""El corte por presupuesto no se lo puede saltar la lista de demo.

La lista de demo existe para poder enseñar el bot con los canales apagados: se
salta la pausa MANUAL a propósito. Pero cuando la pausa la ha puesto el corte
por gasto, saltársela es seguir gastando por encima del tope — y justo las
conversaciones de demo son las que más se usan.

Aquí se comprueba el consumidor (`services/conversation.py`) de la marca que
deja `services/budget.is_budget_pause_active`, en los dos sitios donde el
runtime decide si sigue: la entrada del mensaje y el drenado del buffer.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


needs_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


def _conv_falsa(canal: str = "whatsapp"):
    return SimpleNamespace(id=uuid.uuid4(), canal=SimpleNamespace(value=canal))


def _incoming(phone: str, **kw):
    from app.providers.whatsapp.base import IncomingMessage

    base = dict(
        provider_message_id=f"wamid.{uuid.uuid4().hex}",
        from_phone=phone,
        to_phone="+34900000000",
        message_type="text",
        text="Hola",
    )
    base.update(kw)
    return IncomingMessage(**base)


def _phone() -> str:
    return f"+3464{uuid.uuid4().int % 10_000_000:07d}"


# ---------------------------------------------------------------------------
# La decisión, aislada
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sin_pausa_no_corta():
    from app.services import conversation as conv_mod

    with patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=False)):
        assert await conv_mod._pause_block_reason(_conv_falsa()) is None


@pytest.mark.asyncio
async def test_la_pausa_manual_se_sigue_saltando_con_la_lista_de_demo():
    """Lo de siempre: pausa puesta a mano + conversación de demo → responde."""
    from app.services import conversation as conv_mod

    with patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_budget_pause_active", AsyncMock(return_value=False)), \
        patch.object(conv_mod, "is_in_demo_whitelist", AsyncMock(return_value=True)):
        assert await conv_mod._pause_block_reason(_conv_falsa()) is None


@pytest.mark.asyncio
async def test_pausa_manual_sin_demo_corta():
    from app.services import conversation as conv_mod

    with patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_budget_pause_active", AsyncMock(return_value=False)), \
        patch.object(conv_mod, "is_in_demo_whitelist", AsyncMock(return_value=False)):
        motivo = await conv_mod._pause_block_reason(_conv_falsa())
    assert motivo and "demo" in motivo


@pytest.mark.asyncio
async def test_el_corte_por_presupuesto_no_lo_salta_ni_la_demo():
    from app.services import conversation as conv_mod

    demo = AsyncMock(return_value=True)
    with patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_budget_pause_active", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_in_demo_whitelist", demo):
        motivo = await conv_mod._pause_block_reason(_conv_falsa())

    assert motivo, "una conversación de demo seguía gastando con el tope superado"
    assert "presupuesto" in motivo


# ---------------------------------------------------------------------------
# Los dos sitios del runtime
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_a_la_entrada_no_se_encola_nada_con_el_tope_superado():
    from app.services import conversation as conv_mod
    from app.services import message_buffer
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus

    phone = _phone()
    msg_id = await conv_mod.store_incoming(_incoming(phone, text="hola"))
    assert msg_id is not None

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        await db.commit()

    empujados: list[tuple] = []
    decisiones: list[str] = []

    async def _fake_push(key, mid):
        empujados.append((key, mid))

    async def _fake_decision(**kw):
        decisiones.append(kw.get("decision"))

    with patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_budget_pause_active", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_in_demo_whitelist", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "log_router_decision", _fake_decision), \
        patch.object(message_buffer, "push_message", _fake_push):
        await conv_mod.handle_incoming_message(msg_id)

    assert empujados == [], "el mensaje se encoló pese al corte por presupuesto"
    assert decisiones == ["skip_paused"], f"cortó por otro motivo: {decisiones}"


@needs_db
@pytest.mark.asyncio
async def test_en_el_drenado_tampoco_se_llama_al_modelo():
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.services import conversation as conv_mod
    from app.services import message_buffer
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    phone = _phone()
    msg_id = await conv_mod.store_incoming(_incoming(phone, text="hola"))
    assert msg_id is not None

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.spam_reviewed = True
        await db.commit()
        conv_id = conv.id

    buffer_key = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(buffer_key, str(msg_id))

    run = AsyncMock(return_value="no debería llamarse")
    decisiones: list[str] = []

    async def _fake_decision(**kw):
        decisiones.append(kw.get("decision"))

    with patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_budget_pause_active", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_in_demo_whitelist", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_channel_training", AsyncMock(return_value=False)), \
        patch.object(conv_mod, "log_router_decision", _fake_decision), \
        patch.object(conv_mod, "run_agent", run), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await conv_mod.process_buffered_messages(buffer_key)

    run.assert_not_called()
    assert decisiones == ["skip_paused"], f"cortó por otro motivo: {decisiones}"
