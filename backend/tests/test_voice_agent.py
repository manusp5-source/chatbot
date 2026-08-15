"""Tests del backend del agente de voz (V-01).

Cobertura:
  - Enum: ConversationCanal.retell_voice válido y persistible (insert real).
  - Pausa (V-02): retell_voice reconocido por agent_pause.KNOWN_CHANNELS.
  - Runtime: get_runtime_for_channel("retell_voice") devuelve el Agent de voz
    cuando está asignado al Channel retell_voice.
  - Seed idempotente: seed_voice_agent() dos veces NO duplica el Agent.
  - Filtrado de tools del orchestrator (no necesita DB): tools_enabled
    restringe de verdad las tools (derivar_humano fuera de la voz).

DB-gated con skipif (mismo patrón que test_contact_activity / test_contact_notes):
los casos que tocan BD se saltan si no hay Postgres disponible. El filtrado de
tools es lógica pura y corre siempre.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_contact_activity.py)
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


# ---------------------------------------------------------------------------
# Filtrado de tools (lógica pura — sin DB)
# ---------------------------------------------------------------------------


def test_pause_knows_retell_voice():
    """V-02: retell_voice es un canal conocido por la pausa."""
    from app.services.agent_pause import KNOWN_CHANNELS

    assert "retell_voice" in KNOWN_CHANNELS


def test_enum_has_retell_voice():
    """El miembro Python del enum existe y vale 'retell_voice'."""
    from app.models.conversation import ConversationCanal

    assert ConversationCanal.retell_voice.value == "retell_voice"


def test_voice_tools_exclude_handoff():
    """El agente de voz no incluye derivar_humano y sus tools existen."""
    from app.agents.tools import ALL_TOOLS
    from app.scripts.seed import VOICE_AGENT_TOOLS

    assert "derivar_humano" not in VOICE_AGENT_TOOLS
    assert all(t in ALL_TOOLS for t in VOICE_AGENT_TOOLS)


def test_resolve_allowed_tools_whitelist():
    """tools_enabled restringe de verdad; None = todas, [] = ninguna."""
    from app.agents.orchestrator import _resolve_allowed_tools
    from app.agents.tools import ALL_TOOLS

    # None = SIN CONFIGURAR → todas
    assert _resolve_allowed_tools(None) == set(ALL_TOOLS.keys())
    # Lista vacía = NINGUNA, elegido a propósito desde el panel. Antes devolvía
    # todas (una lista vacía es falsy), así que desmarcar todas las casillas
    # acababa activando TODAS las herramientas.
    assert _resolve_allowed_tools([]) == set()
    # Lista concreta → solo esas (intersección con las registradas)
    allowed = _resolve_allowed_tools(["consultar_kb", "agendar_cita", "inexistente"])
    assert allowed == {"consultar_kb", "agendar_cita"}
    assert "derivar_humano" not in allowed


def test_run_tool_blocks_disallowed():
    """_run_tool rechaza una tool fuera de la lista blanca (defensa en profundidad)."""
    import json

    from app.agents.orchestrator import _run_tool
    from app.providers.llm.base import LLMToolCall

    call = LLMToolCall(id="x", name="derivar_humano", arguments={})
    allowed = {"consultar_kb"}
    result = asyncio.run(_run_tool(call, {}, allowed))
    parsed = json.loads(result)
    assert "error" in parsed
    assert "no disponible" in parsed["error"].lower()


# ---------------------------------------------------------------------------
# DB-gated: enum persistible + runtime + seed idempotente
# ---------------------------------------------------------------------------


async def _make_contact():
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen

    async with db_session() as db:
        c = Contact(
            telefono=f"voice:test:{uuid.uuid4().hex[:8]}",
            origen=ContactOrigen.manual,
            in_crm=False,
        )
        db.add(c)
        await db.commit()
        await db.refresh(c)
        return c.id


@pytestmark_db
def test_enum_retell_voice_persistible():
    """Inserta una Conversation con canal=retell_voice (valida que el enum de BD lo acepta)."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import (
        Conversation,
        ConversationCanal,
        ConversationStatus,
    )

    async def _run():
        contact_id = await _make_contact()
        async with db_session() as db:
            conv = Conversation(
                contact_id=contact_id,
                canal=ConversationCanal.retell_voice,
                session_id=f"call_{uuid.uuid4().hex[:10]}",
                status=ConversationStatus.bot,
            )
            db.add(conv)
            await db.commit()
            conv_id = conv.id
        async with db_session() as db:
            got = (
                await db.execute(select(Conversation).where(Conversation.id == conv_id))
            ).scalar_one()
            assert got.canal == ConversationCanal.retell_voice
        # limpieza
        async with db_session() as db:
            from app.models.contact import Contact

            c = (
                await db.execute(select(Contact).where(Contact.id == contact_id))
            ).scalar_one()
            await db.delete(c)  # cascade borra la conversación
            await db.commit()

    asyncio.run(_run())


@pytestmark_db
def test_seed_voice_agent_idempotent_and_runtime():
    """Seed dos veces no duplica, y el Channel retell_voice resuelve a ese Agent."""
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.agent import Agent
    from app.models.channel import Channel, ChannelType
    from app.scripts.seed import VOICE_AGENT_NAME, seed_voice_agent
    from app.services.runtime_config import get_runtime_for_channel

    async def _run():
        # Crea un canal retell_voice enabled de prueba (sin agente).
        async with db_session() as db:
            ch = Channel(
                type=ChannelType.retell_voice,
                name=f"Retell test {uuid.uuid4().hex[:6]}",
                enabled=True,
                config={},
            )
            db.add(ch)
            await db.commit()
            ch_id = ch.id

        try:
            # Ejecuta el seed dos veces → idempotente. forzar=True porque la
            # base compartida ya tiene agentes de otras pruebas y, sin eso, el
            # seed se salta la siembra (que es lo correcto en producción).
            await seed_voice_agent(forzar=True)
            await seed_voice_agent(forzar=True)

            async with db_session() as db:
                count = (
                    await db.execute(
                        select(func.count())
                        .select_from(Agent)
                        .where(Agent.name == VOICE_AGENT_NAME)
                    )
                ).scalar_one()
                assert count == 1, f"esperaba 1 Agente de Voz, hay {count}"

                agent = (
                    await db.execute(select(Agent).where(Agent.name == VOICE_AGENT_NAME))
                ).scalar_one()
                assert "derivar_humano" not in (agent.tools_enabled or [])
                assert "consultar_kb" in (agent.tools_enabled or [])

                # El canal quedó cableado a este agente + greeting sembrado.
                ch = (
                    await db.execute(select(Channel).where(Channel.id == ch_id))
                ).scalar_one()
                assert ch.agent_id == agent.id
                assert (ch.config or {}).get("greeting")

            # get_runtime_for_channel("retell_voice") devuelve el agente de voz.
            runtime = await get_runtime_for_channel("retell_voice")
            assert runtime is not None
            assert runtime.name == VOICE_AGENT_NAME
            assert runtime.source == "agents"
            assert "derivar_humano" not in (runtime.tools_enabled or [])
        finally:
            # limpieza del canal de prueba (no borramos el Agent: es seed real)
            async with db_session() as db:
                ch = (
                    await db.execute(select(Channel).where(Channel.id == ch_id))
                ).scalar_one_or_none()
                if ch:
                    await db.delete(ch)
                    await db.commit()

    asyncio.run(_run())
