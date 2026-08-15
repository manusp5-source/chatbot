"""Borrado completo (RGPD Art. 17) y retención de audios (5.1.e). DB-gated.

Usa rutas de audio temporales (AUDIO_STORAGE_PATH) para crear ficheros reales
y comprobar que se borran.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

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
async def test_erase_contact_removes_files_gaps_and_redis(tmp_path, monkeypatch):
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.knowledge_gap import KnowledgeGap
    from app.models.message import Message, MessageRole
    from app.services import data_erasure

    monkeypatch.setattr(settings, "AUDIO_STORAGE_PATH", str(tmp_path))

    mid = uuid.uuid4()
    (tmp_path / f"{mid}.ogg").write_bytes(b"AUDIO")
    async with db_session() as db:
        c = Contact(telefono=f"+34{uuid.uuid4().hex[:9]}", origen=ContactOrigen.whatsapp, in_crm=True)
        db.add(c)
        await db.flush()
        cid, phone = c.id, c.telefono
        conv = Conversation(
            contact_id=c.id, canal=ConversationCanal.whatsapp, session_id="s",
            status=ConversationStatus.bot,
        )
        db.add(conv)
        await db.flush()
        conv_id = conv.id
        db.add(
            Message(
                id=mid, conversation_id=conv_id, rol=MessageRole.user,
                contenido="hola", audio_url=f"/audios/{mid}.ogg", audio_transcript="hola",
            )
        )
        db.add(KnowledgeGap(trigger="handoff", conversation_id=conv_id, question="pregunta"))
        await db.commit()

    from app.core.redis import get_redis
    await get_redis().set(f"guard:blocked:{phone}", "1")

    async def get_redis_check(tel):
        return await get_redis().get(f"guard:blocked:{tel}")

    async with db_session() as db:
        report = await data_erasure.erase_contact(db, cid)
        # Las claves de Redis NO se tocan dentro de la transacción: si el commit
        # fallara, se habrían borrado buffers de un contacto que sigue vivo. Se
        # limpian en `finish_erasure`, después de confirmar.
        assert await get_redis_check(phone) is not None, (
            "la limpieza de Redis se está haciendo antes del commit: el borrado "
            "deja de ser todo-o-nada"
        )
        await db.commit()
    await data_erasure.finish_erasure(report)

    assert report["deleted"]
    assert report["files_deleted"] == 1
    assert report["knowledge_gaps_deleted"] == 1
    assert not (tmp_path / f"{mid}.ogg").exists()
    async with db_session() as db:
        assert (
            await db.execute(select(Contact).where(Contact.id == cid))
        ).scalar_one_or_none() is None
        assert (
            await db.execute(select(KnowledgeGap).where(KnowledgeGap.conversation_id == conv_id))
        ).scalar_one_or_none() is None
    assert await get_redis().get(f"guard:blocked:{phone}") is None


@pytestmark_db
@pytest.mark.asyncio
async def test_purge_old_audio_keeps_transcript(tmp_path, monkeypatch):
    from sqlalchemy import select, text

    from app.core.config import settings
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.services.data_erasure import purge_old_audio_files

    monkeypatch.setattr(settings, "AUDIO_STORAGE_PATH", str(tmp_path))

    old_mid = uuid.uuid4()
    (tmp_path / f"{old_mid}.ogg").write_bytes(b"OLD")
    async with db_session() as db:
        c = Contact(telefono=f"+34{uuid.uuid4().hex[:9]}", origen=ContactOrigen.whatsapp, in_crm=False)
        db.add(c)
        await db.flush()
        cid = c.id
        conv = Conversation(
            contact_id=c.id, canal=ConversationCanal.whatsapp, session_id="s2",
            status=ConversationStatus.bot,
        )
        db.add(conv)
        await db.flush()
        db.add(
            Message(
                id=old_mid, conversation_id=conv.id, rol=MessageRole.user, contenido=None,
                audio_url=f"/audios/{old_mid}.ogg", audio_transcript="transcripcion vieja",
                created_at=datetime.now(timezone.utc) - timedelta(days=400),
            )
        )
        await db.commit()

    try:
        res = await purge_old_audio_files(retention_days=270)
        assert res["purged"] >= 1
        assert not (tmp_path / f"{old_mid}.ogg").exists()
        async with db_session() as db:
            m = (await db.execute(select(Message).where(Message.id == old_mid))).scalar_one()
            assert m.audio_url is None
            assert m.media_purged_at is not None
            assert m.audio_transcript == "transcripcion vieja"  # el texto se conserva
    finally:
        async with db_session() as db:
            await db.execute(text("DELETE FROM contacts WHERE id = :i"), {"i": str(cid)})
            await db.commit()
