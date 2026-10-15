"""Aviso de IA contra Postgres real: qué cuenta como «respuesta previa» y el mensaje puente.

Hallazgos de la review del 14 oct sobre `intent-002`:
  - El mensaje puente de `derivar_humano` contaba como respuesta previa y no llevaba aviso: en
    una conversación que empezaba derivando, el cliente no sabía que hablaba con una IA y el
    aviso ya no se ponía nunca después.
  - Un borrador de Entrenamiento que la operadora envía queda con `is_draft=false` y salía como
    «respuesta previa» sin haber llevado el aviso.
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

TEXTO = "¿En qué te ayudo?"


def _escenario(canal_nombre: str, mensajes: list[dict]):
    """Siembra una conversación con `mensajes` y devuelve (aviso_para_envio, con_aviso_puente)."""
    from sqlalchemy import select

    from app.agents.tools import human_handoff as hh
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole
    from app.services.aviso_ia import aviso_ia_para_envio

    marca = uuid.uuid4().hex[:8]

    async def _run():
        async with db_session() as db:
            c = Contact(telefono=f"web:aviso-{marca}", origen=ContactOrigen.web, in_crm=False)
            db.add(c)
            await db.flush()
            conv = Conversation(
                contact_id=c.id,
                canal=ConversationCanal[canal_nombre],
                session_id=uuid.uuid4().hex,
                status=ConversationStatus.bot,
            )
            db.add(conv)
            await db.flush()
            for m in mensajes:
                db.add(
                    Message(
                        conversation_id=conv.id,
                        rol=MessageRole[m["rol"]],
                        contenido=m.get("contenido", "x"),
                        extra=m.get("extra", {}),
                    )
                )
            await db.commit()
            conv_id = conv.id
        try:
            async with db_session() as db:
                conv = (
                    await db.execute(select(Conversation).where(Conversation.id == conv_id))
                ).scalar_one()
                envio = await aviso_ia_para_envio(db, conv, TEXTO)
            puente = await hh._con_aviso_de_ia(conv, "Te paso con el equipo.")
            return envio, puente
        finally:
            async with db_session() as db:
                for c in (
                    await db.execute(
                        select(Contact).where(Contact.telefono == f"web:aviso-{marca}")
                    )
                ).scalars():
                    await db.delete(c)
                await db.commit()

    return asyncio.run(_run())


@pytestmark_db
def test_conversacion_nueva_lleva_el_aviso_tambien_en_el_puente() -> None:
    from app.services.aviso_ia import AVISO_IA_PRIMERA_INTERACCION

    envio, puente = _escenario("web", [{"rol": "user"}])

    assert envio.startswith(AVISO_IA_PRIMERA_INTERACCION)
    assert puente.startswith(AVISO_IA_PRIMERA_INTERACCION)


@pytestmark_db
def test_si_ya_hubo_una_respuesta_entregada_no_se_repite() -> None:
    envio, puente = _escenario(
        "whatsapp", [{"rol": "user"}, {"rol": "assistant", "extra": {"delivery": "ok"}}]
    )

    assert envio == TEXTO
    assert puente == "Te paso con el equipo."


@pytest.mark.parametrize(
    "extra",
    [
        {"is_draft": True},
        {"is_draft": False, "training": True},  # sugerencia de Entrenamiento ya enviada por la operadora
    ],
)
@pytestmark_db
def test_borradores_y_sugerencias_de_entrenamiento_no_cuentan_como_respuesta(extra) -> None:
    from app.services.aviso_ia import AVISO_IA_PRIMERA_INTERACCION

    envio, _ = _escenario("whatsapp", [{"rol": "user"}, {"rol": "assistant", "extra": extra}])

    assert envio.startswith(AVISO_IA_PRIMERA_INTERACCION)


@pytestmark_db
def test_el_correo_no_lleva_aviso_automatico_ni_en_el_puente() -> None:
    envio, puente = _escenario("email", [{"rol": "user"}])

    assert envio == TEXTO
    assert puente == "Te paso con el equipo."
