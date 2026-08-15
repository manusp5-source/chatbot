"""Tests del autoaprendizaje Fase 2 (huecos de conocimiento).

Cobertura:
  - Round-trip cifrado del modelo KnowledgeGap (DB-gated): pregunta y respuesta
    propuesta se guardan cifradas (BYTEA) y se leen en claro vía EncryptedText;
    el resto de campos (trigger, status) se persisten correctamente.

NO añadimos aquí un test de cabeza única de Alembic: el de
`test_agent_correction.py::test_alembic_single_head` ya afirma que hay UNA sola
cabeza y cubre también esta migración (0030_knowledge_gap).

DB-gated con skipif (mismo patrón que test_agent_correction.py): los casos que
tocan BD se saltan si no hay Postgres disponible (se ejecutan en CI con Postgres).
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


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


@pytestmark_db
def test_knowledge_gap_roundtrip_encrypted():
    """Un hueco se guarda y se relee con los textos descifrados."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.knowledge_gap import KnowledgeGap

    telefono = f"web:test-{uuid.uuid4().hex[:8]}"

    async def _run():
        async with db_session() as db:
            contact = Contact(telefono=telefono, origen=ContactOrigen.web, in_crm=False)
            db.add(contact)
            await db.flush()
            conv = Conversation(
                contact_id=contact.id,
                canal=ConversationCanal.web,
                session_id=uuid.uuid4().hex,
                status=ConversationStatus.bot,
            )
            db.add(conv)
            await db.flush()
            gap = KnowledgeGap(
                trigger="kb_miss",
                conversation_id=conv.id,
                question="¿Hacéis envíos a Canarias?",
            )
            db.add(gap)
            await db.commit()
            conv_id, gap_id = conv.id, gap.id

        try:
            async with db_session() as db:
                got = (
                    await db.execute(
                        select(KnowledgeGap).where(KnowledgeGap.id == gap_id)
                    )
                ).scalar_one()
                assert got.trigger == "kb_miss"
                assert got.status == "pendiente"
                assert got.question.startswith("¿Hacéis envíos")
                assert got.suggested_answer is None
                assert got.conversation_id == conv_id
                assert got.resolved_at is None

                # Simula la aprobación: se rellena la respuesta (cifrada).
                got.suggested_answer = "Sí, enviamos a toda España incluidas Canarias."
                got.status = "aprobado"
                await db.commit()

            async with db_session() as db:
                got2 = (
                    await db.execute(
                        select(KnowledgeGap).where(KnowledgeGap.id == gap_id)
                    )
                ).scalar_one()
                assert got2.status == "aprobado"
                assert "Canarias" in (got2.suggested_answer or "")
        finally:
            async with db_session() as db:
                c = (
                    await db.execute(select(Contact).where(Contact.telefono == telefono))
                ).scalar_one_or_none()
                if c:
                    # El hueco tiene conversation_id ON DELETE SET NULL, así que
                    # al borrar el contacto (cascade → conversación) el hueco
                    # queda huérfano; lo borramos explícitamente.
                    await db.delete(
                        (
                            await db.execute(
                                select(KnowledgeGap).where(KnowledgeGap.id == gap_id)
                            )
                        ).scalar_one()
                    )
                    await db.delete(c)
                    await db.commit()

    asyncio.run(_run())
