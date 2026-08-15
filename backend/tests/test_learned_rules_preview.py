"""Reglas aprendidas: se puede ver cómo queda el prompt antes de aprobar.

Las reglas se montan AL FINAL del prompt, que es la posición de más peso, y su
propio texto dice que tienen prioridad. Al aprobar una no se comprobaba nada
contra el prompt, no había forma de editarla (solo desactivar) y son GLOBALES a
todos los agentes, sin que el panel lo advirtiera.
"""
import asyncio
import uuid

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


# ── Detección de conflictos (pura) ───────────────────────────────────────────


def test_detecta_una_regla_que_contradice_el_prompt():
    from app.api.learning import _detect_conflicts

    prompt = (
        "Eres el asistente de la tienda.\n"
        "- Nunca uses emojis en tus respuestas.\n"
        "- Responde siempre en español.\n"
    )
    conflictos = _detect_conflicts("Usa emojis para sonar más cercana", prompt)
    assert any("emojis" in c for c in conflictos)


def test_una_regla_inocua_no_genera_ruido():
    from app.api.learning import _detect_conflicts

    prompt = "- Nunca uses emojis en tus respuestas.\n- Responde en español.\n"
    assert _detect_conflicts("Confirma la cita repitiendo el día", prompt) == []


def test_regla_vacia_no_rompe():
    from app.api.learning import _detect_conflicts

    assert _detect_conflicts("", "- Nunca uses emojis.") == []
    assert _detect_conflicts("algo", "") == []


# ── Endpoint de previsualización ─────────────────────────────────────────────


@db_gated
@pytest.mark.asyncio
async def test_preview_enseña_el_prompt_resultante_y_avisa_de_que_es_global():
    from app.api.learning import RulePreviewIn, preview_rule
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.models.user import User

    ids = []
    user_id = uuid.uuid4()
    async with db_session() as db:
        db.add(
            User(
                id=user_id, email=f"prev-{uuid.uuid4().hex[:8]}@test.local",
                password_hash="x", nombre="Prev", role="admin",
            )
        )
        for n in ("Uno", "Dos"):
            a = Agent(
                name=f"{n}-{uuid.uuid4().hex[:6]}", kind="text",
                prompt_system="- Nunca uses emojis en tus respuestas.",
                model_name="gpt-5.4-mini", is_active=True,
            )
            db.add(a)
            await db.flush()
            ids.append(a.id)
        await db.commit()

    try:
        async with db_session() as db:
            u = await db.get(User, user_id)
            out = await preview_rule(
                RulePreviewIn(text="Usa emojis para sonar cercana", agent_id=ids[0]),
                db=db,
                _=u,
            )
        # La regla candidata aparece en el bloque y en el prompt efectivo.
        assert "Usa emojis para sonar cercana" in out.rules_block
        assert "Usa emojis para sonar cercana" in out.effective_prompt
        # Y el prompt efectivo es el de verdad: seguridad + agente + reglas.
        assert "REGLAS DE SEGURIDAD" in out.effective_prompt
        assert "Nunca uses emojis" in out.effective_prompt
        # El aviso de alcance: son globales, no de este agente.
        assert out.global_scope is True
        assert len(out.affected_agents) >= 2
        # Y señala la instrucción que la regla contradice.
        assert any("emojis" in c for c in out.posibles_conflictos)
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM agents WHERE id = ANY(:ids)"),
                {"ids": [str(i) for i in ids]},
            )
            await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(user_id)})
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_una_regla_se_puede_corregir_sin_tirarla():
    from app.api.learning import RuleUpdateIn, update_rule
    from app.db.session import db_session
    from app.models.learned_rule import LearnedRule
    from app.models.user import User

    user_id = uuid.uuid4()
    rid = None
    async with db_session() as db:
        db.add(
            User(
                id=user_id, email=f"edit-{uuid.uuid4().hex[:8]}@test.local",
                password_hash="x", nombre="Edit", role="admin",
            )
        )
        r = LearnedRule(text="Regla mal redactada  con   espacios", active=True)
        db.add(r)
        await db.commit()
        rid = r.id

    try:
        async with db_session() as db:
            u = await db.get(User, user_id)
            out = await update_rule(
                rule_id=rid, body=RuleUpdateIn(text="Sé más breve al confirmar"),
                db=db, user=u,
            )
        assert out.text == "Sé más breve al confirmar"
        assert out.active is True
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM learned_rules WHERE id = :i"), {"i": str(rid)}
            )
            await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(user_id)})
            await db.commit()
