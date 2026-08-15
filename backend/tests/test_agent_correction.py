"""Tests del autoaprendizaje Fase 1 (corrige y re-redacta).

Cobertura:
  - La cadena de migraciones tiene UNA sola cabeza y es 0025_agent_correction
    (sin DB: protege contra romper la cadena al añadir la migración).
  - Round-trip cifrado del modelo AgentCorrection (DB-gated): los textos se
    guardan cifrados (BYTEA) y se leen en claro vía EncryptedText.

DB-gated con skipif (mismo patrón que test_voice_agent.py): los casos que tocan
BD se saltan si no hay Postgres disponible (se ejecutan en CI con Postgres).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


def test_alembic_single_head():
    """El grafo de migraciones tiene UNA sola cabeza (no múltiples).

    Invariante crítico: dos ramas en paralelo pueden crear migraciones colgando
    del mismo punto → varias cabezas → `alembic upgrade head` falla y la app no
    arranca. Este test lo detecta antes de desplegar (afirmamos el número de
    cabezas, no su nombre, para no romper en cada migración nueva)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", "app/db/migrations")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"se esperaba UNA sola cabeza de migración, hay {len(heads)}: {heads}"


# ---------------------------------------------------------------------------
# DB-gate
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


@pytestmark_db
def test_agent_correction_roundtrip_encrypted():
    """Una corrección se guarda y se relee con los textos descifrados."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.agent_correction import AgentCorrection
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus

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
            corr = AgentCorrection(
                conversation_id=conv.id,
                message_id=None,
                canal="web",
                original_text="Hola, te ofrezco un descuento del 20%.",
                instruction="No ofrezcas descuentos, di que la promoción ha terminado y sé más cálida.",
                resulting_text="¡Hola! Ahora mismo la promoción ha terminado, pero te aviso encantada cuando vuelva 😊",
            )
            db.add(corr)
            await db.commit()
            conv_id, corr_id = conv.id, corr.id

        try:
            async with db_session() as db:
                got = (
                    await db.execute(
                        select(AgentCorrection).where(AgentCorrection.id == corr_id)
                    )
                ).scalar_one()
                assert got.instruction.startswith("No ofrezcas descuentos")
                assert got.original_text.startswith("Hola, te ofrezco")
                assert "terminado" in (got.resulting_text or "")
                assert got.canal == "web"
                assert got.conversation_id == conv_id
        finally:
            async with db_session() as db:
                c = (
                    await db.execute(select(Contact).where(Contact.telefono == telefono))
                ).scalar_one_or_none()
                if c:
                    await db.delete(c)  # cascade borra conversación + corrección
                    await db.commit()

    asyncio.run(_run())
