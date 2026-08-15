"""Tarea Celery: refresca los precios de modelos desde OpenRouter.

Corre una vez al día (ver beat_schedule en app/tasks/__init__.py, 06:40). Trae
el listado público de OpenRouter y actualiza llm_model_price para los modelos
que la app usa. Best-effort: si OpenRouter no responde, se loguea y no pasa nada
(la estimación sigue con los precios que ya había).
"""
from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.tasks import celery_app

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.refresh_prices.refresh_prices")
def refresh_prices() -> dict:
    async def _run() -> dict:
        from app.db.session import db_session
        from app.services.llm_pricing import refresh_price_cache
        from app.services.openrouter_pricing import refresh_model_prices

        async with db_session() as db:
            summary = await refresh_model_prices(db)
            await db.commit()
        # Recarga la caché del proceso worker (informativo; el proceso API se
        # recarga en su propio arranque + cuando se pulsa "Actualizar" en el panel).
        await refresh_price_cache()
        return summary

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.warning("refresh_prices.error", error=str(e))
        return {"updated": [], "unmatched": [], "error": str(e)}
