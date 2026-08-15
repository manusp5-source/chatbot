"""Persistencia best-effort de cada llamada a OpenAI (tokens consumidos).

Los providers (`openai_client`, `openai_embeddings`, `moderation`) llaman a
`track_usage(...)` tras cada peticion exitosa. Si la persistencia falla
(BD caida, etc) NUNCA bloqueamos la respuesta del agente por un fallo del
telemetry, pero tampoco lo dejamos pasar en silencio:

- se loguea como ERROR (antes era un warning que nadie miraba), y
- los tokens que no llegaron a BD se apuntan en un contador de Redis del mes
  (`llm:untracked:{YYYY-MM}`) que `app.services.budget` SUMA al calcular el
  gasto. Sin esto, un rato con la BD tocada dejaba gasto REAL invisible y el
  tope mensual lo contaba como 0 $: el guardarraíl fallaba abierto justo
  cuando hacia falta.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.llm_usage import LLMUsage

logger = get_logger(__name__)

# Hash por mes: campos "{modelo}|pt" y "{modelo}|ct". TTL 33 dias (cubre el
# cambio de mes natural, igual que los flags de budget).
_UNTRACKED_KEY = "llm:untracked:{ym}"
_UNTRACKED_TTL = 33 * 24 * 3600


def _current_ym() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


async def record_untracked_usage(
    model: str, prompt_tokens: int = 0, completion_tokens: int = 0
) -> None:
    """Apunta consumo que NO se pudo escribir en BD, para que el tope lo vea.

    Ultimo recurso: si Redis tampoco responde, lo unico que queda es el log de
    error (y ahi si perdemos la trazabilidad de ese gasto)."""
    try:
        r = get_redis()
        key = _UNTRACKED_KEY.format(ym=_current_ym())
        await r.hincrby(key, f"{model}|pt", int(prompt_tokens or 0))
        await r.hincrby(key, f"{model}|ct", int(completion_tokens or 0))
        await r.expire(key, _UNTRACKED_TTL)
    except Exception as e:
        logger.error(
            "usage.untracked.redis_failed",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=str(e),
        )


async def untracked_usage_tokens() -> dict[str, tuple[int, int]]:
    """{modelo: (prompt_tokens, completion_tokens)} del consumo del mes que no
    llego a BD. Propaga si Redis falla: quien llama decide que hacer con el
    "no lo se" (budget NO lo trata como 0)."""
    raw = await get_redis().hgetall(_UNTRACKED_KEY.format(ym=_current_ym()))
    acc: dict[str, list[int]] = {}
    for k, v in (raw or {}).items():
        field = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        model, _, kind = field.rpartition("|")
        if not model or kind not in ("pt", "ct"):
            continue
        slot = acc.setdefault(model, [0, 0])
        try:
            slot[0 if kind == "pt" else 1] = int(v)
        except (TypeError, ValueError):
            continue
    return {m: (t[0], t[1]) for m, t in acc.items()}


async def track_usage(
    source: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    conversation_id: UUID | None = None,
    agent_id: UUID | str | None = None,
) -> None:
    # agent_id puede llegar como str (viene de un ContextVar) o UUID. Lo
    # normalizamos a UUID; si no es válido, lo ignoramos (best-effort, nunca
    # rompemos el telemetry por esto).
    agent_uuid: UUID | None = None
    if agent_id is not None:
        try:
            agent_uuid = agent_id if isinstance(agent_id, UUID) else UUID(str(agent_id))
        except (ValueError, AttributeError, TypeError):
            agent_uuid = None
    try:
        async with db_session() as db:
            db.add(
                LLMUsage(
                    source=source,
                    model=model,
                    prompt_tokens=int(prompt_tokens or 0),
                    completion_tokens=int(completion_tokens or 0),
                    total_tokens=int((prompt_tokens or 0) + (completion_tokens or 0)),
                    conversation_id=conversation_id,
                    agent_id=agent_uuid,
                )
            )
            await db.commit()
    except Exception as e:
        # Ruidoso a proposito: este fallo deja gasto REAL sin registrar.
        logger.error(
            "usage.track.error",
            source=source,
            model=model,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            error=str(e),
        )
        await record_untracked_usage(model, prompt_tokens, completion_tokens)
