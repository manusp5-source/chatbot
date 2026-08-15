"""Resumen rodante de conversaciones largas. DB-gated.

Verifica que se dispara al superar la ventana de contexto, persiste con su
puntero incremental, y NO vuelve a llamar al LLM sin mensajes nuevos.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


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
async def test_rolling_summary_triggers_and_is_incremental():
    from sqlalchemy import select, text

    import app.services.rolling_summary as rs
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole

    async with db_session() as db:
        c = Contact(telefono=f"+34{uuid.uuid4().hex[:9]}", origen=ContactOrigen.whatsapp, in_crm=False)
        db.add(c)
        await db.flush()
        contact_id = c.id
        conv = Conversation(
            contact_id=c.id, canal=ConversationCanal.whatsapp, session_id="s",
            status=ConversationStatus.bot,
        )
        db.add(conv)
        await db.flush()
        cid = conv.id
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        for i in range(35):  # 35 mensajes, ventana 20 → 15 fuera (≥ batch 10)
            db.add(
                Message(
                    conversation_id=cid,
                    rol=MessageRole.user if i % 2 == 0 else MessageRole.assistant,
                    contenido=f"mensaje {i} sobre el pedido de Ana",
                    created_at=base + timedelta(seconds=i),
                )
            )
        await db.commit()

    class FakeResp:
        content = "Ana pregunta por su pedido; el agente le da el estado."

    fake_llm = AsyncMock()
    fake_llm.complete = AsyncMock(return_value=FakeResp())

    try:
        with patch.object(rs, "resolve_llm_provider", AsyncMock(return_value=fake_llm)):
            out = await rs.maybe_update_rolling_summary(
                cid, context_window=20, model="x", llm_provider_id=None
            )
            assert out and "Ana" in out
            assert fake_llm.complete.await_count == 1

            async with db_session() as db:
                conv = (
                    await db.execute(select(Conversation).where(Conversation.id == cid))
                ).scalar_one()
                assert conv.rolling_summary and "Ana" in conv.rolling_summary
                assert conv.rolling_summary_upto == 15

            # Sin mensajes nuevos → no re-resume (coste acotado).
            out2 = await rs.maybe_update_rolling_summary(
                cid, context_window=20, model="x", llm_provider_id=None
            )
            assert fake_llm.complete.await_count == 1
            assert out2 == out
    finally:
        async with db_session() as db:
            await db.execute(text("DELETE FROM contacts WHERE id = :i"), {"i": str(contact_id)})
            await db.commit()
