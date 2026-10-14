"""KPIs de calidad: la aritmética (sin BD) y las consultas SQL (con BD real, DB-gated)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.quality_kpis import percentil, resumen_primera_respuesta

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


# --- aritmética pura ----------------------------------------------------------------


def test_percentil_sin_datos_es_none() -> None:
    assert percentil([], 50) is None


def test_percentil_por_rango_mas_cercano() -> None:
    valores = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentil(valores, 50) == 30.0
    assert percentil(valores, 90) == 50.0
    assert percentil([7.0], 90) == 7.0


def test_resumen_cuenta_respondidas_y_pendientes() -> None:
    pares = [
        (T0, T0 + timedelta(minutes=5)),
        (T0, T0 + timedelta(minutes=15)),
        (T0, T0 + timedelta(minutes=60)),
        (T0, None),  # derivada y sin respuesta humana: una promesa sin cumplir
    ]

    r = resumen_primera_respuesta(pares)

    assert r["derivaciones"] == 4
    assert r["respondidas"] == 3
    assert r["sin_respuesta"] == 1
    assert r["mediana_min"] == 15.0
    assert r["p90_min"] == 60.0


def test_resumen_sin_derivaciones_no_inventa_numeros() -> None:
    r = resumen_primera_respuesta([])

    assert r["derivaciones"] == 0
    assert r["mediana_min"] is None and r["p90_min"] is None


def test_una_respuesta_anterior_a_la_derivacion_no_da_tiempo_negativo() -> None:
    r = resumen_primera_respuesta([(T0, T0 - timedelta(minutes=3))])

    assert r["mediana_min"] == 0.0


# --- SQL contra Postgres real ----------------------------------------------------------


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
def test_calcular_contra_la_base_de_datos_real() -> None:
    """Se siembra una derivación respondida a los 20 min, otra sin respuesta y un hueco de KB.

    Se comparan DELTAS contra lo que ya hubiera en la base: otros tests dejan datos y esta
    prueba no puede depender de que la base esté vacía.
    """
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.knowledge_gap import KnowledgeGap
    from app.models.message import Message, MessageRole
    from app.services.quality_kpis import calcular

    marca = uuid.uuid4().hex[:8]
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(days=1)

    async def _run():
        async with db_session() as db:
            antes = await calcular(db, desde)

        async with db_session() as db:
            contactos = []
            for i in range(2):
                c = Contact(telefono=f"web:kpi-{marca}-{i}", origen=ContactOrigen.web, in_crm=False)
                db.add(c)
                contactos.append(c)
            await db.flush()

            derivada = ahora - timedelta(hours=2)
            convs = []
            for c in contactos:
                conv = Conversation(
                    contact_id=c.id,
                    canal=ConversationCanal.web,
                    session_id=uuid.uuid4().hex,
                    status=ConversationStatus.humano,
                    derivada_a_humano_at=derivada,
                )
                db.add(conv)
                convs.append(conv)
            await db.flush()

            # Solo la primera conversación recibe respuesta humana, 20 min después.
            resp = Message(
                conversation_id=convs[0].id,
                rol=MessageRole.operator,
                contenido="Hola, te escribo yo.",
            )
            db.add(resp)
            await db.flush()
            resp.created_at = derivada + timedelta(minutes=20)

            db.add(KnowledgeGap(trigger="kb_miss", question=f"pregunta kpi {marca}"))
            await db.commit()

        try:
            async with db_session() as db:
                despues = await calcular(db, desde)
        finally:
            async with db_session() as db:
                for c in (
                    await db.execute(
                        select(Contact).where(Contact.telefono.like(f"web:kpi-{marca}-%"))
                    )
                ).scalars():
                    await db.delete(c)
                for g in (
                    await db.execute(
                        select(KnowledgeGap).where(KnowledgeGap.question == f"pregunta kpi {marca}")
                    )
                ).scalars():
                    await db.delete(g)
                await db.commit()
        return antes, despues

    antes, despues = asyncio.run(_run())

    assert despues["conversaciones"] - antes["conversaciones"] == 2
    assert despues["huecos_kb"] - antes["huecos_kb"] == 1
    pa, pd = antes["primera_respuesta_humana"], despues["primera_respuesta_humana"]
    assert pd["derivaciones"] - pa["derivaciones"] == 2
    assert pd["respondidas"] - pa["respondidas"] == 1
    assert pd["sin_respuesta"] - pa["sin_respuesta"] == 1
