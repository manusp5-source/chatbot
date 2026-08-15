"""El tope de gasto POR AGENTE ya tiene consumidor.

`agents.monthly_budget_usd` se editaba en el panel, viajaba en el runtime y no
lo leía nadie: el único tope que se aplicaba era el global de `agent_config`.
Dos pantallas para lo mismo y solo funcionaba una.

Al superar SU tope, el agente se desactiva (no se pausa la instalación entera:
es su presupuesto, no el de los demás), lo que se ve en la lista de Agentes y
hace que `runtime_config` deje de servirlo al momento.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text


def _db_available() -> bool:
    try:
        async def _check():
            from app.db.session import db_session
            async with db_session() as db:
                await db.execute(text("SELECT 1"))
        asyncio.run(_check())
        return True
    except Exception:
        return False


db_gated = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


@db_gated
@pytest.mark.asyncio
async def test_el_agente_que_supera_su_tope_se_desactiva():
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.services.budget import _check_agent_budget, agent_month_cost_usd

    aid = None
    async with db_session() as db:
        agent = Agent(
            name=f"Caro-{uuid.uuid4().hex[:6]}", kind="text",
            prompt_system="x", model_name="gpt-5.4-mini",
            monthly_budget_usd=1.00, is_active=True,
        )
        db.add(agent)
        await db.commit()
        aid = agent.id

    try:
        # 10M tokens de salida de gpt-5.4-mini a 4,50 $/1M = 45 $ >> 1 $.
        async with db_session() as db:
            await db.execute(
                text(
                    "INSERT INTO llm_usage_log (id, source, model, prompt_tokens, "
                    "completion_tokens, agent_id, created_at) VALUES "
                    "(gen_random_uuid(), 'agent', 'gpt-5.4-mini', 0, 10000000, :aid, :now)"
                ),
                {"aid": str(aid), "now": datetime.now(timezone.utc)},
            )
            await db.commit()

        coste = await agent_month_cost_usd(aid)
        assert coste == pytest.approx(45.0, rel=0.01)

        await _check_agent_budget(aid)

        async with db_session() as db:
            activo = (
                await db.execute(
                    text("SELECT is_active FROM agents WHERE id = :i"), {"i": str(aid)}
                )
            ).scalar_one()
        assert activo is False, "el agente debería haberse desactivado por su tope"
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM llm_usage_log WHERE agent_id = :i"), {"i": str(aid)}
            )
            await db.execute(text("DELETE FROM agents WHERE id = :i"), {"i": str(aid)})
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_un_agente_por_debajo_de_su_tope_sigue_activo():
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.services.budget import _check_agent_budget

    aid = None
    async with db_session() as db:
        agent = Agent(
            name=f"Barato-{uuid.uuid4().hex[:6]}", kind="text",
            prompt_system="x", model_name="gpt-5.4-mini",
            monthly_budget_usd=100.00, is_active=True,
        )
        db.add(agent)
        await db.commit()
        aid = agent.id

    try:
        async with db_session() as db:
            await db.execute(
                text(
                    "INSERT INTO llm_usage_log (id, source, model, prompt_tokens, "
                    "completion_tokens, agent_id, created_at) VALUES "
                    "(gen_random_uuid(), 'agent', 'gpt-5.4-mini', 1000, 500, :aid, :now)"
                ),
                {"aid": str(aid), "now": datetime.now(timezone.utc)},
            )
            await db.commit()

        await _check_agent_budget(aid)
        async with db_session() as db:
            activo = (
                await db.execute(
                    text("SELECT is_active FROM agents WHERE id = :i"), {"i": str(aid)}
                )
            ).scalar_one()
        assert activo is True
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM llm_usage_log WHERE agent_id = :i"), {"i": str(aid)}
            )
            await db.execute(text("DELETE FROM agents WHERE id = :i"), {"i": str(aid)})
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_sin_tope_propio_no_se_toca_al_agente():
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.services.budget import _check_agent_budget

    aid = None
    async with db_session() as db:
        agent = Agent(
            name=f"SinTope-{uuid.uuid4().hex[:6]}", kind="text",
            prompt_system="x", model_name="gpt-5.4-mini",
            monthly_budget_usd=None, is_active=True,
        )
        db.add(agent)
        await db.commit()
        aid = agent.id

    try:
        async with db_session() as db:
            await db.execute(
                text(
                    "INSERT INTO llm_usage_log (id, source, model, prompt_tokens, "
                    "completion_tokens, agent_id, created_at) VALUES "
                    "(gen_random_uuid(), 'agent', 'gpt-5.4-mini', 0, 10000000, :aid, :now)"
                ),
                {"aid": str(aid), "now": datetime.now(timezone.utc)},
            )
            await db.commit()
        await _check_agent_budget(aid)
        async with db_session() as db:
            activo = (
                await db.execute(
                    text("SELECT is_active FROM agents WHERE id = :i"), {"i": str(aid)}
                )
            ).scalar_one()
        assert activo is True
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM llm_usage_log WHERE agent_id = :i"), {"i": str(aid)}
            )
            await db.execute(text("DELETE FROM agents WHERE id = :i"), {"i": str(aid)})
            await db.commit()
