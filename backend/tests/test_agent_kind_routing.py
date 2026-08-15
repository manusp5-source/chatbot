"""Resolución de agente por canal: texto y voz no se mezclan.

El fallo que cubren estos tests era el estado POR DEFECTO de toda instalación
nueva: el seed solo creaba el "Agente de Voz", la resolución por canal cogía
"cualquier agente activo, el más antiguo" y WhatsApp acababa atendido por un
agente cuyo prompt dice "atiendes llamadas telefónicas", sin la herramienta de
derivar a una persona y con la respuesta en un solo bloque. Crear un agente
nuevo desde el panel no lo arreglaba: el de voz seguía siendo el más antiguo.
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


async def _isolate(db):
    """Deja la tabla sin agentes activos y sin AgentConfig activo.

    Devuelve lo desactivado para restaurarlo al final.
    """
    prev_agents = (
        await db.execute(text("SELECT id FROM agents WHERE is_active = true"))
    ).scalars().all()
    await db.execute(text("UPDATE agents SET is_active = false WHERE is_active = true"))
    prev_cfg = (
        await db.execute(text("SELECT id FROM agent_config WHERE is_active = true"))
    ).scalars().all()
    await db.execute(
        text("UPDATE agent_config SET is_active = false WHERE is_active = true")
    )
    return prev_agents, prev_cfg


async def _restore(db, prev_agents, prev_cfg, created_ids):
    if created_ids:
        await db.execute(
            text("DELETE FROM agents WHERE id = ANY(:ids)"),
            {"ids": [str(i) for i in created_ids]},
        )
    if prev_agents:
        await db.execute(
            text("UPDATE agents SET is_active = true WHERE id = ANY(:ids)"),
            {"ids": [str(i) for i in prev_agents]},
        )
    if prev_cfg:
        await db.execute(
            text("UPDATE agent_config SET is_active = true WHERE id = ANY(:ids)"),
            {"ids": [str(i) for i in prev_cfg]},
        )


# ── Parte pura (sin BD) ──────────────────────────────────────────────────────


def test_kind_por_canal():
    from app.services.runtime_config import kind_for_channel

    for canal in ("whatsapp", "webchat", "instagram_dm", "email"):
        assert kind_for_channel(canal) == "text"
    assert kind_for_channel("retell_voice") == "voice"
    # Un canal futuro que nadie ha mapeado se considera de texto, no de voz:
    # es lo que menos daño hace si alguien añade un canal y olvida el mapa.
    assert kind_for_channel("telegram") == "text"


# ── Con BD ───────────────────────────────────────────────────────────────────


@db_gated
@pytest.mark.asyncio
async def test_agente_de_voz_no_atiende_whatsapp():
    """EL bug de la auditoría: solo hay agente de voz → WhatsApp NO lo usa."""
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.services.runtime_config import get_runtime_for_channel

    created = []
    async with db_session() as db:
        prev_agents, prev_cfg = await _isolate(db)
        voz = Agent(
            name=f"Voz-{uuid.uuid4().hex[:6]}", kind="voice",
            prompt_system="Atiendes llamadas telefónicas.",
            model_name="gpt-5.4-mini", is_active=True,
        )
        db.add(voz)
        await db.commit()
        created.append(voz.id)

    try:
        # Antes esto devolvía el agente de voz. Ahora no hay nadie adecuado.
        assert await get_runtime_for_channel("whatsapp") is None
        assert await get_runtime_for_channel("instagram_dm") is None
        # La voz sí lo usa.
        voz_rt = await get_runtime_for_channel("retell_voice")
        assert voz_rt is not None
        assert voz_rt.kind == "voice"
    finally:
        async with db_session() as db:
            await _restore(db, prev_agents, prev_cfg, created)
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_cada_canal_coge_el_agente_de_su_naturaleza():
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.services.runtime_config import get_runtime_for_channel

    created = []
    async with db_session() as db:
        prev_agents, prev_cfg = await _isolate(db)
        # El de VOZ es el más antiguo a propósito: era lo que rompía el orden
        # "por fecha de creación" y hacía inútil crear un agente nuevo.
        voz = Agent(
            name=f"Voz-{uuid.uuid4().hex[:6]}", kind="voice",
            prompt_system="voz", model_name="gpt-5.4-mini", is_active=True,
        )
        db.add(voz)
        await db.commit()
        created.append(voz.id)
    async with db_session() as db:
        txt = Agent(
            name=f"Texto-{uuid.uuid4().hex[:6]}", kind="text",
            prompt_system="chat", model_name="gpt-5.4-mini", is_active=True,
        )
        db.add(txt)
        await db.commit()
        created.append(txt.id)

    try:
        wa = await get_runtime_for_channel("whatsapp")
        assert wa is not None
        assert wa.kind == "text"
        # El prompt efectivo lleva delante la capa de seguridad; comprobamos
        # que el prompt del agente que se ha elegido es el de TEXTO.
        assert wa.prompt_system.endswith("chat")

        voz_rt = await get_runtime_for_channel("retell_voice")
        assert voz_rt is not None and voz_rt.kind == "voice"
        assert voz_rt.prompt_system.endswith("voz")
    finally:
        async with db_session() as db:
            await _restore(db, prev_agents, prev_cfg, created)
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_asignacion_explicita_incompatible_se_rechaza():
    """Un canal de texto con un agente de VOZ asignado a mano no lo usa."""
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.models.channel import Channel, ChannelType
    from app.services.runtime_config import get_runtime_for_channel

    created = []
    chan_id = None
    async with db_session() as db:
        prev_agents, prev_cfg = await _isolate(db)
        voz = Agent(
            name=f"Voz-{uuid.uuid4().hex[:6]}", kind="voice",
            prompt_system="voz", model_name="gpt-5.4-mini", is_active=True,
        )
        db.add(voz)
        await db.flush()
        created.append(voz.id)
        canal = Channel(
            type=ChannelType.whatsapp,
            name=f"WA-{uuid.uuid4().hex[:6]}",
            enabled=True,
            agent_id=voz.id,
            config={},
        )
        db.add(canal)
        await db.commit()
        chan_id = canal.id

    try:
        # Ni con asignación explícita: el prompt de llamadas no vale para chat.
        assert await get_runtime_for_channel("whatsapp") is None
    finally:
        async with db_session() as db:
            if chan_id:
                await db.execute(
                    text("DELETE FROM channels WHERE id = :i"), {"i": str(chan_id)}
                )
            await _restore(db, prev_agents, prev_cfg, created)
            await db.commit()


@db_gated
@pytest.mark.asyncio
async def test_legacy_agent_config_solo_para_texto():
    """El singleton legacy tiene prompt de chat: no puede atender llamadas."""
    from app.db.session import db_session
    from app.models.agent_config import AgentConfig
    from app.services.runtime_config import get_runtime_for_channel

    cfg_id = None
    async with db_session() as db:
        prev_agents, prev_cfg = await _isolate(db)
        cfg = AgentConfig(
            prompt_system="prompt legacy de WhatsApp",
            model_name="gpt-5.4-mini",
            is_active=True,
            version=1,
        )
        db.add(cfg)
        await db.commit()
        cfg_id = cfg.id

    try:
        wa = await get_runtime_for_channel("whatsapp")
        assert wa is not None and wa.source == "agent_config"
        assert wa.kind == "text"
        # La voz NO cae al legacy.
        assert await get_runtime_for_channel("retell_voice") is None
    finally:
        async with db_session() as db:
            if cfg_id:
                await db.execute(
                    text("DELETE FROM agent_config WHERE id = :i"), {"i": str(cfg_id)}
                )
            await _restore(db, prev_agents, prev_cfg, [])
            await db.commit()
