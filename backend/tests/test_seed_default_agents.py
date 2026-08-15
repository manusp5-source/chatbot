"""El seed deja una instalación limpia en un estado utilizable.

Antes solo sembraba el "Agente de Voz". Ahora siembra también el de TEXTO, con
la herramienta de derivar a una persona, que es lo que atiende WhatsApp, el
widget web, Instagram y el correo el día 1.
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


def test_el_agente_de_texto_puede_derivar_a_humano():
    """El de VOZ no deriva (es correcto); el de TEXTO sí, y es lo que faltaba."""
    from app.scripts.seed import TEXT_AGENT_TOOLS, VOICE_AGENT_TOOLS

    assert "derivar_humano" not in VOICE_AGENT_TOOLS
    assert "derivar_humano" in TEXT_AGENT_TOOLS
    assert "consultar_kb" in TEXT_AGENT_TOOLS


def test_las_tools_sembradas_existen_de_verdad():
    """Un nombre mal escrito en el seed se descarta en silencio al filtrar."""
    from app.agents.tools import ALL_TOOLS
    from app.scripts.seed import TEXT_AGENT_TOOLS, VOICE_AGENT_TOOLS

    for name in set(TEXT_AGENT_TOOLS) | set(VOICE_AGENT_TOOLS):
        assert name in ALL_TOOLS, f"tool inexistente en el seed: {name}"


@db_gated
@pytest.mark.asyncio
async def test_seed_crea_agente_de_texto_y_es_idempotente():
    from app.db.session import db_session
    from app.scripts.seed import TEXT_AGENT_NAME, TEXT_AGENT_TOOLS, seed_text_agent

    # Partimos de limpio para este nombre.
    async with db_session() as db:
        await db.execute(
            text("DELETE FROM agents WHERE name = :n"), {"n": TEXT_AGENT_NAME}
        )
        await db.commit()

    try:
        # forzar=True: esta prueba mira el camino de creación, y la base que
        # comparten las pruebas siempre trae agentes de otras.
        await seed_text_agent(forzar=True)
        await seed_text_agent(forzar=True)  # dos veces: no debe duplicar

        async with db_session() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT kind, is_active, tools_enabled FROM agents WHERE name = :n"
                    ),
                    {"n": TEXT_AGENT_NAME},
                )
            ).all()
        assert len(rows) == 1
        kind, is_active, tools = rows[0]
        assert kind == "text"
        assert is_active is True
        assert set(tools) == set(TEXT_AGENT_TOOLS)
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM agents WHERE name = :n"), {"n": TEXT_AGENT_NAME}
            )
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_no_siembra_plantillas_en_una_instalacion_que_ya_trabaja():
    """El caso de una instalación viva, no de una recién hecha.

    Pasó de verdad: un despliegue metió "Agente de Texto" con la
    plantilla dentro en una instalación con seis agentes propios. Nadie lo usaba,
    pero el checklist de Inicio se quedó pendiente para siempre, porque busca
    justo eso: un agente activo con marcadores `[[ RELLENAR ]]`.
    """
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.scripts.seed import (
        TEXT_AGENT_NAME,
        VOICE_AGENT_NAME,
        seed_text_agent,
        seed_voice_agent,
    )

    propio = f"Agente de la casa {uuid.uuid4().hex[:6]}"
    async with db_session() as db:
        await db.execute(
            text("DELETE FROM agents WHERE name IN (:t, :v)"),
            {"t": TEXT_AGENT_NAME, "v": VOICE_AGENT_NAME},
        )
        db.add(
            Agent(
                name=propio, kind="text", prompt_system="el prompt escrito a mano",
                model_name="gpt-5.4-mini", is_active=True,
            )
        )
        await db.commit()

    try:
        await seed_text_agent()
        await seed_voice_agent()

        async with db_session() as db:
            sembrados = (
                await db.execute(
                    text("SELECT count(*) FROM agents WHERE name IN (:t, :v)"),
                    {"t": TEXT_AGENT_NAME, "v": VOICE_AGENT_NAME},
                )
            ).scalar_one()
            intacto = (
                await db.execute(
                    text("SELECT prompt_system FROM agents WHERE name = :n"), {"n": propio}
                )
            ).scalar_one()
        assert sembrados == 0, "no se siembra plantilla donde ya hay agentes propios"
        assert intacto == "el prompt escrito a mano"
    finally:
        async with db_session() as db:
            await db.execute(text("DELETE FROM agents WHERE name = :n"), {"n": propio})
            await db.execute(
                text("DELETE FROM agents WHERE name IN (:t, :v)"),
                {"t": TEXT_AGENT_NAME, "v": VOICE_AGENT_NAME},
            )
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_en_instalacion_limpia_si_se_siembran_las_plantillas():
    """El motivo por el que existen: sin esto, WhatsApp lo atendía el de voz."""
    from app.db.session import db_session
    from app.scripts.seed import (
        TEXT_AGENT_NAME,
        VOICE_AGENT_NAME,
        seed_text_agent,
        seed_voice_agent,
    )

    async with db_session() as db:
        previos = (await db.execute(text("SELECT count(*) FROM agents"))).scalar_one()
    if previos:
        pytest.skip("la base de este entorno ya trae agentes; el caso limpio se cubre en CI")

    try:
        await seed_text_agent()
        await seed_voice_agent()
        async with db_session() as db:
            sembrados = (
                await db.execute(
                    text("SELECT count(*) FROM agents WHERE name IN (:t, :v)"),
                    {"t": TEXT_AGENT_NAME, "v": VOICE_AGENT_NAME},
                )
            ).scalar_one()
        assert sembrados == 2
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM agents WHERE name IN (:t, :v)"),
                {"t": TEXT_AGENT_NAME, "v": VOICE_AGENT_NAME},
            )
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_seed_marca_como_voz_un_agente_de_voz_preexistente():
    """Instalaciones anteriores a la columna `kind` quedaron con 'text'."""
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.scripts.seed import VOICE_AGENT_NAME, seed_voice_agent

    async with db_session() as db:
        await db.execute(
            text("DELETE FROM agents WHERE name = :n"), {"n": VOICE_AGENT_NAME}
        )
        # Simula la fila que dejó la migración con el default 'text'.
        db.add(
            Agent(
                name=VOICE_AGENT_NAME, kind="text",
                prompt_system=f"viejo {uuid.uuid4().hex[:4]}",
                model_name="gpt-5.4-mini", is_active=True,
            )
        )
        await db.commit()

    try:
        await seed_voice_agent()
        async with db_session() as db:
            rows = (
                await db.execute(
                    text("SELECT kind, prompt_system FROM agents WHERE name = :n"),
                    {"n": VOICE_AGENT_NAME},
                )
            ).all()
        assert len(rows) == 1
        kind, prompt = rows[0]
        assert kind == "voice"
        # Y NO se pisa la personalización que hubiera hecho el usuario.
        assert prompt.startswith("viejo ")
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM agents WHERE name = :n"), {"n": VOICE_AGENT_NAME}
            )
            await db.commit()
