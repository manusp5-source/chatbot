"""Guardrails de coste para el agente interno.

Dos topes independientes:

1. **Diario por usuario** (rate limit): cada admin puede hacer N queries
   al dia. Counter en Redis con TTL 24h. Devuelve 429 si supera.
2. **Mensual de coste agregado** (USD): suma de `llm_usage_log` con
   source='internal_agent' del mes en curso. Si supera el tope de
   `internal_agent_config.monthly_budget_usd`, devuelve 429.

A diferencia del bot publico, **no pausamos nada automaticamente**. El
agente interno solo afecta al admin que pregunta; el rate limit ya
contiene cualquier abuso, no hace falta tocar el resto del sistema.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.services.llm_pricing import estimate_cost_usd

DAILY_KEY_PREFIX = "internal_agent:daily:"  # +{user_id}:{YYYYMMDD}
SOURCE = "internal_agent"


def _daily_key(user_id: uuid.UUID) -> str:
    ymd = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{DAILY_KEY_PREFIX}{user_id}:{ymd}"


async def get_daily_count(user_id: uuid.UUID) -> int:
    r = get_redis()
    raw = await r.get(_daily_key(user_id))
    if raw is None:
        return 0
    try:
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        return 0


async def incr_daily_count(user_id: uuid.UUID) -> int:
    """Incrementa y devuelve el valor nuevo. Setea TTL 26h la primera vez."""
    r = get_redis()
    key = _daily_key(user_id)
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 26 * 3600)
    results = await pipe.execute()
    try:
        return int(results[0])
    except (TypeError, ValueError):
        return 0


async def current_month_cost_usd(db: AsyncSession) -> float:
    """Suma de coste estimado del agente interno en el mes actual."""
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    rows = (
        await db.execute(
            sql_text(
                """
                SELECT model,
                       COALESCE(SUM(prompt_tokens), 0) AS pt,
                       COALESCE(SUM(completion_tokens), 0) AS ct
                FROM llm_usage_log
                WHERE created_at >= :since AND source = :source
                GROUP BY model
                """
            ),
            {"since": month_start, "source": SOURCE},
        )
    ).fetchall()
    from app.services.llm_pricing import get_price_map
    prices = await get_price_map(db)
    total = 0.0
    for r in rows:
        total += estimate_cost_usd(r.model or "", int(r.pt or 0), int(r.ct or 0), prices)
    return round(total, 4)


class BudgetExceeded(Exception):
    """El admin (o el sistema) supero un tope. El endpoint lo traduce a 429."""

    def __init__(self, reason: str, *, kind: str):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind  # "daily_limit" | "monthly_budget"
