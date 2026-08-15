"""Acceso a la fila singleton de internal_agent_config.

El registro vive con id fijo (`InternalAgentConfig.SINGLETON_ID`). El seed
inicial lo crea la migracion 0012 — aqui solo leemos / actualizamos.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.internal_agent_config import InternalAgentConfig


async def get_config(db: AsyncSession) -> InternalAgentConfig:
    """Devuelve la fila singleton. Si por algun motivo no existe, lanza.

    La migracion 0012 la siembra; si falta aqui es un bug operacional, no
    deberiamos auto-crear silenciosamente (podriamos perder un prompt
    custom editado).
    """
    cfg = (
        await db.execute(
            select(InternalAgentConfig).where(
                InternalAgentConfig.id == InternalAgentConfig.SINGLETON_ID
            )
        )
    ).scalar_one_or_none()
    if cfg is None:
        raise RuntimeError(
            "internal_agent_config singleton no encontrado. Ejecuta `alembic upgrade head`."
        )
    return cfg


async def update_config(
    db: AsyncSession,
    *,
    model_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    monthly_budget_usd: float | None = ...,  # sentinel: None es valor valido (sin tope)
    daily_query_limit: int | None = None,
    system_prompt: str | None = None,
    llm_provider_id: uuid.UUID | None = ...,  # sentinel: None = proveedor por defecto
    updated_by: uuid.UUID | None = None,
) -> InternalAgentConfig:
    cfg = await get_config(db)
    if model_name is not None:
        cfg.model_name = model_name
    if llm_provider_id is not ...:  # type: ignore[comparison-overlap]
        cfg.llm_provider_id = llm_provider_id
    if temperature is not None:
        cfg.temperature = temperature
    if max_tokens is not None:
        cfg.max_tokens = max_tokens
    if monthly_budget_usd is not ...:  # type: ignore[comparison-overlap]
        cfg.monthly_budget_usd = monthly_budget_usd
    if daily_query_limit is not None:
        cfg.daily_query_limit = daily_query_limit
    if system_prompt is not None:
        cfg.system_prompt = system_prompt
    if updated_by is not None:
        cfg.updated_by = updated_by
    await db.flush()
    # `updated_at` usa onupdate=func.now(): tras el flush queda EXPIRADO en la
    # instancia (el valor lo calcula la BD). Si luego alguien lo lee en un paso
    # SÍNCRONO (p.ej. _cfg_to_out al construir la respuesta/auditoría), SQLAlchemy
    # intenta un SELECT perezoso fuera del greenlet async → MissingGreenlet →
    # 500. Refrescamos aquí, dentro del contexto async, para dejar todos los
    # atributos cargados y que las lecturas posteriores no hagan IO.
    await db.refresh(cfg)
    return cfg
