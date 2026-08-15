"""Tests del desglose de consumo de tokens "por agente" (GET /admin/agent/usage).

Cobertura (DB-gated, mismo patrón que test_template_vars.py):
  - track_usage persiste `agent_id` (normaliza str→UUID; ignora basura).
  - el endpoint agrupa por agente: cada Agente conversacional por su nombre, y
    el clasificador / agente interno / embeddings como sus propios buckets.
  - source='agent' sin agent_id cae en "Agente (sin asignar)".
  - by_agent viene ordenado por total_tokens desc (quién consume más arriba).
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


async def _seed_user():
    from app.db.session import db_session
    from app.models.user import User, UserRole

    async with db_session() as db:
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=UserRole.admin,
            nombre="Operador Test",
        )
        db.add(user)
        await db.flush()
        uid = user.id
        await db.commit()
        return uid


async def _get_user(uid):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.user import User

    async with db_session() as db:
        return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _seed_agent(name: str):
    from app.db.session import db_session
    from app.models.agent import Agent

    async with db_session() as db:
        a = Agent(name=name, prompt_system="x")
        db.add(a)
        await db.flush()
        aid = a.id
        await db.commit()
        return aid


@pytestmark_db
@pytest.mark.asyncio
async def test_usage_breaks_down_by_agent():
    from sqlalchemy import text

    from app.api.admin import agent_usage
    from app.db.session import db_session
    from app.services.usage_tracker import track_usage

    # Aislamiento: este test agrega el consumo GLOBAL por source (clasificador,
    # agente interno, sin-asignar), así que limpiamos la tabla para que las
    # sumas sean deterministas (en CI la BD ya va limpia; aquí evita arrastre).
    async with db_session() as db:
        await db.execute(text("DELETE FROM llm_usage_log"))
        await db.commit()

    user = await _get_user(await _seed_user())
    agent_a = await _seed_agent(f"Agente Texto {uuid.uuid4().hex[:4]}")
    agent_b = await _seed_agent(f"Agente Voz {uuid.uuid4().hex[:4]}")

    # El agente A consume más que el B. agent_id llega como str (viene de un
    # ContextVar) → debe normalizarse a UUID.
    await track_usage("agent", "gpt-5.4-mini", 1000, 200, agent_id=str(agent_a))
    await track_usage("agent", "gpt-5.4-mini", 300, 50, agent_id=str(agent_b))
    # source='agent' sin agent_id → "Agente (sin asignar)".
    await track_usage("agent", "gpt-5.4-mini", 80, 20, agent_id=None)
    # Transversales: se atribuyen por source.
    await track_usage("classifier", "gpt-5.4-nano", 40, 5)
    await track_usage("internal_agent", "gpt-5.4-mini", 60, 10)

    async with db_session() as db:
        out = await agent_usage(range_="30d", db=db, _=user)

    by_label = {b.label: b for b in out.by_agent}
    # Cada agente conversacional aparece por su nombre.
    a_name = next(b.label for b in out.by_agent if b.key == str(agent_a))
    b_name = next(b.label for b in out.by_agent if b.key == str(agent_b))
    assert by_label[a_name].total_tokens == 1200
    assert by_label[b_name].total_tokens == 350
    # Buckets transversales presentes.
    assert by_label["Clasificador"].total_tokens == 45
    assert by_label["Agente interno"].total_tokens == 70
    assert by_label["Agente (sin asignar)"].total_tokens == 100

    # Ordenado por total_tokens desc: el agente A (1200) es el primero.
    assert out.by_agent[0].key == str(agent_a)
    totals = [b.total_tokens for b in out.by_agent]
    assert totals == sorted(totals, reverse=True)

    # by_source sigue funcionando (todas las llamadas 'agent' juntas).
    assert out.by_source["agent"]["total_tokens"] == 1200 + 350 + 100


@pytestmark_db
@pytest.mark.asyncio
async def test_track_usage_ignores_bad_agent_id():
    """Un agent_id no parseable no rompe el track (se guarda como NULL)."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.llm_usage import LLMUsage

    marker_model = f"marker-{uuid.uuid4().hex[:8]}"
    from app.services.usage_tracker import track_usage

    await track_usage("agent", marker_model, 10, 1, agent_id="no-soy-un-uuid")

    async with db_session() as db:
        row = (
            await db.execute(select(LLMUsage).where(LLMUsage.model == marker_model))
        ).scalar_one()
    assert row.agent_id is None
    assert row.total_tokens == 11
