"""Semillas mínimas para los tests que van contra la base de datos.

Motivo: varios tests de punta a punta ejecutan el runtime del agente
(`process_buffered_messages`), y ese runtime sale EN SILENCIO si no hay ningún
agente activo (`get_runtime_for_channel` devuelve None). Con la base recién
migrada no hay ninguno, así que esos tests solo pasaban si otro test había
dejado un agente por ahí. Es decir: no se estaban validando.

Cada test que necesite un agente llama a `ensure_text_agent()` y se acabó la
dependencia del orden de ejecución.
"""
from __future__ import annotations


async def ensure_text_agent(name: str = "SeedTestAgent") -> None:
    """Garantiza que hay un Agent de TEXTO activo (idempotente).

    Si ya hay alguno activo del kind de texto, no hace nada: así no se pisa lo
    que hayan preparado otros tests ni se altera lo que ya existiera.
    """
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.agent import Agent

    async with db_session() as db:
        existing = (
            await db.execute(select(Agent).where(Agent.is_active.is_(True)))
        ).scalars().all()
        for agent in existing:
            # `kind` es reciente; en filas antiguas puede no venir. Sin kind se
            # asume texto, que es el comportamiento histórico.
            if (getattr(agent, "kind", None) or "text") == "text":
                return
        agent = Agent(
            name=name,
            prompt_system="Eres el asistente de pruebas.",
            model_name="gpt-5.4-mini",
            buffer_seconds=1,
            is_active=True,
        )
        if hasattr(Agent, "kind"):
            agent.kind = "text"
        db.add(agent)
        await db.commit()
